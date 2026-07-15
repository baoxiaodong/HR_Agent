"""
简历评价结果管理与附件导出 API。

自动评价接口把邮件系统提供的简历文本交给 ``ResumeEvaluationService`` 完成用户匹配、
JD 匹配、AI 评分和持久化；其余接口基于当前登录用户查询、更新或删除评价记录。
批量导出会先按用户过滤数据库记录，再从磁盘读取原始附件并在内存中生成 ZIP 响应。
"""
import asyncio
import io
import logging
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from uuid import UUID

import aiofiles
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.document import Document
from app.models.resume_evaluation import ResumeEvaluation
import logging
from typing import List, Optional
from uuid import UUID
import os
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.services.resume_evaluation_service import ResumeEvaluationService
from pydantic import BaseModel, Field
from app.schemas.resume_evaluation import (
    ResumeEvaluationResponse,
    ResumeEvaluationListResponse,
    ResumeEvaluationResult
)
from app.models.resume_evaluation import ResumeStatus
from app.core.config import settings
from app.schemas.resume_evaluation import ExportZipRequest
from app.schemas.email_config import AutoEvaluateRequest

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/evaluate-auto", response_model=ResumeEvaluationResult)
async def evaluate_resume_auto(
    payload: AutoEvaluateRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    处理邮件抓取流程提交的纯文本简历，并自动完成评价。

    该入口没有 Bearer 用户上下文，服务层会使用 ``login_name`` 查找归属用户；随后根据
    ``position`` 辅助匹配 JD、调用 AI 评分、保存文本附件和评价记录，最后用响应 Schema
    校验服务层返回的数据结构。
    """
    try:
        # 端点只传递已经由 AutoEvaluateRequest 校验过的字段；用户/JD 匹配均在服务层完成。
        evaluation_service = ResumeEvaluationService(db)
        result = await evaluation_service.evaluate_resume_text_auto(
            login_name=payload.login_name,
            resume_text=payload.resume_text,
            filename=payload.filename,
            subject=payload.position or ""
        )

        # Schema 构造会在返回前再次验证 ID、分数、状态等字段是否符合 API 契约。
        return ResumeEvaluationResult(**result)

    except ValueError as e:
        # 可预期的输入或匹配失败直接返回具体原因，便于调用方修正数据。
        logger.warning(f"简历评价参数错误: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        # 保留服务层已经明确指定的 HTTP 状态码。
        raise
    except Exception as e:
        # 其他内部错误不把堆栈和供应商信息暴露给客户端。
        logger.error(f"自动匹配并评价简历失败: {e}")
        raise HTTPException(status_code=500, detail="自动匹配评价服务暂时不可用")

@router.get("/history", response_model=ResumeEvaluationListResponse)
async def get_evaluation_history(
    skip: int = 0,
    limit: int = 20,
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    分页返回当前登录用户自己的评价记录。

    状态字符串先转换为 ``ResumeStatus`` 枚举，查询上限强制收敛到 100，避免客户端通过
    过大的 limit 一次性读取全部历史。
    """
    try:
        # 在查询数据库前校验状态，非法值作为 400 参数错误返回。
        status_filter = await ResumeEvaluationService.validate_status_param(status)

        # 即使调用方传入更大的值，也只允许服务层最多查询 100 条。
        limit = min(limit, 100)

        # user_id 是数据隔离条件，服务层不会返回其他用户的评价记录。
        evaluation_service = ResumeEvaluationService(db)
        result = await evaluation_service.get_evaluation_history_with_pagination(
            user_id=current_user.id,
            skip=skip,
            limit=limit,
            status=status_filter
        )
        
        return result

    except ValueError as e:
        logger.warning(f"获取评价历史参数错误: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"获取评价历史失败: {e}")
        raise HTTPException(status_code=500, detail="获取评价历史失败")


@router.get("/supported-formats")
async def get_supported_formats():
    """返回解析器当前声明支持的扩展名和大小限制。

    该响应是服务层维护的静态能力描述，不读取数据库，也不代表上传内容已经通过实际解析；
    文件大小、扩展名和正文有效性仍会在评价主流程中再次校验。
    """
    return await ResumeEvaluationService.get_supported_formats()

@router.get("/{evaluation_id}", response_model=ResumeEvaluationResult)
async def get_evaluation_detail(
    evaluation_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """返回当前用户拥有的一条评价记录的扁平化详情。

    路径参数先转换为 UUID；服务层在同一查询中组合评价 ID 与用户 ID，未命中统一返回 404，
    因此不会向请求方暴露记录是不存在还是属于其他用户。结果再经过响应 Schema 校验。
    """
    try:
        # 字符串 ID 在访问数据库前收窄为 UUID，格式错误与资源未找到保持不同状态码。
        eval_uuid = await ResumeEvaluationService.validate_uuid_param(evaluation_id, "评价ID")

        evaluation_service = ResumeEvaluationService(db)
        result = await evaluation_service.get_evaluation_detail(
            evaluation_id=eval_uuid,
            user_id=current_user.id
        )

        if not result:
            raise HTTPException(status_code=404, detail="评价记录不存在")

        return ResumeEvaluationResult(**result)

    except ValueError as e:
        logger.warning(f"获取评价详情参数错误: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取评价详情失败: {e}")
        raise HTTPException(status_code=500, detail="获取评价详情失败")


@router.delete("/{evaluation_id}")
async def delete_evaluation(
    evaluation_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除当前用户的一条评价及其远程面试方案关联。

    服务层先按评价 ID 和用户 ID 校验归属，再删除远程面试方案，最后提交本地评价删除。
    两个数据源无法组成原子事务：远程删除成功而本地提交失败时，评价仍在但关联方案已删除。
    """
    try:
        # 先拒绝非法路径参数，避免把格式错误误报为记录不存在。
        eval_uuid = await ResumeEvaluationService.validate_uuid_param(evaluation_id, "评价ID")

        evaluation_service = ResumeEvaluationService(db)
        success = await evaluation_service.delete_evaluation(
            evaluation_id=eval_uuid,
            user_id=current_user.id
        )

        if not success:
            raise HTTPException(status_code=404, detail="评价记录不存在")

        return {"message": "评价记录已删除"}

    except ValueError as e:
        logger.warning(f"删除评价记录参数错误: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除评价记录失败: {e}")
        raise HTTPException(status_code=500, detail="删除评价记录失败")


@router.put("/{evaluation_id}/status")
async def update_resume_status(
    evaluation_id: str,
    status: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """把当前用户的一条评价推进到指定候选状态。

    路径 ID 和状态字符串分别转换为 UUID、``ResumeStatus``；服务层再以用户 ID 限定目标记录，
    修改 ORM 状态并提交单条本地事务。非法枚举返回 400，未命中或越权统一返回 404。
    """
    try:
        # 在服务写入前完成两类类型收窄，后续分支只处理合法 UUID 和状态枚举。
        eval_uuid = await ResumeEvaluationService.validate_uuid_param(evaluation_id, "评价ID")
        new_status = await ResumeEvaluationService.validate_status_param(status)

        evaluation_service = ResumeEvaluationService(db)
        result = await evaluation_service.update_evaluation_status(
            evaluation_id=eval_uuid,
            user_id=current_user.id,
            new_status=new_status
        )

        if not result:
            raise HTTPException(status_code=404, detail="评价记录不存在")

        return result

    except ValueError as e:
        logger.warning(f"更新简历状态参数错误: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新简历状态失败: {e}")
        raise HTTPException(status_code=500, detail="更新简历状态失败")



@router.post("/export-zip")
async def export_zip(
        payload: ExportZipRequest,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    把当前用户选中的原始简历附件打包为内存 ZIP。

    流程包括数量校验、按用户过滤数据库记录、净化 ZIP 内文件名、读取主/兼容路径、关闭
    ZipFile 后提取完整字节，最后构造下载响应。单个附件缺失会记录到 ``failed``，不会让
    其他可读取附件停止打包。
    """
    resume_ids = payload.resume_ids

    # 空列表没有导出意义；50 条上限用于约束磁盘读取量和内存中的 ZIP 大小。
    if not resume_ids:
        raise HTTPException(status_code=400, detail="resume_ids 不能为空")
    if len(resume_ids) > 50:
        raise HTTPException(status_code=400, detail="单次导出数量不能超过 50")

    from sqlalchemy import select

    # user_id 必须和资源 ID 同时过滤，防止用户通过猜测 UUID 导出他人的简历附件。
    stmt = (
        select(ResumeEvaluation)
        .where(
            ResumeEvaluation.id.in_(resume_ids),
            ResumeEvaluation.user_id == current_user.id
        )
    )
    result = await db.execute(stmt)
    resume_evaluations = result.scalars().all()

    if not resume_evaluations:
        raise HTTPException(status_code=400, detail="未找到相关简历")

    # BytesIO 让服务无需创建临时 ZIP 文件，但内存占用会随附件总大小增长。
    io_buf = io.BytesIO()
    failed = []

    try:
        logger.info(f"准备导出 {len(resume_evaluations)} 个简历")
        with zipfile.ZipFile(io_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for resume in resume_evaluations:
                logger.info(f"处理简历: {resume.original_filename}, 路径: {resume.file_path}")
                original_filename = resume.original_filename

                # 只保留文件名，去掉调用方或历史数据中的目录部分，避免 ZIP 路径穿越。
                safe_name = Path(original_filename).name

                file_found = False

                # 优先读取数据库记录的当前路径；它是正常流程保存附件时的权威位置。
                if resume.file_path and os.path.exists(resume.file_path):
                    logger.info(f"文件路径存在: {resume.file_path}")
                    try:
                        async with aiofiles.open(resume.file_path, "rb") as f:
                            data = await f.read()
                            logger.info(f"成功读取文件 {resume.file_path}, 大小: {len(data)} 字节")

                            # 使用 ZipInfo 设置正确的文件信息
                            zip_info = zipfile.ZipInfo(safe_name)
                            zip_info.date_time = time.localtime(time.time())[:6]
                            zip_info.compress_type = zipfile.ZIP_DEFLATED
                            # 设置文件权限 (可读)
                            zip_info.external_attr = 0o644 << 16

                            zf.writestr(zip_info, data)
                            file_found = True

                    except Exception as e:
                        logger.error(f"读取文件失败 {resume.file_path}: {e}")
                        failed.append({"name": safe_name, "reason": str(e)})
                else:
                    logger.warning(f"数据库中的文件路径不存在或无效: {resume.file_path}")
                    # 尝试在旧路径中查找文件
                    user_id = str(current_user.id)
                    possible_paths = [
                        os.path.join(settings.UPLOAD_DIR, user_id, original_filename),
                        os.path.join(settings.UPLOAD_DIR, str(resume.user_id), original_filename),
                        resume.file_path  # 即使文件不存在也尝试一下
                    ]

                    for file_path in possible_paths:
                        if file_path and os.path.exists(file_path):
                            try:
                                async with aiofiles.open(file_path, "rb") as f:
                                    data = await f.read()
                                    logger.info(f"成功从备用路径读取文件 {file_path}, 大小: {len(data)} 字节")

                                    # 使用 ZipInfo 设置正确的文件信息
                                    zip_info = zipfile.ZipInfo(safe_name)
                                    zip_info.date_time = time.localtime(time.time())[:6]
                                    zip_info.compress_type = zipfile.ZIP_DEFLATED
                                    # 设置文件权限 (可读)
                                    zip_info.external_attr = 0o644 << 16

                                    zf.writestr(zip_info, data)
                                    file_found = True
                                    break  # 成功找到文件，退出循环

                            except Exception as e:
                                logger.error(f"从备用路径读取文件失败 {file_path}: {e}")
                                continue  # 尝试下一个可能的路径

                        if file_found:
                            break

                    if not file_found:
                        logger.warning(f"所有可能的文件路径都尝试过，但未找到: {original_filename}")
                        failed.append({"name": safe_name, "reason": "文件不存在"})

        # 关键修复：在关闭ZipFile后获取完整的字节数据
        zip_data = io_buf.getvalue()

    except Exception as e:
        # 记录错误日志
        logger.error(f"创建ZIP文件失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"创建下载文件失败: {str(e)}")
    finally:
        # 确保流被关闭
        if not io_buf.closed:
            io_buf.close()

    # 生成文件名
    filename = f"resume_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"

    # 返回响应 - 使用Response替代StreamingResponse
    return Response(
        content=zip_data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Length": str(len(zip_data)),  # 重要：设置正确的Content-Length
            "Content-Type": "application/zip"
        }
    )
