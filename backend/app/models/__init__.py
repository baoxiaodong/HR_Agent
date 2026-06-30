"""
    数据库模型包
"""

# 导入模型
from app.models.base import BaseModel
from app.models.user import User
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.models.conversation import Conversation, Message
from app.models.resume_evaluation import ResumeEvaluation
from app.models.exam import Exam, Question
from app.models.exam_result import ExamResult

# 导出对应模型
__all__ = [
    "BaseModel",
    "User",
    "Document",
    "KnowledgeBase",
    "Conversation",
    "Message",
    "ResumeEvaluation",
    "Exam",
    "Question",
    "ExamResult"
]
