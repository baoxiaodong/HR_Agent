"""
职位描述领域的远程服务门面。

当前 JD 数据不在本进程直接持久化，所有增删改查均通过 ``remote_service_client`` 转发到
HR 服务；构造函数保留 ``db`` 参数仅为兼容既有端点。服务同时把 Pydantic 数据转换为
可传输 JSON，并兼容远程保存接口字段不完整的响应。
"""
import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone
from uuid import UUID

from pydantic import ValidationError

from app.schemas.job_description import (
    JobDescriptionCreate,
    JobDescriptionUpdate,
    JobDescriptionResponse,
    JobDescriptionListResponse
)
from app.services.remote_service_client import remote_service_client

logger = logging.getLogger(__name__)


class JobDescriptionService:
    """在认证用户上下文中适配远程 JD CRUD、分页和软删除。"""
    
    def __init__(self, db=None):
        # 不再需要数据库会话，但保留参数以保持接口兼容
        self.db = db
    
    async def create_job_description(
        self, 
        jd_data: JobDescriptionCreate, 
        user_id: UUID
    ) -> JobDescriptionResponse:
        """
        创建新的职位描述
        
        Args:
            jd_data: 职位描述创建数据
            user_id: 用户ID
            
        Returns:
            创建的职位描述对象
            
        Raises:
            Exception: 创建失败时抛出异常
        """
        try:
            # Schema 在 API 层完成字段校验；这里转成纯 JSON 数据，避免 UUID、日期等对象
            # 直接进入 HTTP 客户端后无法编码。
            request_data = jd_data.model_dump(mode="json")

            # user_id 不混入业务正文，而是交给统一远程客户端附加用户上下文。
            # 远程 HR 服务据此完成数据归属隔离，本服务不直接写本地数据库。
            result_data = await remote_service_client.post(
                endpoint="/job-descriptions/save",
                data=request_data,
                user_id=user_id
            )

            # 保存接口可能只返回 id 等少量字段，因此不能直接用远程结果构造完整响应。
            jd_response = self._build_job_description_response(result_data, request_data, user_id)
            logger.info(f"成功创建职位描述: {jd_response.id} - {jd_response.title}")
            return jd_response

        except Exception as e:
            logger.error(f"创建职位描述失败: {str(e)}", exc_info=True)
            raise

    def _build_job_description_response(
        self,
        result_data: Dict[str, Any],
        request_data: Dict[str, Any],
        user_id: UUID,
        jd_id: Optional[str] = None,
    ) -> JobDescriptionResponse:
        """兼容远程保存接口返回部分字段，避免已落库但本地响应校验误报失败。"""
        # 部分远程接口把实体包在 data 中，另一些直接返回实体；先统一成 payload。
        payload = result_data.get("data") if isinstance(result_data.get("data"), dict) else result_data

        # 先放原请求，再覆盖远程返回值：远程生成的 id、时间等服务端事实优先，
        # 请求中的 title/content 等字段只负责补齐“保存成功但返回精简”的情况。
        merged_data = {
            **request_data,
            **payload,
        }
        now = datetime.now(timezone.utc).isoformat()
        if jd_id:
            # 更新接口若不回传 id，仍可使用 URL 中已确认的资源 id。
            merged_data.setdefault("id", jd_id)

        # 以下默认值只在远程响应和原请求都缺失时补入，不会覆盖真实返回值。
        merged_data.setdefault("user_id", str(user_id))
        merged_data.setdefault("workflow_type", request_data.get("workflow_type") or "jd_generation")
        merged_data.setdefault("created_at", now)
        merged_data.setdefault("updated_at", now)
        merged_data.setdefault("is_active", True)

        try:
            # 最后仍由响应 Schema 做一次边界校验，禁止把结构异常的远程数据直接交给 API。
            return JobDescriptionResponse(**merged_data)
        except ValidationError as exc:
            logger.error(
                "远程 JD 已返回但无法组装响应，result_data=%s, request_title=%s",
                result_data,
                request_data.get("title"),
                exc_info=True,
            )
            raise exc
    
    async def update_job_description(
        self, 
        jd_id: str, 
        jd_data: JobDescriptionUpdate, 
        user_id: UUID
    ) -> JobDescriptionResponse:
        """
        更新职位描述
        
        Args:
            jd_id: 职位描述ID
            jd_data: 更新数据
            user_id: 用户ID
            
        Returns:
            更新后的职位描述对象
            
        Raises:
            ValueError: 职位描述不存在或无权限访问
            Exception: 更新失败时抛出异常
        """
        try:
            # PATCH 语义：只转发调用方实际提供的字段，未提交字段保持远端原值。
            request_data = jd_data.model_dump(exclude_unset=True)

            # jd_id 定位资源，user_id 继续作为远程服务的数据归属上下文。
            result_data = await remote_service_client.put(
                endpoint=f"/job-descriptions/{jd_id}",
                data=request_data,
                user_id=user_id
            )

            logger.info(f"成功更新职位描述: {jd_id}")
            # 更新响应同样可能是精简结构，用本次变更字段和 URL id 补齐公开响应。
            return self._build_job_description_response(result_data, request_data, user_id, jd_id=jd_id)

        except ValueError:
            raise
        except Exception as e:
            logger.error(f"更新职位描述失败: {str(e)}")
            raise
    
    async def get_job_description(
        self, 
        jd_id: str, 
        user_id: UUID
    ) -> JobDescriptionResponse:
        """
        获取指定的职位描述
        
        Args:
            jd_id: 职位描述ID
            user_id: 用户ID
            
        Returns:
            职位描述对象
            
        Raises:
            ValueError: 职位描述不存在或无权限访问
            Exception: 获取失败时抛出异常
        """
        try:
            # 发送GET请求到远程服务
            result_data = await remote_service_client.get(
                endpoint=f"/job-descriptions/{jd_id}",
                user_id=user_id
            )
            
            logger.debug(f"成功获取职位描述: {jd_id}")
            return JobDescriptionResponse(**result_data)

        except ValueError:
            raise
        except Exception as e:
            logger.error(f"获取职位描述失败: {str(e)}")
            raise
    
    async def list_job_descriptions(
        self,
        user_id: UUID,
        page: int = 1,
        size: int = 10,
        status_filter: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取用户的职位描述列表
        
        Args:
            user_id: 用户ID
            page: 页码
            size: 每页数量
            status_filter: 状态筛选
            
        Returns:
            包含职位描述列表和分页信息的字典
            
        Raises:
            Exception: 查询失败时抛出异常
        """
        logger.info(f"📥 获取JD列表请求: page={page}, size={size}, status_filter={status_filter}, user_id={user_id}")

        try:
            # 页码、页大小和可选状态都作为查询参数传给远程服务；user_id 仍单独传递，
            # 避免调用方通过查询正文伪造其他用户的数据范围。
            additional_params = {
                "page": page,
                "size": size
            }
            if status_filter:
                additional_params["status_filter"] = status_filter

            # 列表的分页结构由远程服务统一计算，本地不重复切片或重算 total。
            result_data = await remote_service_client.get(
                endpoint="/job-descriptions/",
                user_id=user_id,
                additional_params=additional_params
            )
            
            logger.info(f"📋 返回 {len(result_data.get('items', []))} 条记录")
            return result_data

        except Exception as e:
            logger.error(f"❌ 获取JD列表失败: {str(e)}", exc_info=True)
            raise
    
    async def delete_job_description(
        self, 
        jd_id: str, 
        user_id: UUID
    ) -> Dict[str, Any]:
        """
        删除职位描述（软删除）
        
        Args:
            jd_id: 职位描述ID
            user_id: 用户ID
            
        Returns:
            删除结果消息
            
        Raises:
            ValueError: 职位描述不存在或无权限访问
            Exception: 删除失败时抛出异常
        """
        try:
            # JD 与评分标准位于远程 HR 服务。删除 JD 前先查询关联评分标准，
            # 尽量维持跨资源的一致性；这里没有跨服务事务，因此只能采用“尽力清理”。
            deleted_criteria = []
            criteria_failures = []
            try:
                # 放在函数内导入可避免 JD 服务与评分标准服务在模块加载时循环依赖。
                from app.services.scoring_criteria_service import ScoringCriteriaService

                scoring_service = ScoringCriteriaService(self.db)
                # 当前远程列表接口没有专用的“删除全部关联项”能力，先取最多 100 条再逐项删除。
                criteria_list = await scoring_service.get_scoring_criteria_list(
                    user_id=user_id,
                    page=1,
                    size=100,
                    job_description_id=jd_id,
                )
                for criteria in criteria_list.items:
                    try:
                        await scoring_service.delete_scoring_criteria(str(criteria.id), user_id)
                        deleted_criteria.append({"id": str(criteria.id), "title": criteria.title})
                    except Exception as exc:
                        # 单条评分标准失败不阻断其余清理，失败明细会随最终结果返回。
                        criteria_failures.append({"id": str(criteria.id), "title": criteria.title, "error": str(exc)})
                        logger.warning("删除 JD 关联评分标准失败 jd_id=%s criteria_id=%s: %s", jd_id, criteria.id, exc)
            except Exception as exc:
                # 查询关联项本身失败时也继续删除 JD；这是明确的降级策略，并非原子级联删除。
                criteria_failures.append({"error": str(exc)})
                logger.warning("查询 JD 关联评分标准失败 jd_id=%s: %s", jd_id, exc)

            # 无论关联项是否全部清理成功，都执行用户最初请求的 JD 删除。
            result_data = await remote_service_client.delete(
                endpoint=f"/job-descriptions/{jd_id}",
                user_id=user_id
            )
            if isinstance(result_data, dict):
                # 把级联清理结果附加到远程响应，调用方可据此识别残留项并决定是否重试。
                result_data["deleted_scoring_criteria"] = deleted_criteria
                result_data["scoring_criteria_failures"] = criteria_failures
            
            logger.info(f"成功删除职位描述: {jd_id}，关联删除评分标准 {len(deleted_criteria)} 条")
            return result_data

        except ValueError:
            raise
        except Exception as e:
            logger.error(f"删除职位描述失败: {str(e)}")
            raise
    

    
