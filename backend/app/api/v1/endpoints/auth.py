"""
身份验证端点
"""
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.auth import Token, UserCreate
from app.schemas.user import User as UserSchema
from app.services.user_service import UserService
from app.api.deps import get_current_user

router = APIRouter()


@router.post("/register", response_model=UserSchema)
async def register(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db)
) -> Any:
    """
    注册新用户
    """
    user_service = UserService(db)
    
    try:
        user = await user_service.register_user(user_data)
        return user
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.post("/login", response_model=Token)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """
    登录并获取访问令牌
    """
    user_service = UserService(db)
    
    try:
        token_data = await user_service.login_user(form_data.username, form_data.password)
        return token_data
    except ValueError as e:
        if "用户名或密码错误" in str(e):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(e),
                headers={"WWW-Authenticate": "Bearer"},
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e)
            )


@router.post("/refresh", response_model=Token)
async def refresh_token(
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """
    刷新访问令牌
    """
    user_service = UserService(db)
    return await user_service.refresh_user_token(str(current_user.id))


@router.get("/me")
async def get_current_user_info(
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """
    获取当前用户信息
    """
    user_service = UserService(db)
    return await user_service.get_user_with_roles(str(current_user.id))