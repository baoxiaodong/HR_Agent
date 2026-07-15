"""
试卷生成、保存和答卷提交的请求 Schema。

``ExamGenerateRequest`` 描述给 AI 的出题配置，``ExamCreateRequest`` 还可携带已经生成的
原始内容和结构化题目用于持久化，``ExamSubmitRequest`` 则提交考生答案和完整试卷内容
供评分服务处理。
"""
from typing import Optional, Dict, Any, List
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field

class ExamGenerateRequest(BaseModel):
    """
    AI 出题配置。

    knowledge_files 指定参考材料，question_counts 按题型给出数量；stream 决定端点返回
    完整结果还是生成过程，但不改变最终试卷内容。
    """

    # 试卷基本信息
    title: str
    subject: str
    description: Optional[str] = None
    difficulty: Optional[str] = None
    duration: Optional[int] = None
    total_score: int

    # 出题约束与上下文
    question_types: Optional[List[str]] = None
    question_counts: Optional[Dict[str, int]] = None
    knowledge_files: Optional[List[Dict[str, Any]]] = None
    special_requirements: Optional[str] = None
    conversation_id: Optional[str] = None
    stream: bool = True


class ExamSubmitRequest(BaseModel):
    """公开考试页提交给评分服务的考生信息、逐题答案及试卷原文。"""
    exam_id: str  # 考试ID，用于标识唯一一场考试
    student_name: str  # 学生姓名，用于标识提交答案的学生
    department: str  # 学生所在院系，用于记录学生信息
    answers: Dict[str, Any]  # 学生答案，使用字典格式存储，键为题目ID，值为答案内容
    exam_content: str  # 试卷内容，包含完整的考试题目和要求


class ExamCreateRequest(BaseModel):
    """
    保存试卷时的完整数据。

    前半部分保留生成配置，content 保存可展示原文，questions 保存可编辑/评分的结构化题目。
    """

    # 生成配置快照
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

    # 已生成的实际试卷内容
    content: Optional[str] = None
    questions: Optional[List[Dict[str, Any]]] = None