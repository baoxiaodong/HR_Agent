"""
普通 AI 聊天的业务编排服务。

一次请求依次完成：获取或创建会话、保存用户消息、读取最近历史、按需检索相关文档、
调用 ``LLMService``、保存助手消息并组装响应。流式方法复用同一链路，只是逐 token
向上游产出内容，结束后再持久化完整助手消息。
"""
import logging
import json
from typing import List, Dict, Any, Optional, AsyncGenerator
from uuid import UUID
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.llm_service import LLMService
from app.services.conversation_service import ConversationService
from app.services.document_service import DocumentService
from app.schemas.chat import ChatResponse, ChatRequest
from app.schemas.conversation import ConversationCreate
from app.schemas.user import User as UserSchema
from app.models.conversation import MessageRole

logger = logging.getLogger(__name__)


class BaseChatService:
    """提供会话恢复/创建与聊天错误包装，供具体聊天用例复用。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.llm_service = LLMService()
        self.conversation_service = ConversationService(db)
        self.document_service = DocumentService(db)

    async def get_or_create_conversation(
        self,
        chat_request: ChatRequest,
        current_user: UserSchema
    ) -> Any:
        """恢复当前用户已有会话，或用首条消息创建新会话。

        ``current_user`` 来自认证依赖；当请求携带会话 ID 时，查询条件直接包含用户 ID，未找到
        与越权统一返回 404。未携带 ID 时只取消息前 50 个字符生成标题，再由会话服务提交记录。
        """
        if chat_request.conversation_id:
            # 在服务查询中加入所有者条件，客户端不能借会话 ID 读取他人上下文。
            conversation = await self.conversation_service.get_conversation(
                conversation_id=chat_request.conversation_id,
                user_id=current_user.id
            )
            if not conversation or conversation.user_id != current_user.id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="对话未找到"
                )
            return conversation
        else:
            # 创建新对话
            conversation_data = ConversationCreate(
                title=chat_request.message[:50] + "..." if len(chat_request.message) > 50 else chat_request.message
            )
            return await self.conversation_service.create_conversation(
                user_id=current_user.id,
                conversation_data=conversation_data
            )

    def handle_chat_error(self, error: Exception, operation: str) -> HTTPException:
        """
        统一处理聊天相关错误
        
        Args:
            error: 异常对象
            operation: 操作描述
            
        Returns:
            HTTPException: 格式化后的HTTP异常
        """
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{operation}时出错: {str(error)}"
        )


class ChatService(BaseChatService):
    """编排用户消息持久化、可选文档上下文、模型回答及反馈写入。"""

    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def process_message(
        self,
        user_id: UUID,
        conversation_id: UUID,
        message: str,
        context: Optional[Dict[str, Any]] = None
    ) -> ChatResponse:
        """执行一次完整的非流式聊天编排。

        上层应已确认 ``conversation_id`` 属于 ``user_id``；本方法依次提交用户消息、读取历史、
        按开关检索当前用户文档、调用 LLM、再提交助手消息。各消息保存是独立事务，因此 LLM
        或助手消息保存失败时，先前用户消息仍会保留。最终把 ORM 时间和 ID 转为响应 Schema。
        """
        try:
            # 第一笔提交先持久化用户输入，后续远程模型失败不会自动撤销它。
            user_message = await self.conversation_service.add_message(
                conversation_id=conversation_id,
                content=message,
                role=MessageRole.USER
            )

            # 历史包含刚保存的用户消息；切片排除最后一项，当前问题由 message 单独传给 LLM。
            history = await self.conversation_service.get_conversation_messages(
                conversation_id=conversation_id,
                limit=20
            )

            # ORM 枚举和文本转换成 OpenAI 兼容的 role/content 消息字典。
            conversation_history = []
            for msg in history[:-1]:  # 排除当前消息，防止同一问题在提示中出现两次
                conversation_history.append({
                    "role": msg.role.value,
                    "content": msg.content
                })

            # context 中的开关只决定是否使用检索上下文，不是权限声明；文档服务仍按 user_id 隔离。
            relevant_context = ""
            if context and context.get("search_documents", True):
                search_results = await self.document_service.search_documents(
                    query=message,
                    user_id=user_id,
                    limit=3
                )
                if search_results:
                    # 检索字典被压缩为纯文本提示，每篇最多取前 500 字，避免上下文无限膨胀。
                    relevant_context = "\n".join([
                        f"文档: {doc['filename']}\n内容: {doc['content'][:500]}..."
                        for doc in search_results
                    ])

            # LLM 服务只接收普通字典和文本，不知道用户、会话 ID 或数据库对象。
            ai_response = await self.llm_service.generate_response(
                message=message,
                conversation_history=conversation_history,
                context=relevant_context
            )

            # 第二笔独立提交保存完整助手回复，并记录本次注入了多少篇检索文档。
            ai_message = await self.conversation_service.add_message(
                conversation_id=conversation_id,
                content=ai_response,
                role=MessageRole.ASSISTANT,
                model_name=self.llm_service.chat_model.model_name,
                context={"relevant_documents": len(search_results) if 'search_results' in locals() else 0}
            )

            return ChatResponse(
                message_id=str(ai_message.id),
                conversation_id=str(conversation_id),
                content=ai_response,
                role=MessageRole.ASSISTANT,
                timestamp=ai_message.created_at,
                metadata={
                    "model_name": self.llm_service.chat_model.model_name,
                    "has_context": bool(relevant_context)
                }
            )

        except Exception as e:
            logger.error(f"处理消息时出错: {e}")
            raise

    async def stream_message(
        self,
        user_id: UUID,
        conversation_id: UUID,
        message: str,
        context: Optional[Dict[str, Any]] = None
    ) -> AsyncGenerator[str, None]:
        """复用聊天链路，以 JSON 文本事件逐段输出模型结果。

        用户消息在流开始前提交；每个 token 立即向 API 层 yield，完整助手消息只在模型流正常
        结束后保存。如果模型异常或客户端取消消费，可能只留下用户消息。内部异常被转换为
        ``error`` 事件而不是继续抛出，成功末尾发送带消息 ID 的 ``complete`` 事件。
        """
        try:
            # 与非流式路径相同，用户消息先形成独立持久化事实。
            user_message = await self.conversation_service.add_message(
                conversation_id=conversation_id,
                content=message,
                role=MessageRole.USER
            )

            # 历史包含刚保存的用户消息；切片排除最后一项，当前问题由 message 单独传给 LLM。
            history = await self.conversation_service.get_conversation_messages(
                conversation_id=conversation_id,
                limit=20
            )

            # ORM 枚举和文本转换成 OpenAI 兼容的 role/content 消息字典。
            conversation_history = []
            for msg in history[:-1]:  # 排除当前消息，防止同一问题在提示中出现两次
                conversation_history.append({
                    "role": msg.role.value,
                    "content": msg.content
                })

            # context 中的开关只决定是否使用检索上下文，不是权限声明；文档服务仍按 user_id 隔离。
            relevant_context = ""
            if context and context.get("search_documents", True):
                search_results = await self.document_service.search_documents(
                    query=message,
                    user_id=user_id,
                    limit=3
                )
                if search_results:
                    # 检索字典被压缩为纯文本提示，每篇最多取前 500 字，避免上下文无限膨胀。
                    relevant_context = "\n".join([
                        f"文档: {doc['filename']}\n内容: {doc['content'][:500]}..."
                        for doc in search_results
                    ])

            # 累积值用于最终持久化；客户端收到的是 JSON 字符串而不是原始 token 对象。
            full_response = ""
            async for token in self.llm_service.stream_response(
                message=message,
                conversation_history=conversation_history,
                context=relevant_context
            ):
                full_response += token
                yield json.dumps({"token": token, "type": "token"})

            # 只有远程流正常结束后才提交完整助手消息，确保数据库内容不是半截响应。
            ai_message = await self.conversation_service.add_message(
                conversation_id=conversation_id,
                content=full_response,
                role=MessageRole.ASSISTANT,
                model_name=self.llm_service.chat_model.model_name,
                context={"relevant_documents": len(search_results) if 'search_results' in locals() else 0}
            )

            # 发送完成信号
            yield json.dumps({
                "type": "complete",
                "message_id": str(ai_message.id),
                "timestamp": ai_message.created_at.isoformat()
            })

        except Exception as e:
            logger.error(f"流式传输消息时出错: {e}")
            yield json.dumps({"type": "error", "error": str(e)})

    async def get_suggestions(self, query: str, user_id: UUID) -> List[str]:
        """用当前用户最近会话标题作为轻量上下文生成后续问题建议。

        只向 LLM 暴露最多五个标题，不发送完整消息正文；模型或查询异常时降级为空列表，
        建议功能失败不会影响主聊天流程，也不写数据库。
        """
        try:
            # 会话服务固定按 user_id 查询，其他用户的标题不会进入提示。
            recent_conversations = await self.conversation_service.get_user_conversations(
                user_id=user_id,
                limit=5
            )

            context = ""
            if recent_conversations:
                context = "最近的对话主题: " + ", ".join([
                    conv.title for conv in recent_conversations
                ])

            suggestions = await self.llm_service.generate_suggestions(query, context)
            return suggestions

        except Exception as e:
            logger.error(f"获取建议时出错: {e}")
            return []

    async def submit_feedback(
        self,
        message_id: str,
        user_id: UUID,
        rating: int,
        feedback: str = ""
    ) -> None:
        """委托会话服务写入消息反馈。

        当前实现虽然接收并记录 ``user_id``，但底层更新只按 ``message_id`` 执行，没有用该用户
        校验消息所属会话；因此面向用户的端点必须在调用前完成消息/会话归属验证。写入失败
        继续向上抛出，由 API 层决定错误响应。
        """
        try:
            await self.conversation_service.update_message_feedback(
                message_id=message_id,
                rating=rating,
                feedback=feedback
            )

            logger.info(f"用户{user_id}为消息{message_id}提交了反馈")

        except Exception as e:
            logger.error(f"提交反馈时出错: {e}")
            raise