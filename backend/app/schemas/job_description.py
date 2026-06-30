"""
    职位描述模式，用于API请求/响应验证
"""
from typing import Optional, Dict, Any
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field

from app.models.jd_status import JDStatus


class JobDescriptionBase(BaseModel):
    """职位描述基础模式"""
    title: str
    department: Optional[str] = None
    location: Optional[str] = None
    salary_range: Optional[str] = None
    experience_level: Optional[str] = None
    education: Optional[str] = None
    job_type: Optional[str] = None
    skills: Optional[list[str]] = None
    content: str
    requirements: Optional[str] = None
    status: Optional[str] = "draft"
    meta_data: Optional[Dict[str, Any]] = None
    conversation_id: Optional[str] = None
    workflow_type: Optional[str] = "jd_generation"


class JobDescriptionCreate(JobDescriptionBase):
    """创建新职位描述的模式"""
    pass


class JobDescriptionUpdate(BaseModel):
    """更新职位描述的模式"""
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    department: Optional[str] = Field(None, max_length=100)
    location: Optional[str] = Field(None, max_length=100)
    salary_range: Optional[str] = Field(None, max_length=100)
    experience_level: Optional[str] = Field(None, max_length=100)
    education: Optional[str] = Field(None, max_length=100)
    job_type: Optional[str] = Field(None, max_length=50)
    skills: Optional[list[str]] = None
    content: Optional[str] = Field(None, min_length=1)
    requirements: Optional[str] = None
    status: Optional[JDStatus] = None
    meta_data: Optional[Dict[str, Any]] = None


class JobDescriptionInDB(JobDescriptionBase):
    """数据库中的职位描述模式"""
    id: UUID
    user_id: UUID
    workflow_type: str
    created_at: datetime
    updated_at: datetime
    is_active: bool

    class Config:
        from_attributes = True


class JobDescriptionResponse(JobDescriptionInDB):
    """职位描述API响应模式"""
    pass


class JobDescriptionListResponse(BaseModel):
    """职位描述列表响应模式"""
    items: list[JobDescriptionResponse]
    total: int
    page: int
    size: int
    pages: int

class JDGenerateRequest(BaseModel):
    """职位描述(JD)生成请求模型"""
    requirements: str  # 职位要求描述
    position_title: Optional[str] = None  # 职位名称
    department: Optional[str] = None  # 所属部门
    experience_level: Optional[str] = None  # 经验等级
    conversation_id: Optional[str] = None  # 对话ID
    stream: bool = True  # 是否流式输出