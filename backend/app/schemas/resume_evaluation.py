"""
    请求/响应验证的简历评估方案
"""
from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field
from uuid import UUID


class EvaluationMetric(BaseModel):
    """单个评价指标"""
    name: str = Field(..., description="指标名称")
    score: float = Field(..., description="得分")
    max: float = Field(..., description="满分")
    reason: str = Field(..., description="评分理由")


class ResumeEvaluationCreate(BaseModel):
    """创建简历评价的请求"""
    job_description_id: UUID = Field(..., description="关联的JD ID")
    scoring_criteria_id: Optional[UUID] = Field(None, description="评分标准ID")
    conversation_id: Optional[str] = Field(None, description="对话ID")


class ResumeEvaluationUpdate(BaseModel):
    """更新简历评价的请求"""
    candidate_name: Optional[str] = None
    candidate_position: Optional[str] = None
    candidate_age: Optional[int] = None
    candidate_gender: Optional[str] = None
    work_years: Optional[float] = None
    education_level: Optional[str] = None
    school: Optional[str] = None
    total_score: Optional[float] = None
    evaluation_metrics: Optional[List[EvaluationMetric]] = None


class ResumeEvaluationResponse(BaseModel):
    """简历评价响应"""
    id: UUID
    original_filename: str
    file_type: str
    resume_content: str
    
    # 候选人信息
    candidate_name: Optional[str] = None
    candidate_position: Optional[str] = None
    candidate_age: Optional[int] = None
    candidate_gender: Optional[str] = None
    work_years: Optional[float] = None
    education_level: Optional[str] = None
    school: Optional[str] = None
    
    # 评价结果
    total_score: Optional[float] = None
    evaluation_metrics: Optional[List[Dict[str, Any]]] = None
    
    # 关联信息
    job_description_id: UUID
    scoring_criteria_id: Optional[UUID] = None
    user_id: UUID
    
    # 时间戳
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class ResumeEvaluationListResponse(BaseModel):
    """简历评价列表响应"""
    items: List[ResumeEvaluationResponse]
    total: int
    page: int
    size: int
    pages: int


class ResumeUploadRequest(BaseModel):
    """简历上传请求"""
    job_description_id: UUID = Field(..., description="关联的JD ID")
    conversation_id: Optional[str] = Field(None, description="对话ID")


class AIEvaluationResult(BaseModel):
    """AI评价结果（Dify返回的格式）"""
    evaluation_metrics: List[EvaluationMetric]
    total_score: float
    name: Optional[str] = None
    position: Optional[str] = None
    workYears: Optional[float] = None
    教育水平: Optional[str] = None
    年龄: Optional[int] = None
    sex: Optional[str] = None
    school: Optional[str] = None


class ResumeEvaluationResult(BaseModel):
    """完整的简历评价结果（返回给前端）"""
    id: UUID
    evaluation_metrics: List[Dict[str, Any]]
    total_score: Optional[float]
    name: Optional[str]
    position: Optional[str]
    workYears: Optional[float]
    education: Optional[str]
    age: Optional[int]
    sex: Optional[str]
    school: Optional[str]
    resume_content: str
    original_filename: str
    created_at: str
    updated_at: str

class ExportZipRequest(BaseModel):
    resume_ids: List[UUID]
