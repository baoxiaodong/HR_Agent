"""
FastAPI 认证与授权依赖。

认证依赖负责还原并校验当前用户，授权来源包括 ``current_user.role`` 字段、
超级用户标志和角色关联表查询。
"""
from typing import Generator, Optional, List
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.config import settings
from app.core.database import get_db
from app.models.user import User, Role, UserRoleAssociation
from app.schemas.user import User as UserSchema
from app.services.user_service import UserService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login")

# 认证链：读取 Bearer Token -> 解码用户 ID -> 查询数据库用户 -> 校验启用状态。

async def get_current_user(
    db: AsyncSession = Depends(get_db),
    token: str = Depends(oauth2_scheme)
) -> User:
    """从访问令牌还原当前用户；令牌或用户无效时中断后续依赖链。"""
    import logging
    logger = logging.getLogger(__name__)
    
    logger.info("🔍 调用get_current_user")
    
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        logger.info(f"🔑 正在解码令牌: {token[:20]}...")
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=["HS256"]
        )
        user_id: str = payload.get("sub")
        logger.info(f"👤 提取的用户ID: {user_id}")
        if user_id is None:
            logger.error("❌ 令牌载荷中没有user_id")
            raise credentials_exception
    except JWTError as e:
        logger.error(f"❌ JWT decode error: {e}")
        raise credentials_exception
    
    try:
        from uuid import UUID
        user_service = UserService(db)
        logger.info(f"🔍 正在查找用户ID: {user_id}")
        user_uuid = UUID(user_id)
        user = await user_service.get_user(user_uuid)
        
        if user is None:
            logger.error(f"❌ 未找到用户ID: {user_id}")
            raise credentials_exception
        
        logger.info(f"✅ 找到用户: {user.username}, 活跃状态: {user.is_active}")
        
        if not user.is_active:
            logger.error(f"❌ 用户 {user.username} 处于非活跃状态")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Inactive user"
            )
        
        logger.info(f"✅ 返回用户: {user.username}")
        return user
    except Exception as e:
        logger.error(f"❌ Error in get_current_user: {e}")
        raise



async def get_current_hr_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """允许 HR 角色访问，并按现有规则对超级用户标志直接放行。"""
    from app.models.user import UserRole
    
    if current_user.role not in [UserRole.HR_MANAGER, UserRole.HR_SPECIALIST] and not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="HR permissions required"
        )
    return current_user

async def get_current_superuser(
    current_user: User = Depends(get_current_user),
) -> User:
    """仅允许带有超级用户标志的用户访问，否则返回 403。"""
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superuser permissions required"
        )
    return current_user


async def get_current_admin_by_role(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> User:
    """
    通过用户-角色关联表校验“超级管理员”角色。

    查询只过滤 ``Role.is_active``，不检查关联记录启用状态；超级用户不自动绕过。
    未关联目标角色时返回 403。
    """
    query = (
        select(Role)
        .join(UserRoleAssociation, Role.id == UserRoleAssociation.role_id)
        .where(UserRoleAssociation.user_id == current_user.id, Role.is_active == True)
    )
    result = await db.execute(query)
    roles = {r.name for r in result.scalars().all()}
    if "超级管理员" not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要超级管理员角色"
        )
    return current_user


def require_any_role_names(required: List[str]):
    """
    创建按角色名称校验的依赖，命中任一有效关联角色即可放行。

    查询只过滤 ``Role.is_active``，不检查关联记录启用状态；超级用户不自动绕过。
    无匹配角色时返回 403。
    """
    async def dependency(
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user),
    ) -> User:
        query = (
            select(Role)
            .join(UserRoleAssociation, Role.id == UserRoleAssociation.role_id)
            .where(UserRoleAssociation.user_id == current_user.id, Role.is_active == True)
        )
        result = await db.execute(query)
        roles = {r.name for r in result.scalars().all()}
        if not any(name in roles for name in required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="角色权限不足"
            )
        return current_user
    return dependency
