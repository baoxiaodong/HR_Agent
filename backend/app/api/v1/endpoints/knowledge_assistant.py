"""
知识助手（RAG 问答）API。

提问请求先验证会话归属，再由 ``KnowledgeAssistantService`` 检索知识库并流式生成答案；
生成器把开始、文本片段、结束或错误事件编码为 SSE，完整生成后再把用户问题和助手答案
写入会话。知识库自动选择由独立的 ``KBSelectionService`` 完成，避免端点内混入排序逻辑。
"""
from typing import Any, List, Dict
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
import json
import logging

logger = logging.getLogger(__name__)

from app.core.database import get_db
from app.schemas.document import Document as DocumentSchema
from app.schemas.user import User as UserSchema
from app.services.knowledge_assistant_service import KnowledgeAssistantService
from app.services.conversation_service import ConversationService
from app.models.conversation import MessageRole
from app.api.deps import get_current_user
from app.services.kb_selection_service import KBSelectionService

router = APIRouter()


@router.get("/config")
async def get_knowledge_assistant_config(
    db: AsyncSession = Depends(get_db)
):
    """返回知识助手允许前端使用的公开运行限制。

    当前仅读取进程配置中的上下文条数，不需要登录，也不访问用户数据或暴露模型凭据。
    """
    service = KnowledgeAssistantService(db)
    return await service.get_config()

@router.post("/ask")
async def ask_knowledge_assistant(
    question: str = Form(...),
    knowledge_base_id: str = Form(None),
    context_limit: int = Form(5),  # 应该匹配config.py中的settings.CONTEXT_LIMIT
    conversation_history: str = Form("[]"),  # 对话历史的JSON字符串
    conversation_id: str = Form(...),
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """校验会话归属后执行 RAG 问答，并把内部事件编码为 SSE。

    历史 JSON 解析失败会降级为空列表；会话 ID 必须合法且属于当前用户。生成期间累计助手文本
    与来源，流正常结束后分别保存用户和助手消息；响应开始后的错误只能作为 error 事件输出。
    """
    service = KnowledgeAssistantService(db)
    conv_service = ConversationService(db)
    
    try:
        # 解析对话历史
        try:
            conv_history = json.loads(conversation_history) if conversation_history else []
        except json.JSONDecodeError:
            conv_history = []
        
        # Validate conversation id (must exist and belong to current user)
        from uuid import UUID
        try:
            conv_uuid = UUID(conversation_id)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid conversation_id")

        conversation = await conv_service.get_conversation(conv_uuid, current_user.id)
        if not conversation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found or no permission")
        conv_id = str(conversation.id)
        
        # 生成流式响应
        async def generate_stream():
            try:
                assistant_buffer = ""
                start_sources = []
                async for chunk in service.ask_question_stream(
                    question=question,
                    user_id=current_user.id,
                    knowledge_base_id=knowledge_base_id,
                    context_limit=context_limit,
                    conversation_history=conv_history
                ):
                    if chunk.get("type") == "start":
                        start_sources = chunk.get("sources") or []
                        payload = {**chunk, "conversation_id": conv_id}
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    elif chunk.get("type") == "chunk":
                        assistant_buffer += chunk.get("content", "")
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    elif chunk.get("type") == "end":
                        # Persist messages to conversation
                        payload = {**chunk, "conversation_id": conv_id}
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    else:
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                try:
                    from uuid import UUID
                    await conv_service.add_message(
                        conversation_id=UUID(conv_id),
                        content=question,
                        role=MessageRole.USER,
                        context={"knowledge_base_id": str(knowledge_base_id) if knowledge_base_id else None}
                    )
                    await conv_service.add_message(
                        conversation_id=UUID(conv_id),
                        content=assistant_buffer,
                        role=MessageRole.ASSISTANT,
                        context={"sources": start_sources}
                    )
            
                except GeneratorExit:
                    logger.info("客户端断开连接，停止生成流式响应")
                    return
            except Exception as e:
                logger.error(f"流式响应生成错误: {str(e)}")
                error_data = {
                    "type": "error",
                    "error": str(e)
                }
                yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"
        
        return StreamingResponse(
            generate_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "Cache-Control"
            }
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"生成答案错误: {str(e)}"
        )


@router.get("/documents")
async def get_knowledge_documents(
    knowledge_base_id: str = None,
    skip: int = 0,
    limit: int = 20,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """分页返回当前用户文档，可选限制到指定知识库。

    用户 ID 是固定查询边界；知识库 ID 在服务层转为 UUID，非法值原本会形成 400，但当前端点
    的通用异常包装可能把它转换为 500。返回 ``total`` 是本页数量而非数据库总数。
    """
    service = KnowledgeAssistantService(db)
    
    try:
        result = await service.get_documents(
            user_id=current_user.id,
            knowledge_base_id=knowledge_base_id,
            skip=skip,
            limit=limit
        )
        return result
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取文档列表错误: {str(e)}"
        )

@router.post("/auto-select-kb")
async def auto_select_kb(
    question: str = Form(...),
    max_candidates: int = Form(100),
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """从当前用户的有限候选文档中选择最相关知识库。

    LLM 只推荐文档 ID，服务层会回到用户范围内候选集合验证并映射知识库 ID；无候选、无效
    模型输出或未命中时返回字段稳定的空选择，而不是任意猜测资源。
    """
    try:
        kb_selector = KBSelectionService(db)
        result = await kb_selector.select_kb_for_question(
            question=question,
            user_id=current_user.id,
            max_candidates=max_candidates,
        )
        return result
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"选择知识库时出错: {str(e)}"
        )