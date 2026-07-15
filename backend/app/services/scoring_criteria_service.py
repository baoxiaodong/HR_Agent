"""
评分标准领域的远程服务门面。

创建、更新、查询和软删除都转发给独立 HR 服务，当前进程只负责序列化请求、附加用户
上下文并把远程 JSON 校验为 Pydantic 响应。响应字段缺失时，构造辅助函数会从原请求补齐
必要字段，避免远端已保存成功却因本地响应校验失败而误报。
"""
from typing import Any, Optional, Dict, List
from uuid import UUID
import logging
from datetime import datetime, timezone
from app.schemas.scoring_criteria import (
    ScoringCriteriaCreate,
    ScoringCriteriaUpdate,
    ScoringCriteriaListResponse,
    ScoringCriteriaResponse
)
from app.services.remote_service_client import remote_service_client

logger = logging.getLogger(__name__)


class ScoringCriteriaService:
    """在认证用户上下文中适配远程评分标准 CRUD 与分页响应。"""

    def __init__(self, db=None):
        # 不再需要数据库会话，但保留参数以保持接口兼容
        self.db = db

    async def save_scoring_criteria(
        self,
        criteria_data: ScoringCriteriaCreate,
        user_id: UUID
    ) -> ScoringCriteriaResponse:
        """
        保存生成的评分标准到数据库

        Args:
            criteria_data: 评分标准创建数据
            user_id: 用户ID

        Returns:
            保存的评分标准对象
        """
        try:
            # API Schema 已完成输入校验；mode='json' 把 UUID 等对象转成远程 HTTP 接口可编码的值。
            request_data = criteria_data.model_dump(mode='json')

            # user_id 由统一客户端放入远程请求上下文，用于 HR 服务限制数据归属；
            # 当前服务只做编排，不在本地数据库重复保存评分标准。
            result_data = await remote_service_client.post(
                endpoint="/scoring-criteria/save",
                data=request_data,
                user_id=user_id
            )

            # 远程保存响应可能只含部分字段，统一交给兼容构造器补齐后再校验。
            return self._build_scoring_criteria_response(result_data, request_data, user_id)

        except Exception as e:
            logger.error(f"保存评分标准失败: {str(e)}")
            raise

    async def update_scoring_criteria(
        self,
        criteria_id: str,
        criteria_data: ScoringCriteriaUpdate,
        user_id: UUID
    ) -> ScoringCriteriaResponse:
        """
        更新已保存的评分标准

        Args:
            criteria_id: 评分标准ID
            criteria_data: 评分标准更新数据
            user_id: 用户ID

        Returns:
            更新后的评分标准对象
        """
        try:
            # exclude_unset=True 保留“局部更新”语义：调用方没传的字段不会被序列化成默认值覆盖远端数据。
            request_data = criteria_data.model_dump(mode='json', exclude_unset=True)

            # criteria_id 决定修改哪个资源，user_id 决定当前用户是否有权访问该资源。
            result_data = await remote_service_client.put(
                endpoint=f"/scoring-criteria/{criteria_id}",
                data=request_data,
                user_id=user_id
            )

            # 若远程只返回变更结果，构造器会用请求字段及 URL id 还原完整响应。
            return self._build_scoring_criteria_response(result_data, request_data, user_id, criteria_id=criteria_id)

        except Exception as e:
            logger.error(f"更新评分标准失败: {str(e)}")
            raise

    def _build_scoring_criteria_response(
        self,
        result_data: Dict[str, Any],
        request_data: Dict[str, Any],
        user_id: UUID,
        criteria_id: Optional[str] = None,
    ) -> ScoringCriteriaResponse:
        """把不同形态的远程保存响应统一成 API 对外的完整评分标准。"""
        # 兼容 {"data": {...}} 和直接返回实体两种远程协议。
        payload = result_data.get("data") if isinstance(result_data.get("data"), dict) else result_data

        # 服务端事实覆盖请求值；请求值只在远端返回精简结果时作为补充。
        merged_data = {
            **request_data,
            **payload,
        }
        now = datetime.now(timezone.utc).isoformat()
        if criteria_id:
            # 更新 URL 已包含可信资源 id，仅在响应缺少 id 时回填。
            merged_data.setdefault("id", criteria_id)

        # setdefault 不会覆盖远程服务已经返回的用户、状态或时间字段。
        merged_data.setdefault("user_id", str(user_id))
        merged_data.setdefault("workflow_type", request_data.get("workflow_type") or "scoring_criteria_generation")
        merged_data.setdefault("created_at", now)
        merged_data.setdefault("updated_at", now)
        merged_data.setdefault("is_active", True)

        # 在服务边界重新校验，避免不完整或类型错误的远程 JSON 泄漏到端点响应。
        return ScoringCriteriaResponse(**merged_data)

    async def get_scoring_criteria(
        self,
        criteria_id: str,
        user_id: UUID
    ) -> ScoringCriteriaResponse:
        """
        获取单个评分标准详情

        Args:
            criteria_id: 评分标准ID
            user_id: 用户ID

        Returns:
            评分标准对象
        """
        try:
            # 发送GET请求到远程服务
            result_data = await remote_service_client.get(
                endpoint=f"/scoring-criteria/{criteria_id}",
                user_id=user_id
            )
            
            return ScoringCriteriaResponse(**result_data)

        except Exception as e:
            logger.error(f"获取评分标准失败: {str(e)}")
            raise

    async def get_scoring_criteria_list(
        self,
        user_id: UUID,
        page: int = 1,
        size: int = 10,
        job_description_id: Optional[str] = None
    ) -> ScoringCriteriaListResponse:
        """
        获取评分标准列表

        Args:
            user_id: 用户ID
            page: 页码
            size: 每页数量
            job_description_id: 关联的JD ID

        Returns:
            评分标准列表响应对象
        """
        try:
            # 分页参数控制返回窗口；job_description_id 只在提供时加入，用于查询某个 JD 的关联标准。
            additional_params = {
                "page": page,
                "size": size
            }
            if job_description_id:
                additional_params["job_description_id"] = job_description_id

            # 用户范围不接受 additional_params 覆盖，而由客户端使用独立 user_id 传递。
            result_data = await remote_service_client.get(
                endpoint="/scoring-criteria/",
                user_id=user_id,
                additional_params=additional_params
            )

            # 把远程分页 JSON 校验成 items/total/page/size 明确的响应对象。
            return ScoringCriteriaListResponse(**result_data)

        except Exception as e:
            logger.error(f"获取评分标准列表失败: {str(e)}")
            raise

    async def delete_scoring_criteria(
        self,
        criteria_id: str,
        user_id: UUID
    ) -> Dict[str, str]:
        """
        删除评分标准（软删除）

        Args:
            criteria_id: 评分标准ID
            user_id: 用户ID

        Returns:
            删除结果信息
        """
        try:
            # 发送DELETE请求到远程服务
            result_data = await remote_service_client.delete(
                endpoint=f"/scoring-criteria/{criteria_id}",
                user_id=user_id
            )
            
            return result_data

        except Exception as e:
            logger.error(f"删除评分标准失败: {str(e)}")
            raise
