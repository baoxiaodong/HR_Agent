"""
文档管理 API 端点。

请求先经过 ``get_current_user`` 完成身份校验，再按操作类型交给两个服务：
``LightweightDocumentService`` 负责列表和详情等轻量查询，``EnhancedDocumentService``
负责上传、文本提取、分块和向量化等重处理。端点层不直接操作文件或数据库模型。
"""
from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.document import Document as DocumentSchema, DocumentCreate
from app.schemas.user import User as UserSchema
from app.api.deps import get_current_user
from app.services.enhanced_document_service import EnhancedDocumentService
from app.services.lightweight_document_service import LightweightDocumentService

router = APIRouter()


@router.get("/", response_model=List[DocumentSchema])
async def get_documents(
    skip: int = 0,
    limit: int = 100,
    category: str = None,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """分页读取认证用户自己的文档，可选按分类收窄。

    轻量服务只执行带 ``user_id`` 的 SQL 查询，不初始化 LLM、提取器或向量客户端；返回结果
    按创建时间倒序，不产生写副作用。
    """
    document_service = LightweightDocumentService(db)
    documents = await document_service.get_user_documents(
        user_id=current_user.id,
        skip=skip,
        limit=limit,
        category=category
    )
    return documents


@router.post("/upload", response_model=DocumentSchema)
async def upload_document(
    file: UploadFile = File(...),
    category: str = Form(None),
    tags: List[str] = Form(None),
    knowledge_base_id: str = Form(None),
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """把 multipart 文件和元数据交给增强服务完成上传全链路。

    认证用户 ID 决定文件目录和记录归属；服务层负责读取、哈希去重、类型识别、文本提取、
    数据库记录及后续处理。文件系统、数据库和向量写入不是同一原子事务。
    """
    document_service = EnhancedDocumentService(db)
    
    try:
        document = await document_service.upload_document(
            file=file,
            user_id=current_user.id,
            category=category,
            tags=tags or [],
            knowledge_base_id=knowledge_base_id
        )
        return document
        
    except Exception as e:
        await document_service.handle_document_service_error(e, "上传文档")


@router.post("/{document_id}/process", response_model=DocumentSchema)
async def process_document(
    document_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """对已有文档执行正文提取、分块和向量化处理。

    路径 ID 在端点转为 UUID，但当前调用没有传入认证用户 ID，也未先执行文档权限检查；
    处理权限取决于 ``EnhancedDocumentService.process_document`` 的内部实现。
    """
    document_service = EnhancedDocumentService(db)

    try:
        from uuid import UUID
        document = await document_service.process_document(UUID(document_id))
        return document

    except Exception as e:
        await document_service.handle_document_service_error(e, "处理文档")


@router.get("/{document_id}", response_model=DocumentSchema)
async def get_document(
    document_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """读取一条文档并校验数据库所有者或超级管理员权限。

    轻量服务把不存在与无权访问分别映射为 404、403，不初始化任何模型或向量组件。
    """
    document_service = LightweightDocumentService(db)
    document = await document_service.get_document_with_permission_check(document_id, current_user)
    return document


@router.get("/{document_id}/chunks")
async def get_document_chunks(
    document_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """返回当前用户拥有文档的检索分块，用于前端预览。

    文档 ID 和认证用户 ID 一并交给增强服务，服务层负责所有权校验并从向量存储读取块；
    端点把列表包装为稳定的 ``chunks`` 字段。
    """
    document_service = EnhancedDocumentService(db)
    
    try:
        chunks = await document_service.get_document_chunks(document_id, current_user.id)
        return {"chunks": chunks}
        
    except Exception as e:
        await document_service.handle_document_service_error(e, "获取文档分块")


@router.delete("/{document_id}")
async def delete_document(
    document_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """校验文档所有权后执行完整删除链。

    增强服务依次清理磁盘文件、向量记录和文档 ORM 数据；数据库变更可回滚，但文件系统删除
    不可由事务撤销，因此异常路径可能形成部分删除状态。
    """
    document_service = EnhancedDocumentService(db)
    document = await document_service.get_document_with_permission_check(document_id, current_user)
    
    await document_service.delete(document)
    return {"message": "文档删除成功"}
