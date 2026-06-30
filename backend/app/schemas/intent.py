from pydantic import BaseModel
from typing import Any, Optional, Dict, List

class IntentRouteRequest(BaseModel):
    query: str

class IntentRouteResponse(BaseModel):
    intent: str
    route: str
    query: str = ""

# 新增：需求解析请求/响应模型
class RequirementParseRequest(BaseModel):
    text: str
    conversation_id: Optional[str] = None

class RequirementParseResponse(BaseModel):
    job_title: Optional[str] = None
    department: Optional[str] = None
    location: Optional[str] = None
    salary: Optional[str] = None
    experience: Optional[str] = None
    education: Optional[str] = None
    job_type: Optional[str] = None
    skills: Optional[List[str]] = None
    benefits: Optional[List[str]] = None
    additional_requirements: Optional[str] = None

# 新增：试卷意图解析请求/响应模型
class ExamIntentParseRequest(BaseModel):
    text: str
    conversation_id: Optional[str] = None

# 知识库文件信息模型
class KnowledgeFileInfo(BaseModel):
    id: str
    fileName: Optional[str] = None

class ExamIntentParseResponse(BaseModel):
    title: Optional[str] = None
    subject: Optional[str] = None
    total_score: Optional[int] = 100
    difficulty: Optional[str] = "medium"  # easy/medium/hard
    duration: Optional[int] = 90
    question_counts: Dict[str, int] = {}
    special_requirements: Optional[str] = None
    knowledge_files: List[KnowledgeFileInfo] = []