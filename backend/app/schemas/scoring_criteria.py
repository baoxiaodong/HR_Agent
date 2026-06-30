"""
    评分标准API请求/响应验证的Schema定义
"""
from typing import Optional, Dict, Any, List
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field

from app.models.scoring_status import ScoringStatus


class ScoringCriteriaBase(BaseModel):
    """评分标准基础Schema"""
    title: str
    job_title: Optional[str] = None
    content: str
    criteria_data: Optional[Dict[str, Any]] = None
    total_score: Optional[str] = "100"
    scoring_dimensions: Optional[List[Dict[str, Any]]] = None
    status: Optional[str] = "draft"
    meta_data: Optional[Dict[str, Any]] = None
    conversation_id: Optional[str] = None
    workflow_type: Optional[str] = "scoring_criteria_generation"
    job_description_id: Optional[UUID] = None


class ScoringCriteriaCreate(ScoringCriteriaBase):
    """创建新评分标准的Schema"""
    pass


class ScoringCriteriaUpdate(BaseModel):
    """更新评分标准的Schema"""
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    job_title: Optional[str] = Field(None, max_length=255)
    content: Optional[str] = Field(None, min_length=1)
    criteria_data: Optional[Dict[str, Any]] = None
    total_score: Optional[str] = Field(None, max_length=10)
    scoring_dimensions: Optional[List[Dict[str, Any]]] = None
    status: Optional[ScoringStatus] = None
    meta_data: Optional[Dict[str, Any]] = None
    job_description_id: Optional[UUID] = None


class ScoringCriteriaInDB(ScoringCriteriaBase):
    """数据库中评分标准的Schema"""
    id: UUID
    user_id: UUID
    workflow_type: str
    created_at: datetime
    updated_at: datetime
    is_active: bool

    class Config:
        from_attributes = True


class ScoringCriteriaResponse(ScoringCriteriaInDB):
    """评分标准API响应的Schema"""
    pass


class ScoringCriteriaListResponse(BaseModel):
    """评分标准列表响应的Schema"""
    items: List[ScoringCriteriaResponse]
    total: int
    page: int
    size: int
    pages: int

class ScoringCriteriaGenerateRequest(BaseModel):
    """评分标准生成请求模型"""
    jd_content: str  # 招聘内容(JD)
    job_title: Optional[str] = None  # 职位标题
    requirements: Optional[Dict[str, Any]] = None  # 职位要求
    conversation_id: Optional[str] = None  # 对话ID
    stream: bool = True  # 是否流式返回结果