"""
用于存储简历信息和评价结果的简历评价模型
"""
from sqlalchemy import Column, String, Text, JSON, ForeignKey, Integer, Float, Enum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import enum

from app.models.base import BaseModel


class ResumeStatus(enum.Enum):
    """简历状态枚举"""
    PENDING = "pending"  # 待处理
    REJECTED = "rejected"  # 不通过
    INTERVIEW = "interview"  # 面试


class ResumeEvaluation(BaseModel):
    """用于存储简历信息和评价结果的简历评价模型"""

    __tablename__ = "resume_evaluations"

    # 文件信息
    email_id = Column(String(20), nullable= True) #邮件唯一ID,用于判断邮件简历接收去重
    original_filename = Column(String(255), nullable=False)  # 原始文件名
    file_path = Column(String(500), nullable=True)  # 文件存储路径
    file_type = Column(String(10), nullable=False)  # 文件类型 (pdf, txt, doc, docx)
    file_size = Column(Integer, nullable=True)  # 文件大小（字节）

    # 简历文本内容
    resume_content = Column(Text, nullable=False)  # 解析后的简历文本内容

    # 候选人基本信息（从简历中提取）
    candidate_name = Column(String(100), nullable=True)  # 姓名
    candidate_position = Column(String(200), nullable=True)  # 应聘岗位
    candidate_age = Column(Integer, nullable=True)  # 年龄
    candidate_gender = Column(String(10), nullable=True)  # 性别
    work_years = Column(Float, nullable=True)  # 工作经验（年，数字）
    education_level = Column(String(100), nullable=True)  # 教育水平
    school = Column(String(200), nullable=True)  # 学校

    # 评价结果
    total_score = Column(Float, nullable=True)  # 总分
    evaluation_metrics = Column(JSON, nullable=True)  # 详细评价指标

    # 简历状态
    status = Column(Enum(ResumeStatus), nullable=False, default=ResumeStatus.PENDING)  # 简历状态
    """
    evaluation_metrics 格式示例:
    [
        {
            "name": "学历",
            "score": 15,
            "max": 20,
            "reason": "本科学历符合要求"
        },
        {
            "name": "工作经验",
            "score": 18,
            "max": 20,
            "reason": "3年相关工作经验"
        }
    ]
    """

    # 评价上下文
    job_description_id = Column(UUID(as_uuid=True), nullable=False)
    scoring_criteria_id = Column(UUID(as_uuid=True), nullable=True)
    conversation_id = Column(String(255), nullable=True)  # 对话ID

    # AI评价的原始响应
    ai_response = Column(JSON, nullable=True)  # Dify返回的完整响应

    # 关联关系
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    # job_description = relationship("JobDescription")
    # scoring_criteria = relationship("ScoringCriteria")
    created_by_user = relationship("User")

    # 关联关系
    # interview_plans = relationship("InterviewPlan", back_populates="resume_evaluation")

    def __repr__(self):
        return f"<ResumeEvaluation(id={self.id}, candidate_name='{self.candidate_name}', total_score={self.total_score})>"

    def to_evaluation_result(self):
        """转换为前端需要的评价结果格式"""
        return {
            "id": str(self.id),
            "evaluation_metrics": self.evaluation_metrics or [],
            "total_score": self.total_score,
            "name": self.candidate_name,
            "position": self.candidate_position,
            "workYears": self.work_years,
            "教育水平": self.education_level,
            "年龄": self.candidate_age,
            "sex": self.candidate_gender,
            "school": self.school,
            "resume_content": self.resume_content,
            "original_filename": self.original_filename,
            "status": self.status.value if self.status else "pending",
            "created_at": self.created_at.isoformat() if self.created_at else None
        }