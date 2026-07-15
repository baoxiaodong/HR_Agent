"""
开发环境的内置角色与测试管理员种子脚本。

脚本直接创建异步数据库会话：先按角色名称补齐内置角色，再按用户名补齐测试用户，最后补
齐用户—角色关联。每一步都先查询后插入，重复运行通常不会制造重复数据；但三个阶段分别
提交，并不是一个原子事务。脚本包含固定测试凭据，不应用于生产环境。
"""

import asyncio
from sqlalchemy import select
import sys
import os
from pathlib import Path

# 将后端目录添加到Python路径
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from app.core.database import AsyncSessionLocal, init_db
from app.models.user import Role, User, UserRoleAssociation, UserRole
from app.core.security import get_password_hash


async def seed_roles_and_admin():
    """幂等补齐角色、测试管理员及其多角色关联。

    ``User.role`` 旧枚举和 ``user_role`` 多角色关联都会写入，使依赖任一权限模型的现有
    调用链都能识别该测试管理员。阶段性 ``commit`` 让后续查询可见前一步生成的主键。
    """
    await init_db()

    async with AsyncSessionLocal() as db:
        names = ["普通用户", "超级管理员"]
        existing = await db.execute(select(Role).where(Role.name.in_(names)))
        exist_names = {r.name for r in existing.scalars().all()}

        if "普通用户" not in exist_names:
            db.add(Role(name="普通用户", description="默认普通用户角色", is_builtin=True))
        if "超级管理员" not in exist_names:
            db.add(Role(name="超级管理员", description="系统超级管理员角色", is_builtin=True))
        await db.commit()

        res = await db.execute(select(User).where(User.username == "testuser", User.is_active == True))
        user = res.scalar_one_or_none()
        if not user:
            user = User(
                username="testuser",
                email="testuser@example.com",
                full_name="Test User",
                hashed_password=get_password_hash("test123"),
                role=UserRole.ADMIN,
                is_superuser=True,
                is_verified=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

        res = await db.execute(select(Role).where(Role.name == "超级管理员"))
        role = res.scalar_one()
        link_exists = await db.execute(
            select(UserRoleAssociation).where(
                UserRoleAssociation.user_id == user.id,
                UserRoleAssociation.role_id == role.id,
            )
        )
        if not link_exists.scalar_one_or_none():
            db.add(UserRoleAssociation(user_id=user.id, role_id=role.id))
            await db.commit()


def main():
    """在独立事件循环中执行异步种子流程。"""
    asyncio.run(seed_roles_and_admin())


if __name__ == "__main__":
    main()