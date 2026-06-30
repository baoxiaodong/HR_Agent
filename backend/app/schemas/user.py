"""
User相关的 schemas
"""
from datetime import datetime
from typing import Optional, List
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field

from app.models.user import UserRole


class UserBase(BaseModel):
    """Base user """
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    full_name: Optional[str] = Field(None, max_length=100)
    phone: Optional[str] = Field(None, max_length=20)
    department: Optional[str] = Field(None, max_length=100)
    position: Optional[str] = Field(None, max_length=100)
    employee_id: Optional[str] = Field(None, max_length=50)
    role: Optional[UserRole] = None
    bio: Optional[str] = Field(None, max_length=500)


class UserCreate(UserBase):
    """ 创建user"""
    password: str = Field(..., min_length=6, max_length=100)


class UserUpdate(BaseModel):
    """更新 user"""
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
    is_superuser: Optional[bool] = None
    is_verified: Optional[bool] = None
    is_active: Optional[bool] = None


class UserInDB(UserBase):
    """数据库中用户的 Schema"""
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
    name: str
    description: Optional[str] = None


class RoleCreate(RoleBase):
    is_builtin: Optional[bool] = False


class RoleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class Role(BaseModel):
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