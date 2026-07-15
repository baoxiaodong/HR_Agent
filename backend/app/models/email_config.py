"""
招聘邮箱配置与抓取运行日志模型。

``EmailConfig`` 同时保存 IMAP 读取参数、SMTP 发送参数和自动抓取计划；每次抓取产生一条
``EmailFetchLog``，记录发现邮件数、提取简历数和错误。删除配置时 ORM 级联删除其历史
日志，后台调度任务则由服务层另行停止。
"""
from sqlalchemy import Column, String, Integer, Boolean, Text, ForeignKey, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from datetime import datetime

from app.models.base import BaseModel


class EmailConfig(BaseModel):
    """招聘邮箱配置"""

    __tablename__ = "email_configs"

    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False, unique=True)

    # IMAP配置
    imap_server = Column(String(255), nullable=False)
    imap_port = Column(Integer, nullable=False, default=993)
    imap_ssl = Column(Boolean, nullable=False, default=True)

    # SMTP配置
    smtp_server = Column(String(255), nullable=True)
    smtp_port = Column(Integer, nullable=True, default=587)
    smtp_ssl = Column(Boolean, nullable=False, default=True)

    # 认证信息
    password = Column(String(512), nullable=True)

    # 抓取设置
    fetch_interval = Column(Integer, nullable=False, default=30)
    auto_fetch = Column(Boolean, nullable=False, default=False)
    # 用于过滤简历邮件的主题关键词（逗号分隔，支持全角/半角逗号）
    subject_keywords = Column(Text, nullable=True)

    # 状态信息
    status = Column(String(50), nullable=False, default="active")
    connection_status = Column(String(50), nullable=False, default="unknown")
    last_fetch_at = Column(DateTime, nullable=True)

    # 关联关系
    logs = relationship("EmailFetchLog", back_populates="email_config", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<EmailConfig(email='{self.email}', status='{self.status}', connection='{self.connection_status}')>"


class EmailFetchLog(BaseModel):
    """邮件抓取操作的日志"""

    __tablename__ = "email_fetch_logs"

    email_config_id = Column(UUID(as_uuid=True), ForeignKey("email_configs.id"), nullable=False, index=True)
    status = Column(String(50), nullable=False, default="running")
    emails_found = Column(Integer, nullable=False, default=0)
    resumes_extracted = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)

    email_config = relationship("EmailConfig", back_populates="logs")

    def __repr__(self):
        return f"<EmailFetchLog(config='{self.email_config_id}', status='{self.status}', found={self.emails_found})>"

