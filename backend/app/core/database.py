"""
异步数据库引擎、会话依赖和连接生命周期管理。

请求通过 ``get_db`` 独占会话；应用启动时准备扩展与表结构，关闭阶段负责释放连接池。
"""
import logging
from typing import AsyncGenerator
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

logger = logging.getLogger(__name__)

# 异步引擎在模块导入时创建，应用关闭时由生命周期释放连接池。
engine = create_async_engine(
    settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://"),
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    echo=settings.DEBUG,
)

# 请求级会话工厂；提交后保留已加载属性，便于响应序列化继续读取模型数据。
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# 所有 ORM 模型共享的元数据入口，供初始化阶段统一建表。
Base = declarative_base()


def get_async_engine():
    """返回应用共享的异步数据库引擎。"""
    return engine


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    为单次依赖调用提供异步数据库会话。

    下游异常会触发回滚并继续抛出；无论成功失败，请求结束时都会关闭会话。
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
    初始化数据库能力并按模型元数据创建缺失表。

    CREATE EXTENSION 失败可能使当前事务失效，导致后续建表及应用启动失败。
    """
    try:
        async with engine.begin() as conn:

            # 向量字段依赖 pgvector，因此在建表前先尝试启用扩展。
            if "postgresql" in settings.DATABASE_URL:
                try:
                    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                    logger.info("pgvector扩展已启用")
                except Exception as e:
                    logger.warning(f"无法启用pgvector扩展: {e}")

            # 随后根据已注册模型的元数据调用建表；事务失效时此处会继续抛错。
            await conn.run_sync(Base.metadata.create_all)

        logger.info("数据库初始化成功")

    except Exception as e:
        logger.error(f"数据库初始化错误: {e}")
        raise


async def close_db() -> None:
    """释放引擎连接池；关闭阶段失败只记录日志，不再次抛出。"""
    try:
        await engine.dispose()
        logger.info("数据库连接已关闭")
    except Exception as e:
        logger.error(f"关闭数据库时出错: {e}")


async def check_db_connection() -> bool:
    """用轻量查询探测数据库可用性，并将连接异常转换为 ``False``。"""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            return True

    except Exception as e:
        logger.error(f"数据库连接检查失败: {e}")
        return False
