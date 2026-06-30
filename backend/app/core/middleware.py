"""
    自定义中间件
"""
import time
import logging
from typing import Callable
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class LoggingMiddleware(BaseHTTPMiddleware):
    """
        记录请求和响应信息的中间件
    """
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.time()
        
        # 记录 request日志
        logger.info(f"Request: {request.method} {request.url}")
        
        # 执行 request
        response = await call_next(request)
        
        # 计算时间消耗 time
        process_time = time.time() - start_time
        
        # 记录 response 日志
        logger.info(
            f"Response: {response.status_code} - "
            f"Processing time: {process_time:.4f}s"
        )
        
        # 增加响应头的处理时间
        response.headers["X-Process-Time"] = str(process_time)
        
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
        添加安全Header 中间件
    """
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        
        # Add security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        
        return response


def setup_middleware(app: FastAPI) -> None:
    """
        设置FastAPI应用程序的所有中间件
    """
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    
    logger.info("Middleware setup complete")