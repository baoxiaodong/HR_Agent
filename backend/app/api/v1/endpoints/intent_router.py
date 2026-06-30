"""
意图路由端点：对用户查询进行分类并返回前端路由
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