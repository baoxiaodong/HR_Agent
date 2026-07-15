"""
内部 HR 远程服务的通用 HTTP 客户端。

所有请求统一附加 API Key 和当前用户 ID，并在一个位置处理状态码、剩余调用次数及可选
调试日志。领域服务只需要给出端点和业务数据，不重复拼接 URL、认证信息和错误消息。
"""
import logging
import json
from typing import Any, Optional, Dict
from uuid import UUID
import httpx
from app.core.config import settings

logger = logging.getLogger(__name__)


class RemoteServiceClient:
    """统一内部 HR 服务的 URL、应用认证、用户参数、日志与状态码转换。"""

    def __init__(self):
        self.base_url = f"http://{settings.HR_SERVICE_HOST}:{settings.HR_SERVICE_PORT}/api/v1"
        self.api_key = settings.HR_SERVICE_APIKEY
        self.timeout = 30.0
        self.log_enabled = settings.REMOTE_SERVICE_LOG_ENABLED

    def _get_headers(self) -> Dict[str, str]:
        """构造所有远程请求共享的 JSON 与 API Key 请求头。

        API Key 代表本后端调用远程服务的应用身份；具体用户隔离不在请求头，而在查询参数
        ``current_user_id`` 中传递。
        """
        return {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key
        }

    def _get_params(self, user_id: UUID, **kwargs) -> Dict[str, Any]:
        """把当前用户 UUID 转为远程协议查询参数，并合并端点参数。

        ``kwargs`` 在默认字典之后合并，因此若调用方传入同名 ``current_user_id`` 会覆盖默认值；
        领域服务必须只传分页、筛选等受控参数，不能把客户端任意字典原样放入这里。
        """
        params = {"current_user_id": str(user_id)}
        params.update(kwargs)
        return params

    def _handle_response(self, response: httpx.Response, expected_status: int = 200) -> Dict[str, Any]:
        """校验远程状态码并把成功响应解析为普通 JSON 字典。

        401/403/404 被翻译为业务可读 ``ValueError``，其他非期望状态保留状态码与正文；成功后
        记录额度响应头并调用 ``response.json``。JSON 结构本身不在这里校验，由各领域服务再
        转成 Pydantic Schema 或读取约定字段。
        """
        # 状态码先于 JSON 解析处理，错误页即使不是 JSON 也能形成稳定异常。
        if response.status_code == 401:
            raise ValueError("API密钥未提供")
        elif response.status_code == 403:
            raise ValueError("API密钥认证失败或余额不足或超过有效期")
        elif response.status_code == 404:
            raise ValueError("资源未找到")
        elif response.status_code != expected_status:
            raise ValueError(f"远程服务返回错误: {response.status_code} - {response.text}")
        
        # 记录剩余调用次数
        remaining_calls = response.headers.get("X-Remaining-Calls", "unknown")
        logger.info(f"远程服务剩余调用次数: {remaining_calls}")
        
        # 解析并返回响应数据
        return response.json()

    async def post(self, endpoint: str, data: Dict[str, Any], user_id: UUID, 
                   additional_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发送带应用 API Key 和用户上下文的 JSON POST 请求。

        ``data`` 必须已由领域服务转换为 JSON 可序列化字典；本方法不认识 Pydantic 或 ORM。
        返回远程 JSON，不参与本地事务。网络异常由 httpx 向上抛出，状态码由统一处理器转换。
        调试日志会输出请求头和正文，启用配置时应理解其中可能包含 API Key 与业务数据。
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers()
        params = self._get_params(user_id, **(additional_params or {}))

        # 当前日志语句会直接包含 API Key，与下方受开关控制的详细日志不同；部署时需限制日志访问。
        logger.info(f"准备请求远程服务: {url},参数 :{self.api_key}")
        
        # 记录请求详情
        if self.log_enabled:
            logger.info("=" * 40)
            logger.info(f"[REQUEST] POST {url}")
            logger.info(f"[REQUEST] Headers: {json.dumps(headers, ensure_ascii=False)}")
            logger.info(f"[REQUEST] Params: {json.dumps(params, ensure_ascii=False)}")
            logger.info(f"[REQUEST] Body: {json.dumps(data, ensure_ascii=False, indent=2)}")
            logger.info("=" * 40)
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                json=data,
                headers=headers,
                params=params,
                timeout=self.timeout
            )
            
            # 记录响应详情
            if self.log_enabled:
                try:
                    response_data = response.json()
                    logger.info("-" * 40)
                    logger.info(f"[RESPONSE] Status: {response.status_code}")
                    logger.info(f"[RESPONSE] Headers: {json.dumps(dict(response.headers), ensure_ascii=False)}")
                    logger.info(f"[RESPONSE] Body: {json.dumps(response_data, ensure_ascii=False, indent=2)}")
                    logger.info("-" * 40)
                except Exception as e:
                    logger.warning(f"[RESPONSE] Failed to parse response body: {e}")
            
            return self._handle_response(response)

    async def put(self, endpoint: str, data: Dict[str, Any], user_id: UUID,
                  additional_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发送远程更新请求，协议转换、用户参数和错误处理与 POST 一致。

        该调用不会操作本地 SQLAlchemy 事务；远端一旦更新成功，本地后续失败不能自动撤销。
        启用详细日志时，请求头、查询参数、正文和响应都会被记录。
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers()
        params = self._get_params(user_id, **(additional_params or {}))
        
        # 记录请求详情
        if self.log_enabled:
            logger.info("=" * 80)
            logger.info(f"[REQUEST] PUT {url}")
            logger.info(f"[REQUEST] Headers: {json.dumps(headers, ensure_ascii=False)}")
            logger.info(f"[REQUEST] Params: {json.dumps(params, ensure_ascii=False)}")
            logger.info(f"[REQUEST] Body: {json.dumps(data, ensure_ascii=False, indent=2)}")
            logger.info("=" * 80)
        
        async with httpx.AsyncClient() as client:
            response = await client.put(
                url,
                json=data,
                headers=headers,
                params=params,
                timeout=self.timeout
            )
            
            # 记录响应详情
            if self.log_enabled:
                try:
                    response_data = response.json()
                    logger.info("=" * 80)
                    logger.info(f"[RESPONSE] Status: {response.status_code}")
                    logger.info(f"[RESPONSE] Headers: {json.dumps(dict(response.headers), ensure_ascii=False)}")
                    logger.info(f"[RESPONSE] Body: {json.dumps(response_data, ensure_ascii=False, indent=2)}")
                    logger.info("=" * 80)
                except Exception as e:
                    logger.warning(f"[RESPONSE] Failed to parse response body: {e}")
            
            return self._handle_response(response)

    async def get(self, endpoint: str, user_id: UUID,
                  additional_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发送带用户上下文的远程只读请求并返回 JSON。

        用户 ID 通过查询参数交给远程服务执行隔离，本客户端不在本地再次过滤响应；分页和
        搜索参数来自领域服务。返回结构仍需调用方按端点约定解释。
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers()
        params = self._get_params(user_id, **(additional_params or {}))
        
        # 记录请求详情
        if self.log_enabled:
            logger.info("=" * 80)
            logger.info(f"[REQUEST] GET {url}")
            logger.info(f"[REQUEST] Headers: {json.dumps(headers, ensure_ascii=False)}")
            logger.info(f"[REQUEST] Params: {json.dumps(params, ensure_ascii=False)}")
            logger.info("=" * 80)
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers=headers,
                params=params,
                timeout=self.timeout
            )
            
            # 记录响应详情
            if self.log_enabled:
                try:
                    response_data = response.json()
                    logger.info("=" * 80)
                    logger.info(f"[RESPONSE] Status: {response.status_code}")
                    logger.info(f"[RESPONSE] Headers: {json.dumps(dict(response.headers), ensure_ascii=False)}")
                    logger.info(f"[RESPONSE] Body: {json.dumps(response_data, ensure_ascii=False, indent=2)}")
                    logger.info("=" * 80)
                except Exception as e:
                    logger.warning(f"[RESPONSE] Failed to parse response body: {e}")
            
            return self._handle_response(response)

    async def delete(self, endpoint: str, user_id: UUID,
                     additional_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """请求远程服务删除当前用户范围内的资源。

        本客户端只传递用户上下文和目标端点，不维护本地补偿事务；远端成功后即形成独立
        副作用。目标 ID 应由领域服务从可信资源查询得到，不能直接信任 LLM 输出。
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers()
        params = self._get_params(user_id, **(additional_params or {}))
        
        # 记录请求详情
        if self.log_enabled:
            logger.info("=" * 80)
            logger.info(f"[REQUEST] DELETE {url}")
            logger.info(f"[REQUEST] Headers: {json.dumps(headers, ensure_ascii=False)}")
            logger.info(f"[REQUEST] Params: {json.dumps(params, ensure_ascii=False)}")
            logger.info("=" * 80)
        
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                url,
                headers=headers,
                params=params,
                timeout=self.timeout
            )
            
            # 记录响应详情
            if self.log_enabled:
                try:
                    response_data = response.json()
                    logger.info("=" * 80)
                    logger.info(f"[RESPONSE] Status: {response.status_code}")
                    logger.info(f"[RESPONSE] Headers: {json.dumps(dict(response.headers), ensure_ascii=False)}")
                    logger.info(f"[RESPONSE] Body: {json.dumps(response_data, ensure_ascii=False, indent=2)}")
                    logger.info("=" * 80)
                except Exception as e:
                    logger.warning(f"[RESPONSE] Failed to parse response body: {e}")
            
            return self._handle_response(response)


# 创建全局实例
remote_service_client = RemoteServiceClient()
