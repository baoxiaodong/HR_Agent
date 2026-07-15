"""
Dify 工作流 HTTP 适配器。

调用方只提供工作流类型、查询文本和额外输入，本服务负责认证头、请求体、超时与错误
转换。同步方法返回完整 JSON；流式方法解析 Dify 的 SSE 行，过滤控制帧和心跳后再把
有效内容交给 API 层转发。
"""
import json
import uuid
from typing import Dict, Any, AsyncGenerator, Optional
import httpx
from fastapi import HTTPException
from app.core.config import settings
from app.core.logging import logger


class DifyService:
    """把内部工作流调用适配为 Dify 同步 JSON 或经过过滤的 SSE 数据行。"""

    def __init__(self):
        self.base_url = settings.DIFY_BASE_URL
        self.api_key = settings.DIFY_API_KEY
        self.user_id = settings.DIFY_USER_ID

        if not self.api_key:
            raise ValueError("DIFY_API_KEY是必需的但未配置")

    async def call_workflow_stream(
        self,
        workflow_type: int,
        query: str,
        conversation_id: Optional[str] = None,
        additional_inputs: Optional[Dict[str, Any]] = None
    ) -> AsyncGenerator[str, None]:
        """将内部工作流参数转换为 Dify chat-messages 流式请求。

        ``workflow_type`` 和附加字段被合并进 ``inputs``，查询放在 ``query``，远程会话 ID 为空
        时发送空串。方法逐行过滤 SSE 控制帧后产出 JSON 字符串或非 JSON 正文，不把远程事件
        解释成业务对象；调用方负责提取 answer/delta。超时和网络错误会转换为 HTTPException。
        """
        try:
            # 固定 type 是工作流路由字段；additional_inputs 只补充该工作流需要的业务参数。
            inputs = {"type": workflow_type}
            if additional_inputs:
                inputs.update(additional_inputs)

            # 这是 Dify 对外协议字典；本地 UUID/Schema 等对象必须在上层先转为 JSON 可序列化值。
            request_data = {
                "inputs": inputs,
                "query": query,
                "response_mode": "streaming",
                "conversation_id": conversation_id or "",
                "user": self.user_id
            }

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }

            logger.info(f"调用Dify工作流类型 {workflow_type}，查询: {query[:100]}...")

            # 向Dify发起流式请求
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat-messages",
                    headers=headers,
                    json=request_data
                ) as response:

                    # 非 200 时先完整读取响应体，便于将远程错误原因返回给上层。
                    if response.status_code != 200:
                        error_text = await response.aread()
                        logger.error(f"Dify API错误: {response.status_code} - {error_text}")
                        raise HTTPException(
                            status_code=response.status_code,
                            detail=f"Dify API错误: {error_text.decode()}"
                        )

                    # 流式传输响应
                    async for chunk in response.aiter_lines():
                        if chunk:
                            chunk = chunk.strip()

                            # 跳过SSE控制行和心跳包，避免被前端误当作正文
                            lowered = chunk.lower()
                            if (
                                lowered.startswith("event:")
                                or lowered.startswith("id:")
                                or lowered.startswith("retry:")
                                or lowered == "ping"
                                or lowered == "event: ping"
                            ):
                                continue

                            # 如果存在'data: '前缀则移除
                            if chunk.startswith("data: "):
                                chunk = chunk[6:].strip()

                            # 跳过空行和[DONE]标记
                            if not chunk or chunk == "[DONE]":
                                continue

                            try:
                                # 解析JSON块
                                data = json.loads(chunk)
                                if str(data.get("event") or "").lower() == "ping":
                                    continue
                                yield chunk
                            except json.JSONDecodeError:
                                # 非JSON正文才透传，SSE控制内容在上面已过滤
                                yield chunk

        except httpx.TimeoutException:
            logger.error("Dify API请求超时")
            raise HTTPException(status_code=504, detail="Dify API请求超时")
        except httpx.RequestError as e:
            logger.error(f"Dify API请求错误: {str(e)}")
            raise HTTPException(status_code=503, detail=f"Dify API请求错误: {str(e)}")
        except Exception as e:
            # 该兜底也会捕获上面主动抛出的 HTTPException，并按当前实现重新包装为 500；
            # 因此最终状态码应以实际抛出的异常为准，不能假设远程非 200 一定原样透传。
            logger.error(f"Dify工作流调用中出现意外错误: {str(e)}")
            raise HTTPException(status_code=500, detail=f"内部服务器错误: {str(e)}")

    async def call_workflow_sync(
        self,
        workflow_type: int,
        query: str,
        conversation_id: Optional[str] = None,
        additional_inputs: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """以 blocking 模式调用同一 Dify 端点并返回完整 JSON 字典。

        请求字段与流式版本一致，仅 ``response_mode`` 和超时时间不同；返回 JSON 保留远程自由
        结构，业务层需继续提取并校验 ``answer/data``。该适配器不保存数据库或会话消息。
        """
        try:
            # 工作流类型与业务输入统一放入 inputs，避免散落在顶层请求协议。
            inputs = {"type": workflow_type}
            if additional_inputs:
                inputs.update(additional_inputs)

            request_data = {
                "inputs": inputs,
                "query": query,
                "response_mode": "blocking",
                "conversation_id": conversation_id or "",
                "user": self.user_id
            }

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }

            logger.info(f"调用Dify工作流类型 {workflow_type} (同步)，查询: {query[:100]}...")

            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{self.base_url}/chat-messages",
                    headers=headers,
                    json=request_data
                )

                if response.status_code != 200:
                    error_text = response.text
                    logger.error(f"Dify API错误: {response.status_code} - {error_text}")
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"Dify API错误: {error_text}"
                    )

                return response.json()

        except httpx.TimeoutException:
            logger.error("Dify API请求超时")
            raise HTTPException(status_code=504, detail="Dify API请求超时")
        except httpx.RequestError as e:
            logger.error(f"Dify API请求错误: {str(e)}")
            raise HTTPException(status_code=503, detail=f"Dify API请求错误: {str(e)}")
        except Exception as e:
            # 该兜底也会捕获上面主动抛出的 HTTPException，并按当前实现重新包装为 500；
            # 因此最终状态码应以实际抛出的异常为准，不能假设远程非 200 一定原样透传。
            logger.error(f"Dify工作流调用中出现意外错误: {str(e)}")
            raise HTTPException(status_code=500, detail=f"内部服务器错误: {str(e)}")
