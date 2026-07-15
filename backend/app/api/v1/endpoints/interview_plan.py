"""
面试方案持久化 API。

AI 生成方案的入口位于 ``hr_workflows`` 或 ``agent``，本模块只保存、分页查询、修改和
删除已经生成的方案。所有操作都把当前用户 ID 交给 ``InterviewPlanService``，由服务层
校验方案归属；删除目前是物理删除，而不是其他业务资源常用的软删除。
"""
from typing import Any, List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.schemas.user import User as UserSchema
from app.schemas.interview_plan import (
    InterviewPlanCreate,
    InterviewPlanUpdate,
    InterviewPlanResponse,
    InterviewPlanListResponse,
    InterviewPlanSaveRequest,
    InterviewPlanGenerateRequest
)
from app.services.interview_plan_service import InterviewPlanService

router = APIRouter()


@router.post("/save-generated", response_model=InterviewPlanResponse)
async def create_interview_plan(
    plan_data: InterviewPlanCreate,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """以认证用户为所有者保存一份已经生成的面试方案。

    请求 Schema 在端点前完成结构校验，服务层负责关联简历评价、构造 ORM、提交和回滚；
    已有 ``HTTPException`` 保持原状态码，其他异常统一包装为 500。
    """
    try:
        service = InterviewPlanService(db)
        interview_plan = await service.create_interview_plan(
            user_id=current_user.id,
            plan_data=plan_data
        )
        return interview_plan
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建面试方案失败: {str(e)}"
        )


@router.put("/{plan_id}", response_model=InterviewPlanResponse)
async def update_interview_plan(
    plan_id: UUID,
    plan_data: InterviewPlanUpdate,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """局部更新当前用户拥有的面试方案。

    ``plan_id`` 已由 FastAPI 转为 UUID，服务层把它与 ``current_user.id`` 联合查询并仅写入
    Schema 中显式提交的字段；权限/未命中异常保持原 HTTP 状态。
    """
    try:
        service = InterviewPlanService(db)
        interview_plan = await service.update_interview_plan(
            plan_id=plan_id,
            user_id=current_user.id,
            plan_data=plan_data
        )
        return interview_plan
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"更新面试方案失败: {str(e)}"
        )


@router.get("/{plan_id}", response_model=InterviewPlanResponse)
async def get_interview_plan(
    plan_id: UUID,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """读取当前用户拥有的一份面试方案。

    资源 ID 与用户 ID 一并交给服务层，避免只凭 UUID 读取他人方案；返回 ORM/Schema 由
    ``response_model`` 约束公开字段，预期 HTTP 异常不被通用 500 覆盖。
    """
    try:
        service = InterviewPlanService(db)
        interview_plan = await service.get_interview_plan(
            plan_id=plan_id,
            user_id=current_user.id
        )
        return interview_plan
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取面试方案失败: {str(e)}"
        )


@router.get("/", response_model=InterviewPlanListResponse)
async def list_interview_plans(
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(10, ge=1, le=100, description="每页数量"),
    resume_evaluation_id: Optional[UUID] = Query(None, description="简历评价ID"),
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """分页返回当前用户的面试方案，可选按简历评价 ID 收窄。

    页码和大小先由 Query 约束；服务层按用户过滤并计算总数，端点把内部字典显式映射为
    ``InterviewPlanListResponse``，固定列表分页契约。
    """
    try:
        service = InterviewPlanService(db)
        result = await service.list_interview_plans(
            user_id=current_user.id,
            page=page,
            size=size,
            resume_evaluation_id=resume_evaluation_id
        )
        
        return InterviewPlanListResponse(
            items=result["items"],
            total=result["total"],
            page=result["page"],
            size=result["size"],
            pages=result["pages"]
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取面试方案列表失败: {str(e)}"
        )

# todo：删除面试方案不是软删除，前端未实现
@router.delete("/{plan_id}")
async def delete_interview_plan(
    plan_id: UUID,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """物理删除当前用户拥有的一份面试方案。

    服务层以方案 ID 和用户 ID 校验归属后提交删除；该操作不同于 JD/评分标准的软删除，
    成功后记录不可通过状态过滤恢复。预期 HTTP 异常保持原状态码。
    """
    try:
        service = InterviewPlanService(db)
        result = await service.delete_interview_plan(
            plan_id=plan_id,
            user_id=current_user.id
        )
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除面试方案失败: {str(e)}"
        )
