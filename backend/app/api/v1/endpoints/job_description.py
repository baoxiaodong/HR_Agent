"""
职位描述（JD）管理 API。

本模块管理已经生成并保存的 JD，不负责调用大模型生成文本；生成流程位于
``hr_workflows`` 端点。每次读写都会把当前用户 ID 传入 ``JobDescriptionService``，
由服务层同时完成资源归属检查、分页查询和软删除。
"""
import logging
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.schemas.user import User as UserSchema
from app.schemas.job_description import (
    JobDescriptionCreate,
    JobDescriptionUpdate,
    JobDescriptionResponse,
    JobDescriptionListResponse
)
from app.services.job_description_service import JobDescriptionService

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/save", response_model=JobDescriptionResponse)
async def save_job_description(
    jd_data: JobDescriptionCreate,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """把已生成的 JD Schema 连同认证用户 ID 交给远程领域服务保存。

    端点不再解析模型文本；服务层负责请求体适配和远程响应规范化。当前通用异常统一返回 500。
    """
    try:
        service = JobDescriptionService(db)
        jd = await service.create_job_description(jd_data, current_user.id)
        return jd
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.put("/{jd_id}", response_model=JobDescriptionResponse)
async def update_job_description(
    jd_id: str,
    jd_data: JobDescriptionUpdate,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """更新当前用户范围内已保存 JD 的显式字段。

    JD ID、局部更新 Schema 和用户 ID 一并交给远程服务；远程资源未找到或归属不匹配使用
    ``ValueError`` 表达并映射为 404，其他调用失败返回 500。
    """
    try:
        service = JobDescriptionService(db)
        jd = await service.update_job_description(jd_id, jd_data, current_user.id)
        return jd
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.get("/{jd_id}", response_model=JobDescriptionResponse)
async def get_job_description(
    jd_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """在认证用户上下文中读取一条远程 JD。

    用户 ID 作为远程查询参数参与资源隔离；未命中转换为 404，成功响应再由
    ``JobDescriptionResponse`` 约束字段形状。
    """
    try:
        service = JobDescriptionService(db)
        jd = await service.get_job_description(jd_id, current_user.id)
        return jd
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.get("/", response_model=JobDescriptionListResponse)
async def list_job_descriptions(
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(10, ge=1, le=100, description="每页数量"),
    status_filter: Optional[str] = Query(None, description="状态筛选"),
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """分页列出认证用户的远程 JD，可选按状态筛选。

    Query 先限制页码和大小，服务层继续传递用户范围并规范化远程分页结果；端点使用列表
    Schema 再次固定 ``items/total/page/size/pages`` 契约。
    """
    try:
        service = JobDescriptionService(db)
        result = await service.list_job_descriptions(
            user_id=current_user.id,
            page=page,
            size=size,
            status_filter=status_filter
        )
        
        return JobDescriptionListResponse(**result)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.delete("/{jd_id}")
async def delete_job_description(
    jd_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """在认证用户范围内软删除一条远程 JD。

    服务层通过远程接口推进删除状态而不是移除本地记录；未命中或归属不匹配映射为 404，
    其他远程错误返回 500。
    """
    try:
        service = JobDescriptionService(db)
        result = await service.delete_job_description(jd_id, current_user.id)
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )