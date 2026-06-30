"""
    聊天相关的Schema
"""
from datetime import datetime
from typing import Optional, Dict, Any, List
from uuid import UUID
from pydantic import BaseModel, Field

from app.models.conversation import MessageRole


class ChatMessage(BaseModel):
    """Schema for chat message"""
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: Optional[UUID] = None
    context: Optional[Dict[str, Any]] = None


class ChatRequest(BaseModel):
    """Schema for chat request"""
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: Optional[UUID] = None
    context: Optional[Dict[str, Any]] = None


class ChatResponse(BaseModel):
    """Schema for chat response"""
    message_id: str
    conversation_id: str
    content: str
    role: MessageRole
    timestamp: datetime
    meta_data: Optional[Dict[str, Any]] = None


class ChatStreamMessage(BaseModel):
    """Schema for streaming chat message"""
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: Optional[UUID] = None
    context: Optional[Dict[str, Any]] = None


class ChatSuggestionRequest(BaseModel):
    """Schema for chat suggestion request"""
    query: str = Field(..., min_length=1, max_length=200)
    limit: Optional[int] = Field(5, ge=1, le=10)


class ChatSuggestionResponse(BaseModel):
    """Schema for chat suggestion response"""
    suggestions: List[str]


class ChatFeedback(BaseModel):
    """Schema for chat feedback"""
    message_id: str
    rating: int = Field(..., ge=1, le=5)
    feedback: Optional[str] = Field(None, max_length=500)


class ChatContext(BaseModel):
    """Schema for chat context"""
    search_documents: bool = True
    knowledge_base_id: Optional[UUID] = None
    include_history: bool = True
    max_history_messages: Optional[int] = Field(10, ge=1, le=50)