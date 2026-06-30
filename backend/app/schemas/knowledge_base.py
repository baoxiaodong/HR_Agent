"""
  知识库相关的schemas
"""
from datetime import datetime
from typing import Optional, Dict, Any, List
from uuid import UUID
from pydantic import BaseModel, Field


class KnowledgeBaseBase(BaseModel):
    """Base knowledge base schema"""
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    is_public: bool = False
    is_searchable: bool = True
    category: Optional[str] = Field(None, max_length=100)
    tags: Optional[List[str]] = None
    meta_data: Optional[Dict[str, Any]] = None


class KnowledgeBaseCreate(KnowledgeBaseBase):
    """Schema for creating a knowledge base"""
    pass


class KnowledgeBaseUpdate(BaseModel):
    """Schema for updating a knowledge base"""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    is_public: Optional[bool] = None
    is_searchable: Optional[bool] = None
    category: Optional[str] = Field(None, max_length=100)
    tags: Optional[List[str]] = None
    meta_data: Optional[Dict[str, Any]] = None


class KnowledgeBaseInDB(KnowledgeBaseBase):
    """Schema for knowledge base in database"""
    id: UUID
    document_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class KnowledgeBase(KnowledgeBaseInDB):
    """Public knowledge base schema"""
    pass


class KnowledgeBaseSearch(BaseModel):
    """Schema for knowledge base search"""
    query: str = Field(..., min_length=1, max_length=500)
    limit: Optional[int] = Field(10, ge=1, le=50)


class KnowledgeBaseSearchResult(BaseModel):
    """Schema for knowledge base search result"""
    knowledge_base: Dict[str, Any]
    documents: List[Dict[str, Any]]
    faqs: List[Dict[str, Any]]


class FAQBase(BaseModel):
    """Base FAQ schema"""
    question: str = Field(..., min_length=1, max_length=500)
    answer: str = Field(..., min_length=1, max_length=2000)
    category: Optional[str] = Field(None, max_length=100)
    tags: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None


class FAQCreate(FAQBase):
    """Schema for creating an FAQ"""
    knowledge_base_id: Optional[UUID] = None


class FAQUpdate(BaseModel):
    """Schema for updating an FAQ"""
    question: Optional[str] = Field(None, min_length=1, max_length=500)
    answer: Optional[str] = Field(None, min_length=1, max_length=2000)
    category: Optional[str] = Field(None, max_length=100)
    tags: Optional[List[str]] = None
    knowledge_base_id: Optional[UUID] = None
    metadata: Optional[Dict[str, Any]] = None


class FAQInDB(FAQBase):
    """Schema for FAQ in database"""
    id: UUID
    knowledge_base_id: Optional[UUID]
    view_count: int
    helpful_count: int
    not_helpful_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class FAQ(FAQInDB):
    """Public FAQ schema"""
    pass


class FAQFeedback(BaseModel):
    """Schema for FAQ feedback"""
    is_helpful: bool