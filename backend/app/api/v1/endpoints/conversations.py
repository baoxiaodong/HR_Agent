"""
对话与消息管理 API 端点。

本模块负责会话所有权校验、分页参数接收以及响应结构转换；数据库查询和事务由
``ConversationService`` 完成。部分接口显式把 SQLAlchemy 对象转换为字典，是为了在
异步会话结束后避免懒加载属性引发 ``DetachedInstanceError`` 或序列化错误。
"""
from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.conversation import (
    Conversation as ConversationSchema,
    ConversationCreate,
    MessageCreate,
    MessageUpdate,
    ConversationUpdate
)
from app.schemas.user import User as UserSchema
from app.services.conversation_service import ConversationService
from app.api.deps import get_current_user


router = APIRouter()


@router.get("/", response_model=List[ConversationSchema])
async def get_conversations(
    skip: int = 0,
    limit: int = 100,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """分页读取认证用户自己的会话，并映射为响应 Schema 字段。

    用户 ID 是服务查询的固定过滤条件；端点显式把 ORM 的 ``total_messages`` 重命名为
    ``message_count``，同时避免异步会话结束后的懒加载序列化问题。
    """
    conversation_service = ConversationService(db)
    
    try:
        conversations = await conversation_service.get_user_conversations(
            user_id=current_user.id,
            skip=skip,
            limit=limit
        )
        # Convert conversations to dict to avoid DetachedInstanceError
        result = []
        for conv in conversations:
            result.append({
                "id": conv.id,
                "user_id": conv.user_id,
                "title": conv.title,
                "description": conv.description,
                "status": conv.status,
                "message_count": conv.total_messages,  # Map total_messages to message_count for schema
                "meta_data": conv.meta_data,
                "created_at": conv.created_at,
                "updated_at": conv.updated_at
            })
        
        return result
    except Exception as e:
        raise conversation_service.handle_conversation_error(e, "获取对话列表")


@router.post("/", response_model=ConversationSchema)
async def create_conversation(
    conversation_data: ConversationCreate,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """以认证用户为所有者创建活动会话并返回稳定字典。

    客户端 Schema 不控制 ``user_id``；服务层提交并刷新 ORM 后，端点显式映射消息计数和
    时间字段，避免直接序列化已脱离会话的模型。
    """
    conversation_service = ConversationService(db)
    
    try:
        conversation = await conversation_service.create_conversation(
            user_id=current_user.id,
            conversation_data=conversation_data
        )
        # Convert to dict to avoid DetachedInstanceError
        return {
            "id": conversation.id,
            "user_id": conversation.user_id,
            "title": conversation.title,
            "description": conversation.description,
            "status": conversation.status,
            "message_count": conversation.total_messages,
            "meta_data": conversation.meta_data,
            "created_at": conversation.created_at,
            "updated_at": conversation.updated_at
        }

    except Exception as e:
        raise conversation_service.handle_conversation_error(e, "创建对话")


@router.get("/{conversation_id}", response_model=ConversationSchema)
async def get_conversation(
    conversation_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """读取一条会话并区分资源不存在与所有权不足。

    服务层先按 ID 查询，再把数据库 ``user_id`` 与认证用户比较：未命中返回 404，越权返回
    403。成功后端点显式映射 ORM 字段供响应 Schema 序列化。
    """
    conversation_service = ConversationService(db)
    
    try:
        conversation = await conversation_service.get_conversation_with_permission_check(
            conversation_id, current_user
        )
        # Convert to dict to avoid DetachedInstanceError
        return {
            "id": conversation.id,
            "user_id": conversation.user_id,
            "title": conversation.title,
            "description": conversation.description,
            "status": conversation.status,
            "message_count": conversation.total_messages,
            "meta_data": conversation.meta_data,
            "created_at": conversation.created_at,
            "updated_at": conversation.updated_at
        }
    except HTTPException:
        raise
    except Exception as e:
        raise conversation_service.handle_conversation_error(e, "获取对话")


@router.put("/{conversation_id}", response_model=ConversationSchema)
async def update_conversation(
    conversation_id: str,
    conversation_update: ConversationUpdate,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """校验会话所有权后局部更新认证用户的会话。

    端点先取得明确的 403/404，再由服务以会话 ID 和用户 ID 联合过滤并提交 ``exclude_unset``
    字段；预期 HTTP 状态不会被通用错误包装覆盖。
    """
    conversation_service = ConversationService(db)

    try:
        # 权限检查
        await conversation_service.get_conversation_with_permission_check(
            conversation_id, current_user
        )
        
        updated_conversation = await conversation_service.update_conversation(
            conversation_id, current_user.id, conversation_update
        )
        return updated_conversation
    except HTTPException:
        raise
    except Exception as e:
        raise conversation_service.handle_conversation_error(e, "更新对话")


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """校验所有权后，在一个本地事务中删除会话及其全部消息。

    服务层先删除子表消息再删除父会话并一次提交；任一步失败会回滚。端点保留权限错误，
    未产生删除时返回 400。
    """
    conversation_service = ConversationService(db)
    
    try:
        # 权限检查
        await conversation_service.get_conversation_with_permission_check(
            conversation_id, current_user
        )
        
        success = await conversation_service.delete_conversation(
            conversation_id, current_user.id
        )
        if not success:
            raise HTTPException(
                status_code=400,
                detail="删除对话失败"
            )
        return {"message": "对话删除成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise conversation_service.handle_conversation_error(e, "删除对话")


@router.get("/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: str,
    skip: int = 0,
    limit: int = 100,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """校验父会话所有权后，按时间正序分页返回消息。

    消息查询本身只接收会话 ID，不具备用户隔离，因此前置会话权限检查不可省略；ORM 消息
    随后转换为普通字典，固定上下文、反馈和父消息字段。
    """
    conversation_service = ConversationService(db)
    
    try:
        # 权限检查
        await conversation_service.get_conversation_with_permission_check(
            conversation_id, current_user
        )
        
        messages = await conversation_service.get_conversation_messages(
            conversation_id=conversation_id,
            skip=skip,
            limit=limit
        )
        # Convert messages to dict to avoid PydanticSerializationError
        result = []
        for message in messages:
            result.append({
                "id": message.id,
                "conversation_id": message.conversation_id,
                "content": message.content,
                "role": message.role,
                "model_name": message.model_name,
                "context": message.context,
                "meta_data": message.meta_data,
                "rating": message.rating,
                "feedback": message.feedback,
                "parent_message_id": message.parent_message_id,
                "created_at": message.created_at,
                "updated_at": message.updated_at
            })
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise conversation_service.handle_conversation_error(e, "获取对话消息")


@router.post("/{conversation_id}/messages")
async def add_conversation_message(
    conversation_id: str,
    message_data: MessageCreate,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """校验父会话归属后追加消息，并同步会话计数与活动时间。

    消息插入和父会话更新由服务层一次提交；服务方法本身不接收用户 ID，因此端点的前置权限
    检查是写入边界。返回值显式转成字典以避免异步 ORM 序列化问题。
    """
    conversation_service = ConversationService(db)

    try:
        await conversation_service.get_conversation_with_permission_check(
            conversation_id, current_user
        )

        message = await conversation_service.add_message(
            conversation_id=conversation_id,
            content=message_data.content,
            role=message_data.role,
            model_name=message_data.model_name,
            context=message_data.context,
            parent_id=message_data.parent_id
        )
        return {
            "id": message.id,
            "conversation_id": message.conversation_id,
            "content": message.content,
            "role": message.role,
            "model_name": message.model_name,
            "context": message.context,
            "meta_data": message.meta_data,
            "created_at": message.created_at,
            "updated_at": message.updated_at
        }
    except HTTPException:
        raise
    except Exception as e:
        raise conversation_service.handle_conversation_error(e, "保存对话消息")


@router.put("/{conversation_id}/messages/{message_id}")
async def update_conversation_message(
    conversation_id: str,
    message_id: str,
    message_update: MessageUpdate,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """校验父会话归属后局部更新其中一条消息。

    服务层继续确认消息属于该会话，把 Schema 的 ``user_feedback`` 适配为 ORM ``feedback``，
    并与会话活动时间一次提交；未命中返回 404。
    """
    conversation_service = ConversationService(db)

    try:
        await conversation_service.get_conversation_with_permission_check(
            conversation_id, current_user
        )

        message = await conversation_service.update_message(
            conversation_id=conversation_id,
            message_id=message_id,
            message_update=message_update
        )
        if not message:
            raise HTTPException(status_code=404, detail="消息未找到")
        return {
            "id": message.id,
            "conversation_id": message.conversation_id,
            "content": message.content,
            "role": message.role,
            "model_name": message.model_name,
            "context": message.context,
            "meta_data": message.meta_data,
            "created_at": message.created_at,
            "updated_at": message.updated_at
        }
    except HTTPException:
        raise
    except Exception as e:
        raise conversation_service.handle_conversation_error(e, "更新对话消息")


@router.delete("/{conversation_id}/messages/{message_id}")
async def delete_conversation_message(
    conversation_id: str,
    message_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """校验父会话归属后删除其中一条消息。

    服务层核对消息与会话关联，在同一事务中删除消息、递减且不低于零的计数并刷新活动时间；
    消息不属于该会话时返回 404。
    """
    conversation_service = ConversationService(db)

    try:
        await conversation_service.get_conversation_with_permission_check(
            conversation_id, current_user
        )

        success = await conversation_service.delete_message(
            conversation_id=conversation_id,
            message_id=message_id
        )
        if not success:
            raise HTTPException(status_code=404, detail="消息未找到")
        return {"message": "消息删除成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise conversation_service.handle_conversation_error(e, "删除对话消息")
