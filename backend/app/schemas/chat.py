"""
普通聊天、流式聊天、建议和反馈的传输模型。

Schema 只约束客户端与 API 之间的数据形状；是否创建会话、是否检索文档、如何调用模型
由 ``ChatService`` 决定。``ChatContext`` 中的开关作为服务编排提示，不直接代表权限。
"""
from datetime import datetime
from typing import Optional, Dict, Any, List
from uuid import UUID
from pydantic import BaseModel, Field

from app.models.conversation import MessageRole


class ChatMessage(BaseModel):
    """通用消息输入结构；保留用于兼容早期调用方。"""

    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: Optional[UUID] = None
    context: Optional[Dict[str, Any]] = None


class ChatRequest(BaseModel):
    """``/chat/send`` 与 ``/chat/stream`` 的请求体。"""

    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: Optional[UUID] = None
    context: Optional[Dict[str, Any]] = None


class ChatResponse(BaseModel):
    """非流式聊天完成后返回的已持久化助手消息。"""

    message_id: str
    conversation_id: str
    content: str
    role: MessageRole
    timestamp: datetime
    meta_data: Optional[Dict[str, Any]] = None


class ChatStreamMessage(BaseModel):
    """流式聊天输入结构；输出不走此 Schema，而是逐块 SSE 文本。"""

    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: Optional[UUID] = None
    context: Optional[Dict[str, Any]] = None


class ChatSuggestionRequest(BaseModel):
    """根据短查询生成后续问题建议，limit 被限制在 1 到 10。"""

    query: str = Field(..., min_length=1, max_length=200)
    limit: Optional[int] = Field(5, ge=1, le=10)


class ChatSuggestionResponse(BaseModel):
    """建议接口的字符串列表响应。"""

    suggestions: List[str]


class ChatFeedback(BaseModel):
    """用户对一条已存在消息的 1 到 5 分评价。"""

    message_id: str
    rating: int = Field(..., ge=1, le=5)
    feedback: Optional[str] = Field(None, max_length=500)


class ChatContext(BaseModel):
    """控制聊天服务是否检索文档、限定知识库及携带多少历史消息。"""

    search_documents: bool = True
    knowledge_base_id: Optional[UUID] = None
    include_history: bool = True
    max_history_messages: Optional[int] = Field(10, ge=1, le=50)