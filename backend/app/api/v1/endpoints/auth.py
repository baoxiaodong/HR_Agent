"""
认证 API 端点。

端点层只负责接收 HTTP 参数、注入数据库会话并把业务异常转换为 HTTP 状态码；
注册、密码校验、令牌签发和角色查询都委托给 ``UserService``。受保护端点会先执行
``get_current_user`` 依赖，因此业务函数拿到的是已经通过 JWT 校验且处于启用状态的用户。
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
    """校验公开注册数据并创建用户。

    服务层检查邮箱和用户名、哈希密码、提交用户后再尝试分配默认角色；角色关联是第二次提交，
    因此失败时可能保留无默认角色的用户。可预期重复字段错误映射为 400。
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
    """使用 OAuth2 表单中的用户名/邮箱和密码签发访问令牌。

    服务层依次查询用户名、邮箱并校验密码哈希，成功后提交最后登录时间并签发 JWT；凭据错误
    返回带 Bearer 认证头的 401，其他业务校验错误返回 400。
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
    """为已经通过 JWT 依赖验证的用户重新签发访问令牌。

    服务方法只根据认证上下文中的用户 ID 签名，不再次查询账号状态或角色变化；这些校验由
    ``get_current_user`` 在进入端点前完成。
    """
    user_service = UserService(db)
    return await user_service.refresh_user_token(str(current_user.id))


@router.get("/me")
async def get_current_user_info(
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """返回当前认证用户及其角色列表。

    JWT 依赖先确认用户处于启用状态，服务层随后按用户 ID 重新查询公开资料和角色关联，避免
    仅返回令牌中可能过期的声明。
    """
    user_service = UserService(db)
    return await user_service.get_user_with_roles(str(current_user.id))