"""
知识库管理 API。

读取接口返回当前规则下可访问的知识库，并额外统计每个知识库的文档数量；创建、更新和
删除交给 ``KnowledgeBaseEndpointService``。更新与删除通过管理员依赖提前鉴权，服务层
再负责 UUID 校验、资源存在性检查和数据库事务。
"""
from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.knowledge_base import KnowledgeBase as KnowledgeBaseSchema, KnowledgeBaseCreate, KnowledgeBaseUpdate
from app.schemas.user import User as UserSchema
from app.services.knowledge_base_service import KnowledgeBaseEndpointService
from app.api.deps import get_current_user, get_current_admin_by_role
import logging 

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_model=List[KnowledgeBaseSchema])
async def get_knowledge_bases(
    skip: int = 0,
    limit: int = 100,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """
    返回当前用户可见的知识库，并补齐页面需要的文档数量。

    执行顺序：先由依赖完成 JWT 认证，再查询可访问列表，随后逐个统计关联文档。
    单个知识库统计失败只记录日志，不影响其余列表项返回。
    """
    service = KnowledgeBaseEndpointService(db)

    try:
        # 第一步：服务层根据当前访问规则查询知识库，并应用分页参数。
        knowledge_bases = await service.get_accessible_knowledge_bases(
            user_id=str(current_user.id),
            skip=skip,
            limit=limit
        )

        # 第二步：列表页还需要实时文档数，因此对查询结果逐项补充统计字段。
        # get_knowledge_base_stats 会同步数据库中的 document_count，不只是只读查询。
        from app.services.knowledge_base_service import KnowledgeBaseService
        kb_service = KnowledgeBaseService(db)
        for kb in knowledge_bases:
            try:
                stats = await kb_service.get_knowledge_base_stats(kb.id)
                kb.document_count = stats.get("document_count", 0)
            except Exception as e:
                # 统计属于附加信息：局部失败时保留模型原值，主列表仍可正常展示。
                logger.warning(f"更新知识库 {kb.id} 文档数量时出错: {e}")

        # FastAPI 会依据 response_model 把 ORM 对象序列化为对外 Schema。
        return knowledge_bases
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取知识库列表错误: {str(e)}"
        )


@router.post("/", response_model=KnowledgeBaseSchema)
async def create_knowledge_base(
    kb_data: KnowledgeBaseCreate,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """
    创建一条知识库记录。

    ``KnowledgeBaseCreate`` 已在进入函数前完成字段校验；当前用户依赖用于限制匿名访问，
    实际 ORM 构造、提交和回滚由服务层负责。
    """
    service = KnowledgeBaseEndpointService(db)

    try:
        # 服务成功返回的是已提交并 refresh 后的 ORM 对象，包含数据库生成的 ID 和时间。
        knowledge_base = await service.create_knowledge_base(kb_data)
        return knowledge_base

    except Exception as e:
        # 当前接口把未分类的服务异常统一包装成创建失败响应。
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建知识库错误: {str(e)}"
        )



@router.put("/{kb_id}", response_model=KnowledgeBaseSchema)
async def update_knowledge_base(
    kb_id: str,
    kb_update: KnowledgeBaseUpdate,
    current_user: UserSchema = Depends(get_current_admin_by_role),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """
    由管理员更新知识库中显式提交的字段。

    ``get_current_admin_by_role`` 会在函数执行前完成管理员校验；服务层继续校验 UUID、
    资源是否存在，并使用 ``exclude_unset`` 保留请求中未提供的旧值。
    """
    service = KnowledgeBaseEndpointService(db)

    try:
        updated_kb = await service.update_knowledge_base_with_permission_check(
            kb_id=kb_id,
            kb_update=kb_update,
            current_user=current_user
        )
        return updated_kb

    except HTTPException:
        # 404、403 等预期业务状态不能被下面的通用 500 覆盖。
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"更新知识库错误: {str(e)}"
        )


@router.delete("/{kb_id}")
async def delete_knowledge_base(
    kb_id: str,
    current_user: UserSchema = Depends(get_current_admin_by_role),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """
    由管理员删除知识库及其关联数据。

    服务层会先解除文档对知识库的引用，再删除 FAQ 和知识库记录，并把这些修改放在同一
    数据库事务中；任一步失败都会回滚。
    """
    service = KnowledgeBaseEndpointService(db)

    try:
        result = await service.delete_knowledge_base_with_permission_check(
            kb_id=kb_id,
            current_user=current_user
        )
        return result

    except HTTPException:
        # 保留权限不足、ID 非法或资源不存在等精确错误。
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除知识库错误: {str(e)}"
        )
