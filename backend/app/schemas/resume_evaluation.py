"""
简历评价请求、AI 原始结果、数据库响应和前端结果 Schema。

评价链存在两次结构转换：Dify 返回 ``AIEvaluationResult`` 风格的混合字段名，服务层将其
规范化为 ``ResumeEvaluationResult``；数据库历史列表则使用包含文件与关联信息的
``ResumeEvaluationResponse``。这些模型只验证形状，不执行评分。
"""
from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field
from uuid import UUID


class EvaluationMetric(BaseModel):
    """AI 对一个评价维度给出的得分、满分和可解释理由。"""

    name: str = Field(..., description="指标名称")
    score: float = Field(..., description="得分")
    max: float = Field(..., description="满分")
    reason: str = Field(..., description="评分理由")


class ResumeEvaluationCreate(BaseModel):
    """发起评价时关联 JD、可选评分标准和对话上下文。"""

    job_description_id: UUID = Field(..., description="关联的JD ID")
    scoring_criteria_id: Optional[UUID] = Field(None, description="评分标准ID")
    conversation_id: Optional[str] = Field(None, description="对话ID")


class ResumeEvaluationUpdate(BaseModel):
    """人工修正候选人信息或评价结果时使用的局部更新字段。"""

    # 候选人画像
    candidate_name: Optional[str] = None
    candidate_position: Optional[str] = None
    candidate_age: Optional[int] = None
    candidate_gender: Optional[str] = None
    work_years: Optional[float] = None
    education_level: Optional[str] = None
    school: Optional[str] = None

    # 可人工调整的评价数据
    total_score: Optional[float] = None
    evaluation_metrics: Optional[List[EvaluationMetric]] = None


class ResumeEvaluationResponse(BaseModel):
    """数据库评价记录的公开投影，主要用于历史列表和详情。"""

    # 原始附件和提取文本
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
