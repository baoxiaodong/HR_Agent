
"""
HR Agent后端数据库管理脚本

此脚本提供数据库管理工具：
- 初始化数据库
- 创建迁移
- 应用迁移
- 重置数据库
- 种子初始数据
"""

import asyncio
import sys
import os
from pathlib import Path

# 将后端目录添加到Python路径
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from alembic.config import Config
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
import logging

from app.core.config import settings
from app.core.database import get_async_engine, init_db
from app.models.base import Base

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class DatabaseManager:
    """数据库管理工具"""
    
    def __init__(self):
        self.alembic_cfg = Config(str(backend_dir / "alembic.ini"))
        self.alembic_cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    
    async def create_database(self):
        """如果数据库不存在则创建数据库"""
        try:
            # 从URL中提取数据库名称
            db_name = settings.DATABASE_NAME
            
            # 连接到postgres数据库以创建我们的数据库
            postgres_url = settings.DATABASE_URL.replace(f"/{db_name}", "/postgres")
            engine = create_async_engine(postgres_url)
            
            async with engine.connect() as conn:
                # 检查数据库是否存在
                result = await conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :db_name"),
                    {"db_name": db_name}
                )
                
                if not result.fetchone():
                    # 创建数据库
                    await conn.execute(text("COMMIT"))  # 结束事务
                    await conn.execute(text(f"CREATE DATABASE {db_name}"))
                    logger.info(f"数据库 '{db_name}' 创建成功")
                else:
                    logger.info(f"数据库 '{db_name}' 已存在")
            
            await engine.dispose()
            
        except Exception as e:
            logger.error(f"创建数据库时出错: {e}")
            raise
    
    async def drop_database(self):
        """删除数据库"""
        try:
            db_name = settings.DATABASE_NAME
            postgres_url = settings.DATABASE_URL.replace(f"/{db_name}", "/postgres")
            engine = create_async_engine(postgres_url)
            
            async with engine.connect() as conn:
                # 终止现有连接
                await conn.execute(text("COMMIT"))
                await conn.execute(
                    text("""
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = :db_name AND pid <> pg_backend_pid()
                    """),
                    {"db_name": db_name}
                )
                
                # 删除数据库
                await conn.execute(text(f"DROP DATABASE IF EXISTS {db_name}"))
                logger.info(f"数据库 '{db_name}' 删除成功")
            
            await engine.dispose()
            
        except Exception as e:
            logger.error(f"删除数据库时出错: {e}")
            raise
    
    def create_migration(self, message: str):
        """创建新的迁移"""
        try:
            command.revision(self.alembic_cfg, autogenerate=True, message=message)
            logger.info(f"迁移已创建: {message}")
        except Exception as e:
            logger.error(f"创建迁移时出错: {e}")
            raise
    
    def apply_migrations(self):
        """应用所有待处理的迁移"""
        try:
            command.upgrade(self.alembic_cfg, "head")
            logger.info("迁移应用成功")
        except Exception as e:
            logger.error(f"应用迁移时出错: {e}")
            raise
    
    def downgrade_migration(self, revision: str = "-1"):
        """降级到特定版本"""
        try:
            command.downgrade(self.alembic_cfg, revision)
            logger.info(f"已降级到版本: {revision}")
        except Exception as e:
            logger.error(f"降级迁移时出错: {e}")
            raise
    
    def show_migration_history(self):
        """显示迁移历史"""
        try:
            command.history(self.alembic_cfg)
        except Exception as e:
            logger.error(f"显示迁移历史时出错: {e}")
            raise
    
    def show_current_revision(self):
        """显示当前数据库版本"""
        try:
            command.current(self.alembic_cfg)
        except Exception as e:
            logger.error(f"显示当前版本时出错: {e}")
            raise
    
    async def reset_database(self):
        """重置数据库（删除、创建、迁移）"""
        logger.info("正在重置数据库...")
        await self.drop_database()
        await self.create_database()
        self.apply_migrations()
        logger.info("数据库重置完成")
    
    async def init_database(self):
        """初始化数据库（创建、迁移）"""
        logger.info("正在初始化数据库...")
        await self.create_database()
        self.apply_migrations()
        logger.info("数据库初始化完成")


async def main():
    """处理命令行参数的主函数"""
    if len(sys.argv) < 2:
        print("用法: python db_manager.py <命令> [参数]")
        print("命令:")
        print("  init                    - 初始化数据库")
        print("  reset                   - 重置数据库（删除并重新创建）")
        print("  migrate <消息>          - 创建新迁移")
        print("  upgrade                 - 应用迁移")
        print("  downgrade [版本]        - 降级迁移")
        print("  history                 - 显示迁移历史")
        print("  current                 - 显示当前版本")
        return
    
    command = sys.argv[1]
    db_manager = DatabaseManager()
    
    try:
        if command == "init":
            await db_manager.init_database()
        
        elif command == "reset":
            await db_manager.reset_database()
        
        elif command == "migrate":
            if len(sys.argv) < 3:
                print("错误: 需要迁移消息")
                return
            message = " ".join(sys.argv[2:])
            db_manager.create_migration(message)
        
        elif command == "upgrade":
            db_manager.apply_migrations()
        
        elif command == "downgrade":
            revision = sys.argv[2] if len(sys.argv) > 2 else "-1"
            db_manager.downgrade_migration(revision)
        
        elif command == "history":
            db_manager.show_migration_history()
        
        elif command == "current":
            db_manager.show_current_revision()
        
        else:
            print(f"未知命令: {command}")
            return
    
    except Exception as e:
        logger.error(f"Command failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())