"""
应用程序的异常处理程序
"""
import logging
from typing import Union
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from pydantic import ValidationError

from app.core.exceptions import BaseHTTPException, DatabaseIntegrityError, DatabaseError

logger = logging.getLogger(__name__)


async def base_http_exception_handler(request: Request, exc: BaseHTTPException) -> JSONResponse:
    """处理自定义基础HTTP异常"""
    logger.error(
        f"HTTP异常: {exc.message}",
        extra={
            "status_code": exc.status_code,
            "path": request.url.path,
            "method": request.method,
            "details": exc.details,
        }
    )

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.message,
                "type": exc.__class__.__name__,
                "details": exc.details,
            }
        }
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """处理FastAPI HTTP异常"""
    logger.error(
        f"HTTP异常: {exc.detail}",
        extra={
            "status_code": exc.status_code,
            "path": request.url.path,
            "method": request.method,
        }
    )

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.detail,
                "type": "HTTPException",
            }
        }
    )


async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """处理Starlette HTTP异常"""
    logger.error(
        f"Starlette HTTP异常: {exc.detail}",
        extra={
            "status_code": exc.status_code,
            "path": request.url.path,
            "method": request.method,
        }
    )

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.detail,
                "type": "HTTPException",
            }
        }
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """处理请求验证错误"""
    # 获取请求的表单数据（如果是POST请求）
    form_data = {}
    try:
        if request.method == "POST":
            form_data = await request.form()
            form_data = {k: str(v)[:100] for k, v in form_data.items()}  # 限制长度避免日志过长
    except:
        pass
    
    logger.warning(
        f"验证错误: {exc.errors()}",
        extra={
            "path": request.url.path,
            "method": request.method,
            "errors": exc.errors(),
            "received_form_fields": list(form_data.keys()) if form_data else [],
            "status_code": 422
        }
    )

    # 格式化验证错误
    formatted_errors = []
    for error in exc.errors():
        field_path = " -> ".join(str(loc) for loc in error["loc"])
        formatted_errors.append({
            "field": field_path,
            "message": error["msg"],
            "type": error["type"],
        })

    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "message": "验证失败",
                "type": "ValidationError",
                "details": {
                    "errors": formatted_errors
                }
            }
        }
    )


async def pydantic_validation_exception_handler(request: Request, exc: ValidationError) -> JSONResponse:
    """处理Pydantic验证错误"""
    logger.warning(
        f"Pydantic验证错误: {exc.errors()}",
        extra={
            "path": request.url.path,
            "method": request.method,
            "errors": exc.errors(),
        }
    )

    # 格式化验证错误
    formatted_errors = []
    for error in exc.errors():
        field_path = " -> ".join(str(loc) for loc in error["loc"])
        formatted_errors.append({
            "field": field_path,
            "message": error["msg"],
            "type": error["type"],
        })

    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "message": "数据验证失败",
                "type": "ValidationError",
                "details": {
                    "errors": formatted_errors
                }
            }
        }
    )


async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    """处理SQLAlchemy数据库错误"""
    logger.error(
        f"数据库错误: {str(exc)}",
        extra={
            "path": request.url.path,
            "method": request.method,
            "exception_type": exc.__class__.__name__,
        },
        exc_info=True
    )

    # 处理特定的SQLAlchemy异常
    if isinstance(exc, IntegrityError):
        # 检查常见的完整性约束违规
        error_message = str(exc.orig) if hasattr(exc, 'orig') else str(exc)

        if "unique constraint" in error_message.lower():
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "message": "资源已存在",
                        "type": "ConflictError",
                        "details": {
                            "constraint": "unique_constraint_violation"
                        }
                    }
                }
            )
        elif "foreign key constraint" in error_message.lower():
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "对相关资源的引用无效",
                        "type": "ValidationError",
                        "details": {
                            "constraint": "foreign_key_constraint_violation"
                        }
                    }
                }
            )
        else:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "数据完整性约束被违反",
                        "type": "ValidationError",
                        "details": {
                            "constraint": "integrity_constraint_violation"
                        }
                    }
                }
            )

    # 通用数据库错误
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": "数据库操作失败",
                "type": "DatabaseError",
            }
        }
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """处理所有其他未处理的异常"""
    logger.error(
        f"未处理的异常: {str(exc)}",
        extra={
            "path": request.url.path,
            "method": request.method,
            "exception_type": exc.__class__.__name__,
        },
        exc_info=True
    )

    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": "发生意外错误",
                "type": "InternalServerError",
            }
        }
    )


def setup_exception_handlers(app):
    """设置所有异常处理程序"""

    # 自定义异常处理程序
    app.add_exception_handler(BaseHTTPException, base_http_exception_handler)

    # FastAPI和Starlette异常处理程序
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(StarletteHTTPException, starlette_http_exception_handler)

    # 验证异常处理程序
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(ValidationError, pydantic_validation_exception_handler)

    # 数据库异常处理程序
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)

    # 通用异常处理程序（捕获所有）
    app.add_exception_handler(Exception, generic_exception_handler)

    logger.info("异常处理程序设置完成")