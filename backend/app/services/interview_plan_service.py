"""
面试方案领域的业务门面。

方案主体的增删改查委托给远程 HR 服务；创建前会先在本地数据库确认关联简历评价存在且
属于当前用户，防止把无效关联发送到远端。因此本服务同时持有数据库会话和远程客户端，
承担本地资源校验与远程持久化之间的边界协调。
"""
import logging
from typing import Any, List, Optional, Dict
from uuid import UUID
from fastapi import HTTPException, status
from sqlalchemy import select, and_
from app.models.resume_evaluation import ResumeEvaluation
from app.schemas.interview_plan import (
    InterviewPlanCreate,
    InterviewPlanUpdate,
    InterviewPlanResponse,
    InterviewPlanSaveRequest
)
from app.services.remote_service_client import remote_service_client

logger = logging.getLogger(__name__)
class InterviewPlanService:
    """按用户归属管理面试方案的本地创建、查询、更新和物理删除事务。"""

    def __init__(self, db=None):
        # 方案主体保存在远端，但创建方案前仍需通过本地会话校验简历评价的存在性和归属。
        self.db = db

    # 对应前端保存方案
    async def create_interview_plan(
        self,
        user_id: UUID,
        plan_data: InterviewPlanCreate
    ) -> InterviewPlanResponse:
        """
        创建面试方案

        Args:
            user_id: 用户ID
            plan_data: 面试方案创建数据

        Returns:
            创建的面试方案对象

        Raises:
            HTTPException: 简历评价未找到或无权限访问时抛出
        """
        # 先在本地完成资源归属校验。把 id 与 user_id 放在同一条 SQL 条件中，
        # 即使调用者猜到其他用户的评价 id，也只能得到统一的“不存在或无权限”结果。
        result = await self.db.execute(
            select(ResumeEvaluation).where(
                and_(
                    ResumeEvaluation.id == plan_data.resume_evaluation_id,
                    ResumeEvaluation.user_id == user_id
                )
            )
        )
        resume_evaluation = result.scalar_one_or_none()

        if not resume_evaluation:
            # 不区分“确实不存在”和“属于其他用户”，避免暴露跨用户资源是否存在。
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="简历评价记录未找到或无权限访问"
            )
        try:
            # 本地查询只负责校验，不产生写操作；方案内容转成 JSON 后交给远程 HR 服务持久化。
            request_data = plan_data.model_dump(mode='json')

            # user_id 作为远程数据归属上下文继续向下传递，防止只在本地校验关联项却未隔离方案本身。
            result_data = await remote_service_client.post(
                endpoint="/interview-plans/save-generated",
                data=request_data,
                user_id=user_id
            )

            logger.info(f"成功创建面试方案: {result_data.get('id')}")
            # 远程结果在离开服务层前再按公开 Schema 校验一次。
            return InterviewPlanResponse(**result_data)
            
        except ValueError as e:
            # 统一客户端把远程 404 转成 ValueError；服务层再映射回当前 API 的 404 语义。
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e)
            )
        except Exception as e:
            # 网络错误、远程 5xx 或响应校验错误保留原异常，交给全局异常处理器记录和响应。
            logger.error(f"创建面试方案失败: {str(e)}")
            raise

    # 对应前端编辑后再保存方案
    async def update_interview_plan(
        self,
        plan_id: UUID,
        user_id: UUID,
        plan_data: InterviewPlanUpdate
    ) -> InterviewPlanResponse:
        """
        更新面试方案

        Args:
            plan_id: 面试方案ID
            user_id: 用户ID
            plan_data: 面试方案更新数据

        Returns:
            更新后的面试方案对象

        Raises:
            HTTPException: 面试方案未找到或无权限访问时抛出
        """
        try:
            # 只序列化调用方实际修改的字段，避免 Update Schema 的默认值覆盖远端原内容。
            request_data = plan_data.model_dump(mode='json', exclude_unset=True)

            # 资源 id 来自路径，用户 id 独立传给远程客户端；远端负责方案本身的归属校验。
            result_data = await remote_service_client.put(
                endpoint=f"/interview-plans/{plan_id}",
                data=request_data,
                user_id=user_id
            )

            logger.info(f"成功更新面试方案: {plan_id}")
            return InterviewPlanResponse(**result_data)
            
        except ValueError as e:
            # 远程服务返回404时抛出ValueError，转换为HTTPException
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e)
            )
        except Exception as e:
            logger.error(f"更新面试方案失败: {str(e)}")
            raise

    # async def save_interview_plan_content(
    #     self,
    #     plan_id: UUID,
    #     user_id: UUID,
    #     save_data: InterviewPlanSaveRequest
    # ) -> InterviewPlan:
    #     """
    #     保存面试方案内容（用于前端编辑后保存）

    #     Args:
    #         plan_id: 面试方案ID
    #         user_id: 用户ID
    #         save_data: 保存数据

    #     Returns:
    #         保存后的面试方案对象

    #     Raises:
    #         HTTPException: 面试方案未找到或无权限访问时抛出
    #     """
    #     # 查询面试方案
    #     result = await self.db.execute(
    #         select(InterviewPlan).where(
    #             and_(
    #                 InterviewPlan.id == plan_id,
    #                 InterviewPlan.user_id == user_id
    #             )
    #         )
    #     )
    #     interview_plan = result.scalar_one_or_none()
        
    #     if not interview_plan:
    #         raise HTTPException(
    #             status_code=status.HTTP_404_NOT_FOUND,
    #             detail="面试方案未找到或无权限访问"
    #         )
        
    #     # 更新内容
    #     interview_plan.content = save_data.content
    #     if save_data.candidate_name:
    #         interview_plan.candidate_name = save_data.candidate_name
    #     if save_data.candidate_position:
    #         interview_plan.candidate_position = save_data.candidate_position
        
    #     await self.db.commit()
    #     await self.db.refresh(interview_plan)
    #     logger.info(f"成功保存面试方案内容: {interview_plan.id}")
    #     return interview_plan

    async def get_interview_plan(
        self,
        plan_id: UUID,
        user_id: UUID
    ) -> InterviewPlanResponse:
        """
        获取面试方案详情

        Args:
            plan_id: 面试方案ID
            user_id: 用户ID

        Returns:
            面试方案对象

        Raises:
            HTTPException: 面试方案未找到或无权限访问时抛出
        """
        try:
            # 发送GET请求到远程服务
            result_data = await remote_service_client.get(
                endpoint=f"/interview-plans/{plan_id}",
                user_id=user_id
            )
            
            logger.info(f"成功获取面试方案详情: {plan_id}")
            return InterviewPlanResponse(**result_data)
            
        except ValueError as e:
            # 远程服务返回404时抛出ValueError，转换为HTTPException
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e)
            )
        except Exception as e:
            logger.error(f"获取面试方案失败: {str(e)}")
            raise

    async def list_interview_plans(
        self,
        user_id: UUID,
        page: int = 1,
        size: int = 10,
        resume_evaluation_id: Optional[UUID] = None
    ) -> Dict[str, Any]:
        """
        获取面试方案列表

        Args:
            user_id: 用户ID
            page: 页码
            size: 每页数量
            resume_evaluation_id: 简历评价ID筛选

        Returns:
            包含面试方案列表和分页信息的字典
        """
        try:
            # 分页交由远程服务执行；可选评价 id 用于把方案列表收窄到某次简历评价。
            additional_params = {
                "page": page,
                "size": size
            }
            if resume_evaluation_id:
                # 查询参数必须是 HTTP 可传输的字符串，而不是 Python UUID 对象。
                additional_params["resume_evaluation_id"] = str(resume_evaluation_id)

            # user_id 不放入可由调用方组装的筛选字典，而是作为独立的归属上下文传递。
            result_data = await remote_service_client.get(
                endpoint="/interview-plans/",
                user_id=user_id,
                additional_params=additional_params
            )

            # 本方法保持远程分页结构原样返回，由端点的 response_model 负责最终响应校验。
            logger.info(f"成功获取面试方案列表，第 {page} 页，共 {result_data.get('total', 0)} 条结果")
            return result_data
            
        except Exception as e:
            logger.error(f"获取面试方案列表失败: {str(e)}")
            raise

    async def delete_interview_plan(
        self,
        plan_id: UUID,
        user_id: UUID
    ) -> Dict[str, str]:
        """
        删除面试方案

        Args:
            plan_id: 面试方案ID
            user_id: 用户ID

        Returns:
            删除成功消息

        Raises:
            HTTPException: 面试方案未找到或无权限访问时抛出
        """
        try:
            # 发送DELETE请求到远程服务
            result_data = await remote_service_client.delete(
                endpoint=f"/interview-plans/{plan_id}",
                user_id=user_id
            )
            
            logger.info(f"成功删除面试方案: {plan_id}")
            return result_data
            
        except ValueError as e:
            # 远程服务返回404时抛出ValueError，转换为HTTPException
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e)
            )
        except Exception as e:
            logger.error(f"删除面试方案失败: {str(e)}")
            raise

    # async def save_generated_interview_plan(
    #     self,
    #     user_id: UUID,
    #     plan_data: InterviewPlanCreate
    # ) -> InterviewPlan:
    #     """
    #     保存生成的面试方案内容

    #     Args:
    #         user_id: 用户ID
    #         plan_data: 面试方案创建数据

    #     Returns:
    #         创建的面试方案对象

    #     Raises:
    #         HTTPException: 简历评价未找到或无权限访问时抛出
    #     """
    #     # 验证简历评价是否存在且属于当前用户
    #     result = await self.db.execute(
    #         select(ResumeEvaluation).where(
    #             and_(
    #                 ResumeEvaluation.id == plan_data.resume_evaluation_id,
    #                 ResumeEvaluation.user_id == user_id
    #             )
    #         )
    #     )
    #     resume_evaluation = result.scalar_one_or_none()
        
    #     if not resume_evaluation:
    #         raise HTTPException(
    #             status_code=status.HTTP_404_NOT_FOUND,
    #             detail="简历评价记录未找到或无权限访问"
    #         )
        
    #     # 检查是否已存在面试方案
    #     existing_result = await self.db.execute(
    #         select(InterviewPlan).where(
    #             and_(
    #                 InterviewPlan.resume_evaluation_id == plan_data.resume_evaluation_id,
    #                 InterviewPlan.user_id == user_id
    #             )
    #         )
    #     )
    #     existing_plan = existing_result.scalar_one_or_none()
        
    #     if existing_plan:
    #         # 如果已存在，则更新现有方案
    #         existing_plan.candidate_name = plan_data.candidate_name
    #         existing_plan.candidate_position = plan_data.candidate_position
    #         existing_plan.content = plan_data.content
            
    #         await self.db.commit()
    #         await self.db.refresh(existing_plan)
    #         logger.info(f"成功更新面试方案2: {existing_plan.id}")
    #         return existing_plan
    #     else:
    #         # 如果不存在，则创建新方案
    #         return await self.create_interview_plan(user_id, plan_data)