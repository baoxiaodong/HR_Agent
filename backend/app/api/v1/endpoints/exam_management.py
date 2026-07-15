"""
试卷和考试结果管理 API。

本模块处理已生成试卷的查询、保存、修改、删除以及答卷结果查询；具体 SQL、试卷结构转换
和评分结果持久化由 ``ExamService`` 完成。分享页接口和答卷提交接口不依赖登录用户，
其余管理接口会先执行 ``get_current_user``。
"""
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.core.database import get_db
from app.schemas.user import User as UserSchema
# from app.models.exam import Exam, Question  # 注释掉未使用的导入
# from app.models.exam_result import ExamResult  # 注释掉未使用的导入
from app.api.deps import get_current_user
from app.core.logging import logger
from app.services.exam_service import ExamService
from app.schemas.exam import ExamGenerateRequest, ExamSubmitRequest, ExamCreateRequest

router = APIRouter()


# 获取试卷列表
@router.get("/papers")
async def get_exam_list(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """分页搜索试卷列表。

    查询参数已由 FastAPI 限制范围；虽然端点要求登录，但当前服务调用未传 ``current_user.id``，
    因此数据是否按用户隔离完全取决于 ``ExamService.get_exam_list`` 的内部实现。
    """
    try:
        exam_service = ExamService(db)
        result = await exam_service.get_exam_list(skip, limit, search)
        return result
    except Exception as e:
        logger.error(f"Error getting exam list: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取试卷列表失败: {str(e)}"
        )


# 保存试卷
@router.post("/papers")
async def save_exam(
    exam_data: ExamCreateRequest,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """把已校验试卷 Schema 转为字典，并以当前用户作为创建者持久化。

    服务层负责题目结构转换和数据库事务；端点把所有未分类异常统一包装为 500。
    """
    try:
        exam_service = ExamService(db)
        result = await exam_service.save_exam(exam_data.dict(), current_user.id)
        return result
    except Exception as e:
        logger.error(f"Error saving exam to database: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"保存试卷失败: {str(e)}"
        )


# 获取试卷详情
@router.get("/papers/{paper_id}")
async def get_exam_detail(
    paper_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """按试卷 ID 返回管理端详情。

    端点要求登录，但当前调用只传 ``paper_id``，未把认证用户 ID 继续交给服务；资源归属隔离
    取决于服务方法。服务抛出的 ``ValueError`` 被映射为 404。
    """
    try:
        exam_service = ExamService(db)
        result = await exam_service.get_exam_detail(paper_id)
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error getting exam detail: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取试卷详情失败: {str(e)}"
        )


# 更新试卷
@router.put("/papers/{paper_id}")
async def update_exam(
    paper_id: str,
    exam_data: ExamCreateRequest,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """用完整试卷 Schema 替换指定试卷内容。

    当前用户 ID 随请求数据进入服务层，用于资源归属校验或更新审计；服务返回不存在时通过
    ``ValueError`` 转换为 404，其他事务或结构错误统一返回 500。
    """
    try:
        exam_service = ExamService(db)
        result = await exam_service.update_exam(paper_id, exam_data.dict(), current_user.id)
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error updating exam: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"更新试卷失败: {str(e)}"
        )


# 删除试卷
@router.delete("/papers/{paper_id}")
async def delete_exam(
    paper_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """按试卷 ID 执行删除。

    端点要求登录，但当前调用没有传入认证用户 ID，删除权限边界只能由试卷 ID 和服务内部
    逻辑决定；未命中映射为 404，其他异常映射为 500。
    """
    try:
        exam_service = ExamService(db)
        result = await exam_service.delete_exam(paper_id)
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error deleting exam: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除试卷失败: {str(e)}"
        )


# 获取单个试卷（用于分享页面）
@router.get("/papers/{paper_id}/share")
async def get_exam_for_share(
    paper_id: str,
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """返回分享页所需的公开试卷内容。

    该路由不注入认证用户，任何持有 ``paper_id`` 的请求方都可调用；服务层应只返回答题所需
    字段并避免泄露答案或内部审计信息。未命中转换为 404。
    """
    try:
        exam_service = ExamService(db)
        result = await exam_service.get_exam_for_share(paper_id)
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error getting exam: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取试卷失败: {str(e)}"
        )


# 获取考试结果列表
@router.get("/exam-results")
async def get_exam_results(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    search: str = Query(None, description="搜索关键词（学生姓名或考试名称）"),
    exam_id: Optional[UUID] = Query(None, description="考试ID筛选"),
    department: str = Query(None, description="部门筛选"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """按页码及可选考试、学生/试卷关键词和部门筛选答卷结果。

    UUID 筛选器在进入服务前转成字符串。端点虽然要求登录，但当前服务调用未传用户 ID，
    结果集是否隔离到当前用户取决于 ``ExamService`` 的查询实现。
    """
    try:
        exam_service = ExamService(db)
        # 转换UUID为字符串
        exam_id_str = str(exam_id) if exam_id else None
        result = await exam_service.get_exam_results(page, page_size, search, exam_id_str, department)
        return result
    except Exception as e:
        logger.error(f"Error getting exam results: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取考试结果失败: {str(e)}"
        )


# 导出考试结果为CSV
@router.get("/exam-results/{result_id}/export")
async def export_exam_result(
    result_id: str,
    db: AsyncSession = Depends(get_db)
):
    """把指定答卷结果转换为带 UTF-8 BOM 的 CSV 下载响应。

    该路由不要求认证，只按 ``result_id`` 查询；服务返回 Unicode 文本后，端点使用
    ``utf-8-sig`` 编码以兼容 Excel，并设置附件文件名。未命中映射为 404。
    """
    try:
        exam_service = ExamService(db)
        csv_content = await exam_service.export_exam_result_to_csv(result_id)

        # 创建响应头，指定为CSV文件下载
        from fastapi import Response

        headers = {
            "Content-Disposition": f"attachment; filename=exam_result_{result_id}.csv",
            "Content-Type": "text/csv; charset=utf-8"
        }

        # 返回响应，使用 utf-8-sig 编码确保Excel正确显示中文
        return Response(
            content=csv_content.encode('utf-8-sig'),
            headers=headers
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error exporting exam result: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"导出考试结果失败: {str(e)}"
        )


# 获取考试结果
@router.get("/exam-results/{result_id}")
async def get_exam_result(
    result_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """按结果 ID 返回单份答卷和评分详情。

    端点要求登录，但当前服务调用未传认证用户 ID；资源归属边界由服务查询决定。未命中通过
    ``ValueError`` 映射为 404，其他异常统一返回 500。
    """
    try:
        exam_service = ExamService(db)
        result = await exam_service.get_exam_result(result_id)
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error getting exam result: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取考试结果失败: {str(e)}"
        )


# 导出考试结果为CSV
@router.get("/exam-results/{result_id}/export")
async def export_exam_result(
    result_id: str,
    db: AsyncSession = Depends(get_db)
):
    """把指定答卷结果转换为带 UTF-8 BOM 的 CSV 下载响应。

    该路由不要求认证，只按 ``result_id`` 查询；服务返回 Unicode 文本后，端点使用
    ``utf-8-sig`` 编码以兼容 Excel，并设置附件文件名。未命中映射为 404。
    """
    try:
        exam_service = ExamService(db)
        csv_content = await exam_service.export_exam_result_to_csv(result_id)

        # 创建响应头，指定为CSV文件下载
        from fastapi import Response

        headers = {
            "Content-Disposition": f"attachment; filename=exam_result_{result_id}.csv",
            "Content-Type": "text/csv; charset=utf-8"
        }

        # 返回响应，使用 utf-8-sig 编码确保Excel正确显示中文
        return Response(
            content=csv_content.encode('utf-8-sig'),
            headers=headers
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error exporting exam result: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"导出考试结果失败: {str(e)}"
        )