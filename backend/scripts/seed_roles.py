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
    asyncio.run(seed_roles_and_admin())


if __name__ == "__main__":
    main()