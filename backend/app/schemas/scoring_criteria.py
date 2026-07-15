"""
简历评分标准生成、持久化和分页响应 Schema。

生成请求携带 JD 原文及职位要求，Create/Update 模型描述可保存字段，Response 模型补充
远程资源的 ID、用户、工作流和审计信息。``scoring_dimensions`` 保留为字典列表，以兼容
模型生成的不同评分维度结构。
"""
from typing import Optional, Dict, Any, List
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field

from app.models.scoring_status import ScoringStatus


class ScoringCriteriaBase(BaseModel):
    """评分标准正文、结构化维度和来源上下文的共用字段。"""

    # 用户可读内容
    title: str
    job_title: Optional[str] = None
    content: str

    # AI 生成的结构化评分数据
    criteria_data: Optional[Dict[str, Any]] = None
    total_score: Optional[str] = "100"
    scoring_dimensions: Optional[List[Dict[str, Any]]] = None
    status: Optional[str] = "draft"

    # 追踪评分标准来自哪个会话、工作流和 JD。
    meta_data: Optional[Dict[str, Any]] = None
    conversation_id: Optional[str] = None
    workflow_type: Optional[str] = "scoring_criteria_generation"
    job_description_id: Optional[UUID] = None


class ScoringCriteriaCreate(ScoringCriteriaBase):
    """把生成结果首次保存到远程服务时使用。"""
    pass


class ScoringCriteriaUpdate(BaseModel):
    """编辑评分标准时的局部字段，未提交项不覆盖旧数据。"""
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
    """远程服务持久化后补充资源归属、审计时间和软删除状态。"""
    id: UUID
    user_id: UUID
    workflow_type: str
    created_at: datetime
    updated_at: datetime
    is_active: bool

    class Config:
        from_attributes = True


class ScoringCriteriaResponse(ScoringCriteriaInDB):
    """评分标准详情接口的最终响应结构。"""
    pass


class ScoringCriteriaListResponse(BaseModel):
    """标准分页结构：当前页数据、总数、页码、页大小和总页数。"""
    items: List[ScoringCriteriaResponse]
    total: int
    page: int
    size: int
    pages: int


class ScoringCriteriaGenerateRequest(BaseModel):
    """把 JD 内容和要求提交给 Dify 生成评分标准。"""

    jd_content: str  # 评分标准最主要的事实来源
    job_title: Optional[str] = None
    requirements: Optional[Dict[str, Any]] = None
    conversation_id: Optional[str] = None
    stream: bool = True  # True 返回生成过程，False 等待完整结果