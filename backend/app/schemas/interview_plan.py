"""
面试方案生成、保存与分页响应 Schema。

生成请求引用当前用户可见的简历评价；服务层据此组织 LLM 输入，生成的自由文本再通过保存
请求写入远程 HR 服务。Schema 负责 UUID、必填字段和响应形状校验，不负责证明简历评价或
返回方案归属于当前认证用户。
"""
from typing import Optional, Dict, Any, List
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field




class InterviewPlanBase(BaseModel):
    """面试方案基础模型"""
    candidate_name: str = Field(..., description="候选人姓名")
    candidate_position: str = Field(..., description="应聘岗位")
    content: str = Field(..., description="面试方案内容")


class InterviewPlanCreate(InterviewPlanBase):
    """创建面试方案的请求模型"""
    resume_evaluation_id: UUID = Field(..., description="关联的简历评价ID")


class InterviewPlanUpdate(BaseModel):
    """更新面试方案的请求模型"""
    candidate_name: Optional[str] = Field(None, description="候选人姓名")
    candidate_position: Optional[str] = Field(None, description="应聘岗位")
    content: Optional[str] = Field(None, description="面试方案内容")


class InterviewPlanResponse(InterviewPlanBase):
    """面试方案响应模型"""
    id: UUID = Field(..., description="面试方案ID")
    resume_evaluation_id: UUID = Field(..., description="关联的简历评价ID")
    user_id: UUID = Field(..., description="创建用户ID")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")

    class Config:
        from_attributes = True


class InterviewPlanListResponse(BaseModel):
    """面试方案列表响应模型"""
    items: List[InterviewPlanResponse] = Field(..., description="面试方案列表")
    total: int = Field(..., description="总数量")
    page: int = Field(..., description="当前页码")
    size: int = Field(..., description="每页大小")
    pages: int = Field(..., description="总页数")


class InterviewPlanSaveRequest(BaseModel):
    """保存面试方案内容的请求模型"""
    content: str = Field(..., description="面试方案内容")
    candidate_name: Optional[str] = Field(None, description="候选人姓名")
    candidate_position: Optional[str] = Field(None, description="应聘岗位")


class InterviewPlanGenerateRequest(BaseModel):
    """生成面试方案的请求模型"""
    resume_evaluation_id: UUID = Field(..., description="简历评价ID")
    candidate_name: str = Field(..., description="候选人姓名")
    candidate_position: str = Field(..., description="应聘岗位")