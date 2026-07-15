"""
仪表盘统计 API。

所有接口先认证当前用户，再把用户 ID 和分页/时间范围交给 ``StatsService`` 聚合数据。
端点层不编写统计 SQL，只负责选择统计场景并将服务异常转换为 500 响应。
"""
from typing import Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.schemas.user import User as UserSchema
from app.services.stats_service import StatsService

router = APIRouter()


@router.get("/dashboard")
async def get_dashboard_stats(
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """聚合当前用户的 JD、简历、待面试和会话卡片统计。

    用户 ID 转为字符串后交给服务层；各子统计已在服务内部独立降级，因此通常可返回部分
    数据，只有组合流程自身异常才由端点映射为 500。
    """
    try:
        stats_service = StatsService(db)
        stats = await stats_service.get_dashboard_stats(str(current_user.id))
        return stats
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取统计数据失败: {str(e)}"
        )


@router.get("/recruitment-trend")
async def get_recruitment_trend(
    days: int = 30,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """返回当前用户在指定天数窗口内的远程 JD 趋势序列。

    ``days`` 当前没有 Query 上下界，由服务原样传给远程统计接口；远程失败在服务层降级为
    空 ``dates/counts``，端点只处理未被吸收的异常。
    """
    try:
        stats_service = StatsService(db)
        trend_data = await stats_service.get_recruitment_trend_data(str(current_user.id), days)
        return trend_data
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取招聘趋势数据失败: {str(e)}"
        )


@router.get("/training-completion")
async def get_training_completion_stats(
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """统计当前用户简历评价的高、中、低分占比。

    聚合 SQL 和空集合/查询失败的降级口径由服务层维护；端点只提供认证用户范围并返回固定
    三字段结构。
    """
    try:
        stats_service = StatsService(db)
        completion_stats = await stats_service.get_training_completion_stats(str(current_user.id))
        return completion_stats
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取简历评价分布统计失败: {str(e)}"
        )


@router.get("/recent-activities")
async def get_recent_activities(
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """合并当前用户的远程 JD、本地简历和会话活动后分页返回。

    ``limit``、``offset`` 由 Query 限制；服务层先归一化各来源、按时间排序，再对合并结果
    切片，单个来源失败不会清空其他活动。
    """
    try:
        stats_service = StatsService(db)
        activities = await stats_service.get_recent_activities(str(current_user.id), limit, offset)
        return activities
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取最近活动记录失败: {str(e)}"
        )