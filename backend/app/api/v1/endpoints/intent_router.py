"""
前端导航意图路由 API。

端点只从请求体提取并清理 ``query``，实际的意图识别、目标页面选择和结果格式化均交给
``IntentService``。当前用户 ID 同步传入服务，为后续个性化或权限过滤保留上下文。
"""
from typing import Any, Dict
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.schemas.user import User as UserSchema
from app.services.intent_service import IntentService

router = APIRouter()


@router.post("/route")
async def route_by_intent(
    payload: Dict[str, Any],
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """
    对用户查询进行分类并返回前端路由和意图。
    请求体: { "query": "用户输入内容" }
    """
    query = (payload or {}).get("query", "").strip()
    
    # 使用IntentService处理意图分类和路由
    intent_service = IntentService(db)
    result = await intent_service.route_query(query, current_user.id)
    
    return result