"""
考生答卷与评分结果的数据库模型。

每条 ``ExamResult`` 保存试卷标识、考生信息和总分摘要，同时把试卷、答案及逐题评分整体
保存在 ``exam_data`` JSON 中，便于完整回放。``exam_id`` 当前只是 UUID 字段，不声明
数据库外键关系。
"""
from sqlalchemy import Column, String, Integer, Text, JSON, ForeignKey, Float, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class ExamResult(BaseModel):
    """考试结果模型"""
    
    __tablename__ = "exam_results"
    
    # 基本元数据
    exam_id = Column(UUID(as_uuid=True), nullable=False, comment="考试ID")
    exam_name = Column(String(200), nullable=False, comment="考试名称")
    student_name = Column(String(100), nullable=False, comment="考生姓名")
    department = Column(String(100), nullable=True, comment="考生部门")
    total_possible_score = Column(Float, nullable=False, comment="试卷总分")
    total_actual_score = Column(Float, nullable=False, comment="实际得分")
    
    # 完整考试数据（JSON格式，包含试卷、答案、评分等所有信息）
    exam_data = Column(JSON, nullable=False, comment="完整考试数据JSON，包含试卷内容、学生答案、评分结果等")
    
    # 提交时间
    submit_time = Column(DateTime, nullable=True, comment="提交时间")
    
    # 状态信息
    status = Column(String(20), nullable=False, default="completed", comment="考试状态")