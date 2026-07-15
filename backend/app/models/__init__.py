"""
数据库模型包的集中导出入口。

导入这些模型会把表元数据注册到共享 SQLAlchemy ``Base``，使应用初始化阶段能够统一
创建缺失表；``__all__`` 则明确允许其他模块从本包直接导入的公共模型。
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
