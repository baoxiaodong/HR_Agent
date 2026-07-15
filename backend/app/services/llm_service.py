"""
大模型与嵌入模型的底层适配器。

本服务使用 OpenAI 兼容协议连接配置中的模型供应商，统一构造系统提示、最近对话历史和
检索上下文，并提供同步文本、流式文本、向量、摘要和建议等原子能力。它不处理数据库、
会话归属或 HR 工作流，这些职责由上层服务组合。
"""
import logging
import httpx
import asyncio
import openai
from typing import List, Dict, Any, Optional, AsyncGenerator
# 移除了LangChain导入以避免兼容性问题

from app.core.config import settings

logger = logging.getLogger(__name__)


class LLMService:
    """封装无状态模型调用和提示组装，不负责业务校验、权限或持久化。"""

    def __init__(self):
        # 使用配置的LLM初始化OpenAI客户端
        llm_api_key = getattr(settings, 'LLM_API_KEY', None) or settings.LLM_API_KEY
        llm_base_url = getattr(settings, 'LLM_BASE_URL', None) or 'https://dashscope.aliyuncs.com/compatible-mode/v1'
        self.llm_model = getattr(settings, 'LLM_MODEL', 'qwen-max')

        # 传递base_url，除非是默认的OpenAI URL
        client_kwargs = {'api_key': llm_api_key}
        if llm_base_url != 'https://api.openai.com/v1':
            client_kwargs['base_url'] = llm_base_url

        self.client = openai.AsyncOpenAI(**client_kwargs)

        # 对于嵌入，我们将使用自定义实现，因为我们有不同的API
        self.embedding_api_key = settings.EMBEDDING_API_KEY or settings.LLM_API_KEY
        self.embedding_base_url = settings.EMBEDDING_BASE_URL or 'https://dashscope.aliyuncs.com/compatible-mode/v1'
        self.embedding_model = settings.EMBEDDING_MODEL or 'text-embedding-v1'

        # 嵌入配置只在真正调用 generate_embedding 时使用，初始化聊天模型时不输出 info 日志，避免误以为 Agent 规划调用了向量模型。
        logger.debug(f"Embedding config - Base URL: {self.embedding_base_url}")
        logger.debug(f"Embedding config - Model: {self.embedding_model}")

        # HR Agent 当前产品能力提示：避免普通聊天夸大成通用 HR 咨询助手
        self.system_prompt = """你是招聘场景的 HR Agent 助手，不是通用人力资源政策/薪酬/绩效咨询助手。

        你当前能帮助用户完成：
        - 生成岗位 JD，并生成简历评分标准
        - 上传简历后，基于指定 JD 进行简历评分/筛选
        - 基于已评分候选人生成面试计划
        - 基于上传文档生成笔试试卷；如果有当前面试方案，可按约 8:2 结合文档和面试方案出题
        - 生成邮件通知草稿，但不会自动发送
        - 通过聊天删除已生成的 JD、简历评分记录、面试方案、试卷

        如果用户询问“你能做什么/有哪些能力”，只介绍以上能力。
        不要宣称可以处理员工薪酬福利、绩效管理、劳动法合规、员工关系冲突等当前产品未接入的泛 HR 咨询能力。
        如果用户提出超出当前工具范围的需求，礼貌说明暂不支持，并建议转为当前支持的招聘闭环任务。"""

    async def generate_response(
        self,
        message: str,
        conversation_history: List[Dict[str, str]] = None,
        context: Optional[str] = None
    ) -> str:
        """把系统约束、最近历史、检索上下文和当前问题组装为一次聊天请求。

        上层传入的 ORM/Schema 已转换为 ``role/content`` 字典；这里最多保留最后十条历史，并
        把检索文本和问题合并成最后一条 user 消息。方法只返回供应商文本，不验证业务 JSON、
        不保存会话；模型输出必须由上层按具体用途继续校验。
        """
        try:
            # 系统提示固定产品能力边界，始终位于用户历史之前。
            messages = [{"role": "system", "content": self.system_prompt}]

            # 限制历史条数以控制 token 成本；角色值由会话服务在上层转换。
            if conversation_history:
                for msg in conversation_history[-10:]:  # 保留最后10条消息
                    messages.append({"role": msg["role"], "content": msg["content"]})

            # 检索内容只是模型参考上下文，不会在本层被解释为权限或可信指令。
            if context:
                context_message = f"相关上下文: {context}\n\n用户问题: {message}"
                messages.append({"role": "user", "content": context_message})
            else:
                messages.append({"role": "user", "content": message})

            response = await self.client.chat.completions.create(
                model=self.llm_model,
                messages=messages,
                temperature=0.7,
                max_tokens=2000
            )

            return response.choices[0].message.content

        except Exception as e:
            logger.error(f"生成响应时出错: {e}")
            raise

    async def stream_response(
        self,
        message: str,
        conversation_history: List[Dict[str, str]] = None,
        context: Optional[str] = None
    ) -> AsyncGenerator[str, None]:
        """使用与非流式调用相同的提示结构，逐段产出供应商增量文本。

        本层不累积完整回答，也不发送 SSE/JSON 协议；上层聊天或 Agent 服务负责累计、去重、
        持久化并包装传输事件。远程异常原样向上抛出，以便调用方选择降级策略。
        """
        try:
            messages = [{"role": "system", "content": self.system_prompt}]

            # 流式与非流式路径使用相同的十条历史上限，避免回答语义因传输模式改变。
            if conversation_history:
                for msg in conversation_history[-10:]:
                    messages.append({"role": msg["role"], "content": msg["content"]})

            # 相关文档被拼入最后一条用户消息，不会创建额外系统权限。
            if context:
                context_message = f"相关上下文: {context}\n\n用户问题: {message}"
                messages.append({"role": "user", "content": context_message})
            else:
                messages.append({"role": "user", "content": message})

            # OpenAI 兼容客户端返回 SDK 块，本方法只产出非空 content 字符串。
            stream = await self.client.chat.completions.create(
                model=self.llm_model,
                messages=messages,
                temperature=0.7,
                max_tokens=2000,
                stream=True
            )

            async for chunk in stream:
                if chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

        except Exception as e:
            logger.error(f"流式传输响应时出错: {e}")
            raise

    async def generate_embedding(self, text: str) -> List[float]:
        """通过独立的 OpenAI 兼容 embeddings 端点把文本转换为浮点向量。

        聊天与嵌入可以使用不同密钥、地址和模型。请求 JSON 只包含模型名和输入文本；成功
        响应从 ``data[0].embedding`` 提取普通列表，供向量库使用。非 200、网络或响应结构异常
        都向上抛出，本层不提供本地伪向量降级。
        """
        try:
            async with httpx.AsyncClient() as client:
                # API 密钥只进入请求头，不写入返回值或业务数据。
                headers = {
                    "Authorization": f"Bearer {self.embedding_api_key}",
                    "Content-Type": "application/json"
                }

                data = {
                    "model": self.embedding_model,
                    "input": text
                }

                response = await client.post(
                    f"{self.embedding_base_url}/embeddings",
                    headers=headers,
                    json=data,
                    timeout=30.0
                )

                if response.status_code == 200:
                    result = response.json()
                    return result["data"][0]["embedding"]
                else:
                    logger.error(f"嵌入API错误: {response.status_code} - {response.text}")
                    raise Exception(f"嵌入API错误: {response.status_code}")

        except Exception as e:
            logger.error(f"生成嵌入时出错: {e}")
            raise


    async def summarize_text(self, text: str, max_length: int = 200) -> str:
        """把原文和长度要求包装成一次独立模型调用并返回去空白摘要。

        ``max_length`` 用于提示词和粗略 token 上限，不是服务端对最终字符数的硬截断；调用方
        若有严格存储长度约束仍需自行裁剪。该能力不读取会话历史或数据库。
        """
        try:
            prompt = f"""请用不超过{max_length}个词提供以下文本的简洁摘要：

{text}

摘要:"""

            response = await self.client.chat.completions.create(
                model=self.llm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=max_length * 2  # 为令牌留出一些缓冲区
            )

            return response.choices[0].message.content.strip()

        except Exception as e:
            logger.error(f"总结文本时出错: {e}")
            raise

    async def generate_suggestions(self, query: str, context: str = "") -> List[str]:
        """让模型生成换行分隔的后续问题，并规范化为最多五个字符串。

        返回格式不是结构化工具调用，解析策略只过滤空行和截取前五项；编号等模型文本仍会
        保留。异常向上抛出，由聊天服务将建议功能降级为空列表。
        """
        try:
            prompt = f"""基于以下HR相关查询，建议5个用户可能想要询问的相关问题：

查询: {query}
上下文: {context}

提供5个简短、相关的问题（每行一个）："""

            response = await self.client.chat.completions.create(
                model=self.llm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=300
            )
            suggestions_text = response.choices[0].message.content.strip()

            # 解析建议
            suggestions = [s.strip() for s in suggestions_text.split("\n") if s.strip()]
            return suggestions[:5]

        except Exception as e:
            logger.error(f"生成建议时出错: {e}")
            raise
