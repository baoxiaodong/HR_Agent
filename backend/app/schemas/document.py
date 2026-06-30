"""
    Document相关的 schemas
"""
from datetime import datetime
from typing import Optional, Dict, Any, List
from uuid import UUID
import numpy as np
from pydantic import BaseModel, Field, field_validator


class DocumentBase(BaseModel):
    """Base document schema"""
    filename: str = Field(..., min_length=1, max_length=255)
    category: Optional[str] = Field(None, max_length=100)
    tags: Optional[List[str]] = None
    meta_data: Optional[Dict[str, Any]] = None


class DocumentCreate(DocumentBase):
    """Schema for creating a document"""
    knowledge_base_id: Optional[UUID] = None


class DocumentUpdate(BaseModel):
    """Schema for updating a document"""
    filename: Optional[str] = Field(None, min_length=1, max_length=255)
    category: Optional[str] = Field(None, max_length=100)
    tags: Optional[List[str]] = None
    knowledge_base_id: Optional[UUID] = None
    meta_data: Optional[Dict[str, Any]] = None


class DocumentInDB(DocumentBase):
    """Schema for document in database"""
    id: UUID
    user_id: UUID
    knowledge_base_id: Optional[UUID]
    file_path: str
    file_size: int
    file_hash: str
    mime_type: str
    extracted_content: Optional[str]
    embedding: Optional[List[float]] = None
    created_at: datetime
    updated_at: datetime

    @field_validator('embedding', mode='before')
    @classmethod
    def convert_embedding(cls, v):
        """Convert numpy array to list for JSON serialization"""
        if v is None:
            return None
        if isinstance(v, np.ndarray):
            return v.tolist()
        return v

    class Config:
        from_attributes = True


class Document(DocumentInDB):
    """Public document schema"""
    pass


class DocumentUpload(BaseModel):
    """Schema for document upload"""
    knowledge_base_id: Optional[UUID] = None
    category: Optional[str] = Field(None, max_length=100)
    tags: Optional[List[str]] = None


class DocumentSearch(BaseModel):
    """Schema for document search"""
    query: str = Field(..., min_length=1, max_length=500)
    knowledge_base_id: Optional[UUID] = None
    category: Optional[str] = None
    limit: Optional[int] = Field(10, ge=1, le=50)


class DocumentSearchResult(BaseModel):
    """Schema for document search result"""
    id: str
    filename: str
    content: str
    category: Optional[str]
    tags: List[str]
    created_at: str
    relevance_score: Optional[float] = None


class DocumentChunkBase(BaseModel):
    """Base document chunk schema"""
    content: str
    chunk_index: int
    chunk_size: int
    meta_data: Optional[Dict[str, Any]] = None


class DocumentChunkInDB(DocumentChunkBase):
    """Schema for document chunk in database"""
    id: UUID
    document_id: UUID
    embedding: Optional[List[float]] = None
    created_at: datetime
    updated_at: datetime

    @field_validator('embedding', mode='before')
    @classmethod
    def convert_embedding(cls, v):
        """Convert numpy array to list for JSON serialization"""
        if v is None:
            return None
        if isinstance(v, np.ndarray):
            return v.tolist()
        return v

    class Config:
        from_attributes = True


class DocumentChunk(DocumentChunkInDB):
    """Public document chunk schema"""
    pass