"""
评分标准管理 API。

端点接收 Pydantic Schema 校验后的数据，把当前用户 ID 一并交给
``ScoringCriteriaService``，由服务层限定数据归属并执行持久化。接口只负责编排分页、
筛选参数及异常到状态码的转换；删除采用服务层定义的软删除，不直接移除数据库记录。
"""
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.schemas.user import User as UserSchema
from app.schemas.scoring_criteria import (
    ScoringCriteriaCreate,
    ScoringCriteriaUpdate,
    ScoringCriteriaResponse,
    ScoringCriteriaListResponse
)
from app.services.scoring_criteria_service import ScoringCriteriaService
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/save", response_model=ScoringCriteriaResponse)
async def save_scoring_criteria(
    criteria_data: ScoringCriteriaCreate,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """以认证用户为归属保存一条已生成评分标准。

    端点只接收结构化 Schema；服务层负责远程请求适配、保存响应补全与对外 Schema 转换。
    未分类的远程或校验异常统一包装为 500。
    """
    try:
        service = ScoringCriteriaService(db)
        result = await service.save_scoring_criteria(criteria_data, current_user.id)
        return result
    except Exception as e:
        logger.error(f"保存评分标准失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"保存评分标准失败: {str(e)}"
        )


@router.put("/{criteria_id}", response_model=ScoringCriteriaResponse)
async def update_scoring_criteria(
    criteria_id: str,
    criteria_data: ScoringCriteriaUpdate,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """局部更新认证用户范围内的一条评分标准。

    标准 ID、更新 Schema 和用户 ID 一并传给远程服务；未命中或归属不匹配映射为 404，
    其他远程、结构或网络错误返回 500。
    """
    try:
        service = ScoringCriteriaService(db)
        result = await service.update_scoring_criteria(criteria_id, criteria_data, current_user.id)
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"更新评分标准失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"更新评分标准失败: {str(e)}"
        )


@router.get("/{criteria_id}", response_model=ScoringCriteriaResponse)
async def get_scoring_criteria(
    criteria_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """在认证用户上下文中读取一条评分标准详情。

    服务层把用户 ID 作为远程资源隔离条件，并将返回数据规范化为响应 Schema；未命中映射
    为 404，其他失败统一包装为 500。
    """
    try:
        service = ScoringCriteriaService(db)
        result = await service.get_scoring_criteria(criteria_id, current_user.id)
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"获取评分标准失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取评分标准失败: {str(e)}"
        )


@router.get("/", response_model=ScoringCriteriaListResponse)
async def get_scoring_criteria_list(
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(10, ge=1, le=100, description="每页数量"),
    job_description_id: Optional[str] = Query(None, description="关联的JD ID"),
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """分页列出认证用户的评分标准，可选按关联 JD 收窄。

    Query 先约束分页范围，服务层继续携带用户 ID 和 JD 过滤器访问远程接口，并返回稳定的
    列表分页结构。
    """
    try:
        service = ScoringCriteriaService(db)
        result = await service.get_scoring_criteria_list(
            user_id=current_user.id,
            page=page,
            size=size,
            job_description_id=job_description_id
        )
        return result
    except Exception as e:
        logger.error(f"获取评分标准列表失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取评分标准列表失败: {str(e)}"
        )


@router.delete("/{criteria_id}")
async def delete_scoring_criteria(
    criteria_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """在认证用户范围内软删除一条评分标准。

    服务层通过远程接口推进删除状态而不物理移除记录；未命中或归属不匹配映射为 404，
    其他远程错误统一返回 500。
    """
    try:
        service = ScoringCriteriaService(db)
        result = await service.delete_scoring_criteria(criteria_id, current_user.id)
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"删除评分标准失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除评分标准失败: {str(e)}"
        )