"""
认证接口的请求与令牌响应 Schema。

这些 Pydantic 模型在进入端点前完成邮箱格式、密码最短长度等校验；注册模型包含明文密码，
但仅用于请求传输，服务层会立即哈希，任何公开用户响应都不从本模块返回密码字段。
"""
from typing import Optional
from pydantic import BaseModel, EmailStr, field_validator


class UserLogin(BaseModel):
    """邮箱与明文密码登录输入；密码只在请求处理期间存在。"""

    email: EmailStr
    password: str


class UserRegister(BaseModel):
    """用户自助注册输入，联系信息和组织信息均可选。"""

    # 登录凭据
    username: str
    email: EmailStr
    password: str

    # 可选个人资料
    full_name: Optional[str] = None
    phone: Optional[str] = None
    department: Optional[str] = None
    position: Optional[str] = None
    employee_id: Optional[str] = None
    bio: Optional[str] = None

    @field_validator("password")
    @classmethod
    def validate_password(cls, v):
        """在进入用户服务前拒绝少于 6 个字符的密码。"""
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters long")
        return v


class UserCreate(BaseModel):
    """管理员或注册服务创建用户时使用，字段与注册请求保持兼容。"""
    username: str
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    phone: Optional[str] = None
    department: Optional[str] = None
    position: Optional[str] = None
    employee_id: Optional[str] = None
    bio: Optional[str] = None

    @field_validator("password")
    @classmethod
    def validate_password(cls, v):
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters long")
        return v


class Token(BaseModel):
    """登录或刷新成功后的 Bearer Token 响应及秒级有效期。"""
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class TokenData(BaseModel):
    """JWT 解码后的可选身份字段，不直接作为公开接口响应。"""
    user_id: Optional[str] = None
    email: Optional[str] = None


class PasswordReset(BaseModel):
    """发起密码重置时用于定位账号的邮箱。"""
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    """携带一次性重置令牌和新密码的确认请求。"""
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v):
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters long")
        return v


class ChangePassword(BaseModel):
    """已认证用户修改密码时提交旧密码和新密码。"""
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v):
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters long")
        return v