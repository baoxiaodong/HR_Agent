"""
数据库配置和连接管理
"""
import logging
from typing import AsyncGenerator
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

logger = logging.getLogger(__name__)

# 创建SQLAlchemy引擎
engine = create_async_engine(
    settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://"),
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    echo=settings.DEBUG,
)

# 创建会话工厂
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# 创建模型的基类
Base = declarative_base()


def get_async_engine():
    """
    获取异步数据库引擎
    """
    return engine


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    获取数据库会话的依赖项
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """
    初始化数据库并创建表
    """
    try:
        async with engine.begin() as conn:

            # 如果使用PostgreSQL则启用pgvector扩展
            if "postgresql" in settings.DATABASE_URL:
                try:
                    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                    logger.info("pgvector扩展已启用")
                except Exception as e:
                    logger.warning(f"无法启用pgvector扩展: {e}")

            # 创建所有表
            await conn.run_sync(Base.metadata.create_all)

        logger.info("数据库初始化成功")

    except Exception as e:
        logger.error(f"数据库初始化错误: {e}")
        raise


async def close_db() -> None:
    """
    关闭数据库连接
    """
    try:
        await engine.dispose()
        logger.info("数据库连接已关闭")
    except Exception as e:
        logger.error(f"关闭数据库时出错: {e}")


async def check_db_connection() -> bool:
    """
    检查数据库连接
    """
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            return True

    except Exception as e:
        logger.error(f"数据库连接检查失败: {e}")
        return False