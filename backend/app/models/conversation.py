"""
用于存储聊天历史和AI交互的对话模型
"""
from sqlalchemy import Column, String, Text, ForeignKey, JSON, Enum, Float, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import enum

from app.models.base import BaseModel


class MessageRole(str, enum.Enum):
    """消息角色枚举"""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ConversationStatus(str, enum.Enum):
    """对话状态枚举"""
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class Conversation(BaseModel):
    """用于存储聊天会话的对话模型"""

    __tablename__ = "conversations"

    # 基本信息
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(Enum(ConversationStatus), default=ConversationStatus.ACTIVE, nullable=False)

    # 元数据
    meta_data = Column(JSON, nullable=True)
    total_messages = Column(Integer, default=0, nullable=False)

    # 关系
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    user = relationship("User", back_populates="conversations")

    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Conversation(title='{self.title}', user_id='{self.user_id}')>"


class Message(BaseModel):
    """用于在对话中存储单条消息的消息模型"""

    __tablename__ = "messages"

    model_config = {"protected_namespaces": ()}

    # 内容
    content = Column(Text, nullable=False)
    role = Column(Enum(MessageRole), nullable=False)

    # AI特定字段
    model_name = Column(String(100), nullable=True)  # 例如："gpt-3.5-turbo"
    tokens_used = Column(Integer, nullable=True)
    response_time = Column(Float, nullable=True)  # 响应时间（秒）

    # 附加上下文和元数据
    context = Column(JSON, nullable=True)  # 用于生成的附加上下文
    meta_data = Column(JSON, nullable=True)  # 消息特定的元数据

    # 反馈
    rating = Column(Integer, nullable=True)  # 用户评分（1-5）
    feedback = Column(Text, nullable=True)  # 用户反馈

    # 关系
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False)
    conversation = relationship("Conversation", back_populates="messages")

    parent_message_id = Column(UUID(as_uuid=True), ForeignKey("messages.id"), nullable=True)
    parent_message = relationship("Message", remote_side="Message.id")

    def __repr__(self):
        return f"<Message(role='{self.role}', conversation_id='{self.conversation_id}')>"