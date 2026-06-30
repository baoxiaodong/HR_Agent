"""
自定义异常类
"""
from typing import Any, Dict, Optional


class BaseHTTPException(Exception):
    """基础HTTP异常类"""
    
    def __init__(
        self,
        message: str,
        status_code: int = 500,
        details: Optional[Dict[str, Any]] = None
    ):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)


class ValidationError(BaseHTTPException):
    """验证错误异常"""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, status_code=400, details=details)


class AuthenticationError(BaseHTTPException):
    """认证错误异常"""
    
    def __init__(self, message: str = "Authentication failed"):
        super().__init__(message, status_code=401)


class AuthorizationError(BaseHTTPException):
    """授权错误异常"""
    
    def __init__(self, message: str = "Access denied"):
        super().__init__(message, status_code=403)


class NotFoundError(BaseHTTPException):
    """资源未找到异常"""
    
    def __init__(self, message: str = "Resource not found"):
        super().__init__(message, status_code=404)


class ConflictError(BaseHTTPException):
    """资源冲突异常"""
    
    def __init__(self, message: str = "Resource conflict"):
        super().__init__(message, status_code=409)


class RateLimitError(BaseHTTPException):
    """速率限制超出异常"""
    
    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__(message, status_code=429)


class InternalServerError(BaseHTTPException):
    """内部服务器错误异常"""
    
    def __init__(self, message: str = "Internal server error"):
        super().__init__(message, status_code=500)


class ServiceUnavailableError(BaseHTTPException):
    """服务不可用异常"""
    
    def __init__(self, message: str = "Service unavailable"):
        super().__init__(message, status_code=503)


# User-related exceptions
class UserNotFoundError(NotFoundError):
    """用户未找到异常"""
    
    def __init__(self, user_id: str = None):
        message = f"User {user_id} not found" if user_id else "User not found"
        super().__init__(message)


class UserAlreadyExistsError(ConflictError):
    """用户已存在异常"""
    
    def __init__(self, field: str = "email"):
        super().__init__(f"User with this {field} already exists")


class InvalidCredentialsError(AuthenticationError):
    """无效凭据异常"""
    
    def __init__(self):
        super().__init__("Invalid email or password")


class InactiveUserError(AuthenticationError):
    """非活跃用户异常"""
    
    def __init__(self):
        super().__init__("User account is inactive")


class InvalidTokenError(AuthenticationError):
    """无效令牌异常"""
    
    def __init__(self):
        super().__init__("Invalid or expired token")


class TokenExpiredError(AuthenticationError):
    """令牌已过期异常"""
    
    def __init__(self):
        super().__init__("Token has expired")


# Document-related exceptions
class DocumentNotFoundError(NotFoundError):
    """文档未找到异常"""
    
    def __init__(self, document_id: str = None):
        message = f"Document {document_id} not found" if document_id else "Document not found"
        super().__init__(message)


class DocumentUploadError(ValidationError):
    """文档上传错误异常"""
    
    def __init__(self, message: str = "Document upload failed"):
        super().__init__(message)


class UnsupportedFileTypeError(ValidationError):
    """不支持的文件类型异常"""
    
    def __init__(self, file_type: str = None):
        message = f"Unsupported file type: {file_type}" if file_type else "Unsupported file type"
        super().__init__(message)


class FileSizeExceededError(ValidationError):
    """文件大小超出限制异常"""
    
    def __init__(self, max_size: str = None):
        message = f"File size exceeds maximum allowed size of {max_size}" if max_size else "File size too large"
        super().__init__(message)


class DocumentProcessingError(InternalServerError):
    """文档处理错误异常"""
    
    def __init__(self, message: str = "Document processing failed"):
        super().__init__(message)


# Conversation-related exceptions
class ConversationNotFoundError(NotFoundError):
    """对话未找到异常"""
    
    def __init__(self, conversation_id: str = None):
        message = f"Conversation {conversation_id} not found" if conversation_id else "Conversation not found"
        super().__init__(message)


class MessageNotFoundError(NotFoundError):
    """消息未找到异常"""
    
    def __init__(self, message_id: str = None):
        message = f"Message {message_id} not found" if message_id else "Message not found"
        super().__init__(message)


class ConversationAccessDeniedError(AuthorizationError):
    """对话访问被拒绝异常"""
    
    def __init__(self):
        super().__init__("Access to this conversation is denied")


# Knowledge base-related exceptions
class KnowledgeBaseNotFoundError(NotFoundError):
    """知识库未找到异常"""
    
    def __init__(self, kb_id: str = None):
        message = f"Knowledge base {kb_id} not found" if kb_id else "Knowledge base not found"
        super().__init__(message)


class FAQNotFoundError(NotFoundError):
    """FAQ未找到异常"""
    
    def __init__(self, faq_id: str = None):
        message = f"FAQ {faq_id} not found" if faq_id else "FAQ not found"
        super().__init__(message)


# LLM-related exceptions
class LLMServiceError(InternalServerError):
    """LLM服务错误异常"""
    
    def __init__(self, message: str = "LLM service error"):
        super().__init__(message)


class LLMRateLimitError(RateLimitError):
    """LLM速率限制超出异常"""
    
    def __init__(self):
        super().__init__("LLM API rate limit exceeded")


class LLMQuotaExceededError(ServiceUnavailableError):
    """LLM配额超出异常"""
    
    def __init__(self):
        super().__init__("LLM API quota exceeded")


class EmbeddingServiceError(InternalServerError):
    """嵌入服务错误异常"""
    
    def __init__(self, message: str = "Embedding service error"):
        super().__init__(message)


# Database-related exceptions
class DatabaseError(InternalServerError):
    """数据库错误异常"""
    
    def __init__(self, message: str = "Database error"):
        super().__init__(message)


class DatabaseConnectionError(ServiceUnavailableError):
    """数据库连接错误异常"""
    
    def __init__(self):
        super().__init__("Database connection failed")


class DatabaseIntegrityError(ConflictError):
    """数据库完整性错误异常"""
    
    def __init__(self, message: str = "Database integrity constraint violated"):
        super().__init__(message)


# Search-related exceptions
class SearchError(InternalServerError):
    """搜索错误异常"""
    
    def __init__(self, message: str = "Search operation failed"):
        super().__init__(message)


class VectorSearchError(SearchError):
    """向量搜索错误异常"""
    
    def __init__(self, message: str = "Vector search failed"):
        super().__init__(message)


# Configuration-related exceptions
class ConfigurationError(InternalServerError):
    """配置错误异常"""
    
    def __init__(self, message: str = "Configuration error"):
        super().__init__(message)


class MissingConfigurationError(ConfigurationError):
    """缺少配置异常"""
    
    def __init__(self, config_key: str):
        super().__init__(f"Missing required configuration: {config_key}")


# External service exceptions
class ExternalServiceError(ServiceUnavailableError):
    """外部服务错误异常"""
    
    def __init__(self, service_name: str = "external service"):
        super().__init__(f"{service_name} is currently unavailable")


class ExternalServiceTimeoutError(ServiceUnavailableError):
    """外部服务超时异常"""
    
    def __init__(self, service_name: str = "external service"):
        super().__init__(f"{service_name} request timed out")