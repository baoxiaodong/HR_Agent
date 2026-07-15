"""
OpenAI 兼容嵌入客户端的 LangChain 适配器。

实现 ``Embeddings`` 约定后，LangChain 向量存储可以直接调用配置的 DashScope 兼容接口。
文档按 20 条分批以避开供应商批量上限；异步方法把同步 SDK 调用放在线程池执行，避免
阻塞 FastAPI 事件循环。
"""

import asyncio
from typing import List, Optional
from openai import OpenAI
from langchain_core.embeddings import Embeddings
from ..core.config import settings
import logging
logger = logging.getLogger(__name__)

class CompatibleOpenAIEmbeddings(Embeddings):
    """把 OpenAI 兼容 embeddings 接口适配为 LangChain 同步/异步嵌入协议。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        model: str = "text-embedding-v1",
        dimensions: int = None
    ):
        self.api_key = api_key or settings.EMBEDDING_API_KEY
        self.base_url = base_url
        self.model = model
        self.dimensions = dimensions or settings.VECTOR_DIMENSION

        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )

        logger.info(f"DashScope兼容嵌入已使用模型初始化: {self.model}")

    BATCH_SIZE = 20  # API限制为25，留余量

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """按 20 条分批调用同步嵌入 API，并保持输入顺序合并向量。

        供应商响应可能不按请求顺序排列，因此每批先按 ``index`` 排序；任一批失败会整体抛错，
        已得到的前序向量只存在内存中，不会由本方法持久化。
        """
        try:
            all_embeddings = []
            # 批量上限留出余量，降低供应商限制变化导致整批请求失败的概率。
            for i in range(0, len(texts), self.BATCH_SIZE):
                batch = texts[i:i + self.BATCH_SIZE]
                response = self.client.embeddings.create(
                    model=self.model,
                    input=batch,
                    dimensions=self.dimensions,
                    encoding_format="float"
                )
                sorted_data = sorted(response.data, key=lambda x: x.index)
                all_embeddings.extend(d.embedding for d in sorted_data)
            return all_embeddings
        except Exception as e:
            logger.error(f"嵌入文档时出错: {e}")
            raise

    def embed_query(self, text: str) -> List[float]:
        """为单个检索问题生成向量，维度必须与文档向量及 PGVector 列一致。"""
        try:
            response = self.client.embeddings.create(
                model=self.model,
                input=text,
                dimensions=self.dimensions,
                encoding_format="float"
            )
            return response.data[0].embedding
        except Exception as e:
            logger.error(f"嵌入查询时出错: {e}")
            raise

    async def aembed_documents(self, texts: List[str]) -> List[List[float]]:
        """在线程池执行同步批量 SDK，避免阻塞异步 Web 事件循环。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.embed_documents, texts)

    async def aembed_query(self, text: str) -> List[float]:
        """在线程池执行同步单文本嵌入；异常会跨线程重新抛给异步调用方。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.embed_query, text)