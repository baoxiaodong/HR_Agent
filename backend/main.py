"""
FastAPI 应用入口。

启动时完成日志、数据库和邮件调度器初始化；正常关闭路径先停止后台任务，再释放连接池。
若停止任务抛错，同一 ``try`` 中的 ``close_db`` 会被跳过。
路由和中间件在 ``create_application`` 中集中注册。
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
    """管理应用启动与关闭；``yield`` 前后分别对应 FastAPI 的启动和清理阶段。"""
    # 启动阶段：初始化失败会阻止应用进入可服务状态。
    logger.info("正在启动HR Agent后端...")
    setup_logging()
    logger.info("日志配置完成")

    try:
        await init_db()
        logger.info("数据库初始化成功")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
        raise

    # 后台任务阶段：调度器需在数据库就绪后启动。
    email_scheduler = EmailScheduler()
    await email_scheduler.start()
    # 保存生命周期内的共享实例，供关闭阶段停止其任务。
    app.state.email_scheduler = email_scheduler
    logger.info("邮件调度器启动成功")

    logger.info("HR Agent后端启动成功")
    yield

    # 正常关闭路径先停止后台任务，再释放连接池；停止步骤抛错会跳过 close_db。
    logger.info("正在关闭HR Agent后端...")
    try:
        # 调度器未挂载或实例为空时跳过停止逻辑。
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
    """集中创建应用并注册异常处理、中间件、路由和生命周期钩子。"""
    app = FastAPI(
        title=settings.PROJECT_NAME,
        description="HR Agent - AI驱动的人力资源助手",
        version=settings.VERSION,
        openapi_url=f"{settings.API_V1_STR}/openapi.json",
        docs_url=f"{settings.API_V1_STR}/docs",
        redoc_url=f"{settings.API_V1_STR}/redoc",
        lifespan=lifespan
    )

    # 应用装配顺序保持集中可见，避免各业务路由自行修改全局行为。
    setup_exception_handlers(app)

    # CORS 作为全局中间件处理跨域请求。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.BACKEND_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册项目自定义中间件。
    setup_middleware(app)

    # 所有 v1 业务路由统一挂载在配置的 API 前缀下。
    app.include_router(api_router, prefix=settings.API_V1_STR)

    # 根端点只提供服务发现信息，不承载业务逻辑。
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
