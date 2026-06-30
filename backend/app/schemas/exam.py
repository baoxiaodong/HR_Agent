"""
    试卷相关的 Schema
"""
from typing import Optional, Dict, Any, List
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field

# 试卷生成请求模型
class ExamGenerateRequest(BaseModel):
    """试卷生成请求模型"""
    title: str
    subject: str
    description: Optional[str] = None
    difficulty: Optional[str] = None
    duration: Optional[int] = None
    total_score: int
    question_types: Optional[List[str]] = None
    question_counts: Optional[Dict[str, int]] = None
    knowledge_files: Optional[List[Dict[str, Any]]] = None
    special_requirements: Optional[str] = None
    conversation_id: Optional[str] = None
    stream: bool = True

# 考试提交请求模型
class ExamSubmitRequest(BaseModel):
    exam_id: str  # 考试ID，用于标识唯一一场考试
    student_name: str  # 学生姓名，用于标识提交答案的学生
    department: str  # 学生所在院系，用于记录学生信息
    answers: Dict[str, Any]  # 学生答案，使用字典格式存储，键为题目ID，值为答案内容
    exam_content: str  # 试卷内容，包含完整的考试题目和要求


# 试卷创建请求模型
class ExamCreateRequest(BaseModel):
    """试卷创建请求模型"""
    title: str
    subject: str
    description: Optional[str] = None
    difficulty: Optional[str] = None
    duration: Optional[int] = None
    total_score: int
    question_types: Optional[List[str]] = None
    question_counts: Optional[Dict[str, int]] = None
    knowledge_files: Optional[List[Dict[str, Any]]] = None
    special_requirements: Optional[str] = None
    content: Optional[str] = None
    questions: Optional[List[Dict[str, Any]]] = None