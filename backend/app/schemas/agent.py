"""
HR Agent 请求/响应模型
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AgentAttachment(BaseModel):
    """聊天附件元信息，用于 Agent 规划前置条件"""
    name: str
    size: Optional[int] = None
    content_type: Optional[str] = None


class AgentChatRequest(BaseModel):
    """HR Agent 对话请求"""
    message: str = Field(..., min_length=1, description="用户自然语言需求")
    conversation_id: Optional[str] = None
    auto_execute: bool = Field(True, description="是否自动执行低风险生成类工具")
    confirmed_requirements: Optional[Dict[str, Any]] = Field(None, description="用户确认后的结构化招聘需求")
    attachments: List[AgentAttachment] = Field(default_factory=list, description="聊天消息中携带的附件元信息")


class AgentStep(BaseModel):
    """Agent 执行步骤"""
    id: str
    title: str
    status: str
    detail: Optional[str] = None
    tool: Optional[str] = None


class AgentArtifact(BaseModel):
    """Agent 产物"""
    type: str
    title: str
    content: Any
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AgentChatResponse(BaseModel):
    """HR Agent 对话响应"""
    message: str
    intent: str
    route: Optional[str] = None
    steps: List[AgentStep] = Field(default_factory=list)
    artifacts: List[AgentArtifact] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)
    requires_confirmation: bool = False
    missing_fields: List[str] = Field(default_factory=list)
