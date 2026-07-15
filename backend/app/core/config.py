"""
应用配置的统一入口。

``Settings`` 继承 Pydantic ``BaseSettings``：类属性提供默认值，启动时再由 ``.env``
中的同名变量覆盖并完成类型转换。模块末尾只创建一个 ``settings`` 单例，其他模块均从
这里读取配置，避免各业务模块自行解析环境变量。
"""
from pathlib import Path
from typing import List, Optional
from pydantic import field_validator, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用程序设置"""

    # 基本应用设置
    PROJECT_NAME: str = "HR Agent"
    VERSION: str = "1.0.0"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # API设置
    API_V1_STR: str = "/api/v1"

    # 安全设置
    SECRET_KEY: str = "your-secret-key-change-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8  # 8天

    # CORS设置
    BACKEND_CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:8080"]

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v):
        """组装CORS源"""
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",")]
        elif isinstance(v, (list, str)):
            return v
        raise ValueError(v)

    # 数据库设置
    DATABASE_URL: str = "postgresql://postgres:change-me@localhost:5432/hr_agent"
    DATABASE_HOST: str = "localhost"
    DATABASE_PORT: int = 5432
    DATABASE_NAME: str = "hr_agent"
    DATABASE_USER: str = "postgres"
    DATABASE_PASSWORD: str = "change-me"
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20

    # 远程服务配置
    HR_SERVICE_HOST: str = "127.0.0.1"
    HR_SERVICE_PORT: int = 8000
    HR_SERVICE_APIKEY: str = "your - api - key"

    @property
    def DATABASE_NAME_FROM_URL(self) -> str:
        """从DATABASE_URL中提取数据库名称"""
        if "/" in self.DATABASE_URL:
            return self.DATABASE_URL.split("/")[-1]
        return self.DATABASE_NAME

    # 向量数据库设置
    VECTOR_DIMENSION: int = 1536  # 通义千问 text-embedding-v1维度


    # LLM设置
    LLM_API_KEY: Optional[str] = None
    LLM_BASE_URL: Optional[str] = None
    LLM_MODEL: str = "qwen-max"

    # 嵌入设置
    EMBEDDING_API_KEY: Optional[str] = None
    EMBEDDING_BASE_URL: Optional[str] = None
    EMBEDDING_MODEL: str = "text-embedding-v1"

    # Qwen设置
    QWEN_API_KEY: Optional[str] = None
    QWEN_MODEL: str = "gte-rerank-v2"  # 默认Qwen重排模型

    # 文件上传设置
    MAX_FILE_SIZE: int = 10 * 1024 * 1024  # 10MB
    UPLOAD_DIR: str = "uploads"

    # Dify 工作流配置
    DIFY_BASE_URL: str = "http://localhost/v1"
    DIFY_API_KEY: Optional[str] = None
    DIFY_USER_ID: str = "hr-agent-user"

    # 重排设置
    RERANK_ENABLED: bool = True  # 启用重排（使用Qwen模型）
    RERANK_TOP_K: int = 10  # 需要重排的候选项数量
    RERANK_FINAL_K: int = 10  # 重排后的最终结果数量

    # 查询改写设置
    KB_QUERY_ENHANCE_ENABLED: bool = True  # 启用查询改写功能
    KB_QUERY_EXPANSION_MAX_TERMS: int = 6  # 查询扩展最大术语数量

    # RAG 分数组合权重设置
    RAG_CONTENT_WEIGHT: float = 0.7  # 向量相似度权重
    RAG_TEXT_WEIGHT: float = 0.3  # 文本匹配权重
    RAG_MIN_SIMILARITY_SCORE: float = 0  # 低于该阈值的检索结果将被过滤

    # 上下文设置
    CONTEXT_LIMIT: int = Field(10, description="要检索的上下文文档数量")  # 要检索的上下文文档数量

    # 日志设置
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # 远程服务请求/响应日志设置
    REMOTE_SERVICE_LOG_ENABLED: bool = False  # 是否输出远程服务请求/响应详情

    model_config = {
        "env_file": Path(__file__).parent.parent.parent / ".env",  # 正确的.env文件路径
        "case_sensitive": True,
        "extra": "ignore"
    }


settings = Settings()
