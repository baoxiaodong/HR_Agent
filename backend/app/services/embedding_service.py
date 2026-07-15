"""
共享的文本切分与向量嵌入组件。

服务采用进程内单例，避免每次文档处理都重新创建模型客户端和切分器。调用方通过
``get_embedding_service`` 获取同一实例，再分别取嵌入客户端或固定参数的递归文本切分器；
本模块只提供组件，不负责把向量写入数据库。
"""
import logging
from typing import Optional, List
from langchain_text_splitters import RecursiveCharacterTextSplitter
from app.core.config import settings
from .compatible_embeddings import CompatibleOpenAIEmbeddings

logger = logging.getLogger(__name__)


class EmbeddingService:
    """持有进程内共享的嵌入适配器和固定配置文本切分器。"""

    _instance: Optional['EmbeddingService'] = None
    _initialized: bool = False

    def __new__(cls) -> 'EmbeddingService':
        """只分配一个进程内实例；不跨 worker、进程或主机共享。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # Python 仍可能多次调用单例的 __init__，布尔标记避免重复创建 SDK 客户端。
        if not self._initialized:
            self._initialize()
            self._initialized = True

    def _initialize(self):
        """从配置创建嵌入适配器和固定参数的递归文本切分器。

        嵌入调用仍在真正请求时发生；这里仅创建客户端。切分器按 Python 字符长度计算，300
        字符块保留 100 字符重叠，不等同于模型 token 数。初始化失败会阻止单例可用。
        """
        try:
            # 嵌入服务可使用独立密钥；未配置时才复用聊天模型密钥。
            api_key = settings.EMBEDDING_API_KEY or settings.LLM_API_KEY
            base_url = settings.EMBEDDING_BASE_URL or "https://dashscope.aliyuncs.com/compatible-mode/v1"
            model = settings.EMBEDDING_MODEL or "text-embedding-v1"

            # 使用新的CompatibleOpenAIEmbeddings类
            self.embeddings = CompatibleOpenAIEmbeddings(
                api_key=api_key,
                base_url=base_url,
                model=model
            )

            # 初始化文本分割器
            self.text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=300,
                chunk_overlap=100,
                length_function=len,
                separators=["\n\n", "\n", " ", ""]
            )

            logger.info("EmbeddingService已成功初始化OpenAI嵌入")

        except Exception as e:
            logger.error(f"初始化EmbeddingService失败: {e}")
            raise

    def get_embeddings(self) -> CompatibleOpenAIEmbeddings:
        """返回进程内共享的嵌入适配器；真正的远程请求由其 ``embed_*`` 方法触发。"""
        return self.embeddings

    def get_text_splitter(self) -> RecursiveCharacterTextSplitter:
        """返回初始化时创建的固定参数切分器，调用方不应在共享实例上改写配置。"""
        return self.text_splitter

    @classmethod
    def get_instance(cls) -> 'EmbeddingService':
        """惰性创建并返回当前进程的单例；并发 worker 之间不会共享该对象。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance


# 模块级访问入口保持调用方与单例实现解耦。
def get_embedding_service() -> EmbeddingService:
    """通过类级惰性初始化入口取得共享 ``EmbeddingService``。"""
    return EmbeddingService.get_instance()