"""
HR Agent 的聊天 API 端点。

普通请求依次完成身份认证、获取或创建会话、调用 ``ChatService`` 处理消息，最后
返回模型响应。流式接口复用同一业务服务，只是通过异步生成器逐块输出 SSE 数据，
因此 HTTP 传输方式与 AI 处理逻辑彼此分离。
"""
from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.chat import ChatResponse, ChatRequest
from app.schemas.user import User as UserSchema
from app.schemas.conversation import ConversationCreate
from app.services.chat_service import ChatService
from app.services.conversation_service import ConversationService
from app.api.deps import get_current_user


router = APIRouter()


@router.post("/send", response_model=ChatResponse)
async def send_message(
    chat_request: ChatRequest,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """完成认证会话恢复/创建，再执行非流式聊天链。

    ``get_or_create_conversation`` 用认证用户限制已有会话归属；随后服务层分别保存用户消息和
    助手消息，并可按请求开关检索该用户文档。HTTPException 保留原状态，其他错误统一为 500。
    """
    chat_service = ChatService(db)
    
    try:
        # 先得到已校验所有权的 ORM 会话，后续底层消息方法本身不再接收 user_id。
        conversation = await chat_service.get_or_create_conversation(
            chat_request, current_user
        )
        
        response = await chat_service.process_message(
            user_id=current_user.id,
            conversation_id=conversation.id,
            message=chat_request.message,
            context=chat_request.context
        )
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        raise chat_service.handle_chat_error(e, "处理消息")


@router.post("/stream")
async def stream_message(
    chat_request: ChatRequest,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """校验会话后，把聊天服务的 JSON 字符串块包装成 SSE data 帧。

    流式服务会先提交用户消息，正常结束后才保存完整助手消息；远程异常以服务生成的 error
    JSON 继续输出。响应使用 ``text/plain``，但正文格式遵循 ``data: ...`` 的 SSE 风格。
    """
    chat_service = ChatService(db)
    
    try:
        conversation = await chat_service.get_or_create_conversation(
            chat_request, current_user
        )
        
        async def generate_response():
            async for chunk in chat_service.stream_message(
                user_id=current_user.id,
                conversation_id=conversation.id,
                message=chat_request.message,
                context=chat_request.context
            ):
                yield f"data: {chunk}\n\n"
        
        return StreamingResponse(
            generate_response(),
            media_type="text/plain",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise chat_service.handle_chat_error(e, "处理流式消息")


@router.get("/suggestions")
async def get_suggestions(
    query: str = "",
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> List[str]:
    """为认证用户生成最多五条后续问题建议。

    服务层可结合该用户最近文档摘要构造上下文，再调用 LLM；建议解析或模型异常由服务层
    降级为空列表，端点保持稳定的字符串数组响应。
    """
    chat_service = ChatService(db)
    
    try:
        suggestions = await chat_service.get_suggestions(
            query=query, user_id=current_user.id
        )
        return suggestions
        
    except Exception as e:
        raise chat_service.handle_chat_error(e, "获取建议")


@router.post("/feedback")
async def submit_feedback(
    message_id: str,
    rating: int,
    feedback: str = "",
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """为当前用户拥有的一条聊天消息提交评分和文字反馈。

    服务层把 ``message_id`` 规范化后校验消息所属会话及用户归属，再更新反馈字段；请求中的
    用户 ID 不可覆盖认证上下文，失败通过聊天服务统一转换为 HTTP 错误。
    """
    chat_service = ChatService(db)
    
    try:
        await chat_service.submit_feedback(
            message_id=message_id,
            user_id=current_user.id,
            rating=rating,
            feedback=feedback
        )
        
        return {"message": "反馈提交成功"}
        
    except Exception as e:
        raise chat_service.handle_chat_error(e, "提交反馈")
