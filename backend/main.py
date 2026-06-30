"""
HR Agent后端 - FastAPI应用程序入口点
"""
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging

from app.core.config import settings
from app.core.database import init_db, close_db
from app.api.v1.api import api_router
from app.core.middleware import setup_middleware
from app.core.logging import setup_logging
from app.core.exception_handlers import setup_exception_handlers
from app.services.email_scheduler import EmailScheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用程序生命周期事件"""
    # 启动
    logger.info("正在启动HR Agent后端...")
    setup_logging()
    logger.info("日志配置完成")

    try:
        await init_db()
        logger.info("数据库初始化成功")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
        raise

    # 初始化并启动邮件调度器
    email_scheduler = EmailScheduler()
    await email_scheduler.start()
    app.state.email_scheduler = email_scheduler
    logger.info("邮件调度器启动成功")

    logger.info("HR Agent后端启动成功")
    yield

    # 关闭
    logger.info("正在关闭HR Agent后端...")
    try:
        # 停止邮件调度器
        if hasattr(app.state, 'email_scheduler') and app.state.email_scheduler:
            for stopper in app.state.email_scheduler.stoppers.values():
                stopper.set()
            for task in app.state.email_scheduler.tasks.values():
                task.cancel()
            logger.info("邮件调度器已停止")
        
        await close_db()
        logger.info("数据库连接已关闭")
    except Exception as e:
        logger.error(f"关闭时出错: {e}")
    logger.info("HR Agent后端关闭完成")


def create_application() -> FastAPI:
    """创建并配置FastAPI应用程序"""
    app = FastAPI(
        title=settings.PROJECT_NAME,
        description="HR Agent - AI驱动的人力资源助手",
        version=settings.VERSION,
        openapi_url=f"{settings.API_V1_STR}/openapi.json",
        docs_url=f"{settings.API_V1_STR}/docs",
        redoc_url=f"{settings.API_V1_STR}/redoc",
        lifespan=lifespan
    )

    # 设置异常处理程序
    setup_exception_handlers(app)

    # 设置CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.BACKEND_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 设置自定义中间件
    setup_middleware(app)

    # 包含API路由
    app.include_router(api_router, prefix=settings.API_V1_STR)

    # 添加根端点
    @app.get("/")
    async def root():
        """带有API信息的根端点"""
        return {
            "message": "欢迎使用HR Agent API",
            "version": settings.VERSION,
            "docs": f"{settings.API_V1_STR}/docs",
            "redoc": f"{settings.API_V1_STR}/redoc",
            "health": f"{settings.API_V1_STR}/health"
        }

    return app


app = create_application()


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info"
    )