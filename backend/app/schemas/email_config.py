"""
    招聘邮箱配置和日志的Schema
"""
from datetime import datetime
from typing import Optional, List
from uuid import UUID
from pydantic import BaseModel, Field, EmailStr


class EmailConfigBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    email: EmailStr
    imap_server: str = Field(..., min_length=1, max_length=255)
    imap_port: int = Field(993, ge=1, le=65535)
    imap_ssl: bool = True
    smtp_server: Optional[str] = Field(None, max_length=255)
    smtp_port: Optional[int] = Field(587, ge=1, le=65535)
    smtp_ssl: bool = True
    fetch_interval: int = Field(30, ge=1, le=1440)
    auto_fetch: bool = False
    status: str = Field("active")
    # 逗号分隔的主题过滤关键词；支持全角/半角逗号
    subject_keywords: Optional[str] = None


class EmailConfigCreate(EmailConfigBase):
    password: str = Field(..., min_length=1, max_length=512)


class EmailConfigUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    email: Optional[EmailStr] = None
    imap_server: Optional[str] = Field(None, min_length=1, max_length=255)
    imap_port: Optional[int] = Field(None, ge=1, le=65535)
    imap_ssl: Optional[bool] = None
    smtp_server: Optional[str] = Field(None, max_length=255)
    smtp_port: Optional[int] = Field(None, ge=1, le=65535)
    smtp_ssl: Optional[bool] = None
    fetch_interval: Optional[int] = Field(None, ge=1, le=1440)
    auto_fetch: Optional[bool] = None
    status: Optional[str] = None
    password: Optional[str] = Field(None, min_length=0, max_length=512)
    subject_keywords: Optional[str] = None


class EmailConfigInDB(EmailConfigBase):
    id: UUID
    connection_status: str
    last_fetch_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EmailConfig(EmailConfigInDB):
    pass


class EmailConnectionTest(BaseModel):
    imap_server: str
    imap_port: int
    imap_ssl: bool
    email: EmailStr
    password: str


class EmailFetchLogBase(BaseModel):
    status: str
    emails_found: int
    resumes_extracted: int
    error_message: Optional[str] = None


class EmailFetchLog(EmailFetchLogBase):
    id: UUID
    email_config_id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EmailFetchLogList(BaseModel):
    items: List[EmailFetchLog]

class AutoEvaluateRequest(BaseModel):
    resume_text: str = Field(..., description="简历文本内容")
    position: Optional[str] = Field("", description="投递邮件主题，用于辅助匹配JD")
    filename: Optional[str] = Field(None, description="保存的文件名，默认为生成的 .txt 文件")
    login_name: str = Field(..., description="用户登录名称")
