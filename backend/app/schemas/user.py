"""
用户资料、认证结果和角色管理 Schema。

``UserCreate`` 定义注册必填字段，``UserUpdate`` 的字段均可选以支持局部更新，``User`` 和
``UserWithRoles`` 用于对外响应且不包含密码哈希。角色 Schema 同时支持旧单值枚举和动态
多角色列表，与数据库模型中的两套角色表示对应。
"""
from datetime import datetime
from typing import Optional, List
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field

from app.models.user import UserRole


class UserBase(BaseModel):
    """创建请求和公开响应共用的账号、组织及个人资料字段。"""

    # 登录标识
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr

    # 个人和组织资料
    full_name: Optional[str] = Field(None, max_length=100)
    phone: Optional[str] = Field(None, max_length=20)
    department: Optional[str] = Field(None, max_length=100)
    position: Optional[str] = Field(None, max_length=100)
    employee_id: Optional[str] = Field(None, max_length=50)
    role: Optional[UserRole] = None
    bio: Optional[str] = Field(None, max_length=500)


class UserCreate(UserBase):
    """创建账号时额外接收明文密码，服务层会在落库前哈希。"""
    password: str = Field(..., min_length=6, max_length=100)


class UserUpdate(BaseModel):
    """
    用户局部更新输入。

    字段全部可选；服务层结合当前操作者权限决定哪些敏感字段实际允许修改。
    """

    # 基础资料
    username: Optional[str] = Field(None, min_length=3, max_length=50)
    email: Optional[EmailStr] = None
    full_name: Optional[str] = Field(None, max_length=100)
    phone: Optional[str] = Field(None, max_length=20)
    department: Optional[str] = Field(None, max_length=100)
    position: Optional[str] = Field(None, max_length=100)
    employee_id: Optional[str] = Field(None, max_length=50)
    role: Optional[UserRole] = None
    bio: Optional[str] = Field(None, max_length=500)
    password: Optional[str] = Field(None, min_length=6, max_length=100)
    avatar_url: Optional[str] = None

    # 账号状态字段不能仅依赖 Schema，应由服务层继续做管理员权限校验。
    is_superuser: Optional[bool] = None
    is_verified: Optional[bool] = None
    is_active: Optional[bool] = None


class UserInDB(UserBase):
    """从 ORM 用户对象投影出的公开持久化字段，不包含 hashed_password。"""
    id: UUID
    is_active: bool
    is_superuser: bool
    is_verified: bool
    avatar_url: Optional[str]
    last_login: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class User(UserInDB):
    """公共user (不包括密码)"""
    pass


class UserLogin(BaseModel):
    """ user 登录"""
    email: EmailStr
    password: str


class UserRegister(UserCreate):
    """ user 注册"""
    pass


class Token(BaseModel):
    """  authentication token 验证"""
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class TokenData(BaseModel):
    """ token data"""
    user_id: Optional[UUID] = None


class RoleBase(BaseModel):
    """角色名称和说明的公共输入字段。"""
    name: str
    description: Optional[str] = None


class RoleCreate(RoleBase):
    """创建角色时可标记是否为系统内置角色。"""
    is_builtin: Optional[bool] = False


class RoleUpdate(BaseModel):
    """角色名称或说明的局部更新。"""
    name: Optional[str] = None
    description: Optional[str] = None


class Role(BaseModel):
    """角色列表接口返回的持久化字段。"""
    id: UUID
    name: str
    description: Optional[str]
    is_builtin: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AssignRolesRequest(BaseModel):
    role_ids: List[UUID]


class UserWithRoles(BaseModel):
    id: UUID
    username: str
    email: EmailStr
    full_name: Optional[str]
    phone: Optional[str]
    department: Optional[str]
    position: Optional[str]
    employee_id: Optional[str]
    role: Optional[UserRole]
    is_superuser: bool
    is_verified: bool
    is_active: bool
    bio: Optional[str]
    avatar_url: Optional[str]
    last_login: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    roles: List[Role]