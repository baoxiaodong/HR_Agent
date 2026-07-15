"""
仪表盘统计聚合服务。

服务把本地数据库中的简历、会话、试卷数据与远程 HR 服务中的 JD 统计合并为前端需要的
结构。各子统计独立捕获异常并返回空数据，因此单个数据源不可用时仪表盘仍能展示其他
模块，而总入口只负责组合结果。
"""
import logging
from typing import Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from datetime import datetime, timedelta
from uuid import UUID

# from app.models.job_description import JobDescription
from app.models.exam import Exam
from app.models.exam_result import ExamResult
from app.models.resume_evaluation import ResumeEvaluation, ResumeStatus
from app.models.conversation import Conversation
from app.services.remote_service_client import remote_service_client

logger = logging.getLogger(__name__)


class StatsService:
    """按用户聚合本地与远程统计，并为各数据源提供独立降级结果。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_dashboard_stats(self, user_id: str) -> Dict[str, Any]:
        """
        获取仪表板统计数据

        Args:
            user_id: 用户ID

        Returns:
            包含各类统计数据的字典
        """
        try:
            # 四个子统计按前端卡片结构分别获取。每个子方法都自行降级为空统计，
            # 因此某个远程服务或本地查询失败时，不会阻断其他卡片。
            recruitment_stats = await self._get_recruitment_stats(user_id)
            training_stats = await self._get_training_stats(user_id)
            interview_stats = await self._get_interview_stats(user_id)
            assistant_stats = await self._get_assistant_stats(user_id)

            # 在这里统一命名领域，端点无需理解每项统计来自本地还是远程。
            return {
                "recruitment": recruitment_stats,
                "training": training_stats,
                "interview": interview_stats,
                "assistant": assistant_stats
            }

        except Exception as e:
            logger.error(f"获取仪表板统计数据失败: {e}")
            raise

    async def _get_recruitment_stats(self, user_id: str) -> Dict[str, Any]:
        """把用户 UUID 转发给远程 JD 仪表盘接口，失败时返回零值卡片。"""
        try:
            # JD 不存于本地数据库。先把端点传入的字符串恢复为 UUID，
            # 再由统一客户端向远程统计接口传递用户范围。
            result_data = await remote_service_client.get(
                endpoint="/jd-stats/jd_dashboard",
                user_id=UUID(user_id)
            )

            return result_data

        except Exception as e:
            logger.error(f"获取招聘统计数据失败: {e}")
            # 统计属于辅助展示：远端不可用或 user_id 非法时返回零值，不让仪表盘整体报错。
            return {"total": 0, "change": 0}

    async def _get_training_stats(self, user_id: str) -> Dict[str, Any]:
        """统计用户简历评价总数及近七天新增占比，查询失败时返回零值。"""
        try:
            # 所有计数都在 SQL 中带 user_id 条件，数据库只返回聚合数字，
            # 不会把其他用户的简历评价加载到应用内存。
            total_query = select(func.count(ResumeEvaluation.id)).where(
                ResumeEvaluation.user_id == user_id
            )
            total_result = await self.db.execute(total_query)
            total_count = total_result.scalar() or 0

            # 以当前 UTC 时间向前取七天，统计这个时间窗口内新增的评价。
            week_ago = datetime.utcnow() - timedelta(days=7)
            recent_query = select(func.count(ResumeEvaluation.id)).where(
                and_(
                    ResumeEvaluation.user_id == user_id,
                    ResumeEvaluation.created_at >= week_ago
                )
            )
            recent_result = await self.db.execute(recent_query)
            recent_count = recent_result.scalar() or 0

            # change 表示“近七天新增数占当前总数的比例”，不是与上一个七天周期相比的环比。
            # total 为零时跳过除法，避免统计接口因空数据产生异常。
            growth_rate = 0
            if total_count > 0:
                growth_rate = round((recent_count / total_count) * 100, 2)

            return {
                "total": total_count,
                "change": growth_rate
            }

        except Exception as e:
            logger.error(f"获取简历统计数据失败: {e}")
            return {"total": 0, "change": 0}

    async def _get_interview_stats(self, user_id: str) -> Dict[str, Any]:
        """统计用户待面试评价及近七天新增占比，并按前端约定返回负变化值。"""
        try:
            # 待面试数量只统计当前用户、INTERVIEW 状态且未软删除的评价。
            pending_query = select(func.count(ResumeEvaluation.id)).where(
                and_(
                    ResumeEvaluation.user_id == user_id,
                    ResumeEvaluation.status == ResumeStatus.INTERVIEW,
                    ResumeEvaluation.is_active == True
                )
            )
            pending_result = await self.db.execute(pending_query)
            pending_count = pending_result.scalar() or 0

            # 在相同过滤条件上追加七天时间窗，保证分子与分母属于同一数据集合。
            week_ago = datetime.utcnow() - timedelta(days=7)
            recent_query = select(func.count(ResumeEvaluation.id)).where(
                and_(
                    ResumeEvaluation.user_id == user_id,
                    ResumeEvaluation.status == ResumeStatus.INTERVIEW,
                    ResumeEvaluation.is_active == True,
                    ResumeEvaluation.created_at >= week_ago
                )
            )
            recent_result = await self.db.execute(recent_query)
            recent_count = recent_result.scalar() or 0

            # change 使用近期新增待处理项的占比；返回负值是前端展示约定，
            # 用来表达“待处理面试减少”的方向，并非一次周期环比计算。
            change_rate = 0
            if pending_count > 0:
                change_rate = round((recent_count / pending_count) * 100, 2)

            return {
                "total": pending_count,
                "change": -change_rate
            }

        except Exception as e:
            logger.error(f"获取面试统计数据失败: {e}")
            return {"total": 0, "change": 0}

    async def _get_assistant_stats(self, user_id: str) -> Dict[str, Any]:
        """统计用户会话总数及近七天新增占比，查询失败时返回零值。"""
        try:
            # 会话统计同样先在 SQL 层按用户隔离，只读取 count 聚合值。
            total_query = select(func.count(Conversation.id)).where(
                Conversation.user_id == user_id
            )
            total_result = await self.db.execute(total_query)
            total_count = total_result.scalar() or 0

            # 近七天查询复用相同用户条件，再追加创建时间下界。
            week_ago = datetime.utcnow() - timedelta(days=7)
            recent_query = select(func.count(Conversation.id)).where(
                and_(
                    Conversation.user_id == user_id,
                    Conversation.created_at >= week_ago
                )
            )
            recent_result = await self.db.execute(recent_query)
            recent_count = recent_result.scalar() or 0

            # 与简历卡片保持同一口径：近期新增数占当前总数的百分比。
            growth_rate = 0
            if total_count > 0:
                growth_rate = round((recent_count / total_count) * 100, 2)

            return {
                "total": total_count,
                "change": growth_rate
            }

        except Exception as e:
            logger.error(f"获取AI助手统计数据失败: {e}")
            return {"total": 0, "change": 0}

    async def get_recruitment_trend_data(self, user_id: str, days: int = 30) -> Dict[str, Any]:
        """
        获取招聘趋势数据

        Args:
            user_id: 用户ID
            days: 天数范围

        Returns:
            包含趋势数据的字典
        """
        try:
            # 招聘趋势完全由远程 JD 统计服务计算；days 作为查询窗口传递，
            # 本地不重算日期序列，避免两个服务对时间边界的理解不一致。
            result_data = await remote_service_client.get(
                endpoint="/jd-stats/jd-recruitment-trend",
                user_id=UUID(user_id),
                additional_params={"days": days}
            )

            return result_data

        except Exception as e:
            logger.error(f"获取招聘趋势数据失败: {e}")
            # 返回形状稳定的空序列，前端图表无需区分“无数据”和“远程统计暂不可用”。
            return {"dates": [], "counts": []}

    async def get_training_completion_stats(self, user_id: str) -> Dict[str, Any]:
        """
        获取简历评价分布统计

        Args:
            user_id: 用户ID

        Returns:
            包含简历评价分布统计数据的字典
        """
        try:
            # 三条聚合查询都在数据库侧按当前用户计算，只把计数结果带回应用层。
            total_query = select(func.count(ResumeEvaluation.id)).where(
                ResumeEvaluation.user_id == user_id
            )
            total_result = await self.db.execute(total_query)
            total_count = total_result.scalar() or 0

            # 高分区间包含 80 分。
            high_score_query = select(func.count(ResumeEvaluation.id)).where(
                and_(
                    ResumeEvaluation.user_id == user_id,
                    ResumeEvaluation.total_score >= 80
                )
            )
            high_score_result = await self.db.execute(high_score_query)
            high_score_count = high_score_result.scalar() or 0

            # 中分区间为 [60, 80)，与高分条件无重叠；低分比例稍后用总比例扣除得到。
            medium_score_query = select(func.count(ResumeEvaluation.id)).where(
                and_(
                    ResumeEvaluation.user_id == user_id,
                    ResumeEvaluation.total_score >= 60,
                    ResumeEvaluation.total_score < 80
                )
            )
            medium_score_result = await self.db.execute(medium_score_query)
            medium_score_count = medium_score_result.scalar() or 0

            # 空集合时高、中分比例设为 0 以避免除零；低分沿用“100% 减去前两类”的现有口径，
            # 因而空集合也会得到 low_score=100，而查询异常走下方另一套默认展示比例。
            high_percentage = round((high_score_count / total_count) * 100, 2) if total_count > 0 else 0
            medium_percentage = round((medium_score_count / total_count) * 100, 2) if total_count > 0 else 0
            low_percentage = round(100 - high_percentage - medium_percentage, 2)

            return {
                "high_score": high_percentage,
                "medium_score": medium_percentage,
                "low_score": low_percentage
            }

        except Exception as e:
            logger.error(f"获取简历评价分布统计失败: {e}")
            # 查询失败与“确实没有评价”含义不同：这里返回固定演示比例，保证图表仍可渲染。
            return {
                "high_score": 25,
                "medium_score": 50,
                "low_score": 25
            }

    async def get_recent_activities(self, user_id: str, limit: int = 10, offset: int = 0) -> Dict[str, Any]:
        """
        获取最近活动记录（支持分页）

        Args:
            user_id: 用户ID
            limit: 返回记录数限制
            offset: 偏移量

        Returns:
            包含活动记录和分页信息的字典
        """
        try:
            # 三个来源先归一化为相同字典结构，再统一排序和分页。
            # 每个来源都有独立 try/except，单一来源失败时仍可返回其余活动。
            activities = []

            # JD 活动来自远程服务，本地没有对应表。
            try:
                jd_activities = await remote_service_client.get(
                    endpoint="/jd-stats/jd-recent-activities",
                    user_id=UUID(user_id)
                )

                for activity in jd_activities:
                    # 统一排序键类型：远程 JSON 中的 ISO 字符串需先恢复为 datetime，
                    # 才能与本地 ORM 记录的 datetime 放在一起比较。
                    if isinstance(activity.get('created_at'), str):
                        try:
                            activity['created_at'] = datetime.fromisoformat(activity['created_at'])
                        except (ValueError, TypeError):
                            # 无法解析的远程时间不能参与可靠排序，现有降级策略将其视作刚刚发生。
                            activity['created_at'] = datetime.utcnow()
                    activities.append(activity)
            except Exception as e:
                # 只放弃 JD 来源，不清空已经收集或随后可获取的本地来源。
                logger.warning(f"获取职位描述记录失败: {e}")

            # 本地简历评价查询在 SQL 层按用户隔离，并按时间倒序读取。
            try:
                resume_query = select(ResumeEvaluation).where(
                    ResumeEvaluation.user_id == user_id
                ).order_by(ResumeEvaluation.created_at.desc())

                resume_result = await self.db.execute(resume_query)
                resume_records = resume_result.scalars().all()

                # ORM 实体转换为前端统一活动结构，同时保留 created_at 供最终跨来源排序。
                for record in resume_records:
                    candidate_name = record.candidate_name or "未知候选人"
                    activities.append({
                        "id": str(record.id),
                        "type": "training",
                        "icon": "Reading",
                        "title": f"评价了{candidate_name}的简历",
                        "time": self._format_time_diff(record.created_at),
                        "created_at": record.created_at
                    })
            except Exception as e:
                logger.warning(f"获取简历评价记录失败: {e}")

            # 对话记录使用相同用户范围和排序方式，然后映射为 assistant 类型活动。
            try:
                conversation_query = select(Conversation).where(
                    Conversation.user_id == user_id
                ).order_by(Conversation.created_at.desc())

                conversation_result = await self.db.execute(conversation_query)
                conversation_records = conversation_result.scalars().all()

                for record in conversation_records:
                    # 会话模型未在这里展开消息内容，活动标题使用固定文案，避免额外查询消息表。
                    title = "与AI助手进行了对话"

                    activities.append({
                        "id": str(record.id),
                        "type": "assistant",
                        "icon": "ChatDotRound",
                        "title": title,
                        "time": self._format_time_diff(record.created_at),
                        "created_at": record.created_at
                    })
            except Exception as e:
                logger.warning(f"获取对话记录失败: {e}")

            # 远程 JD、本地简历和本地会话到此才按真实时间统一排序。
            activities.sort(key=lambda x: x['created_at'], reverse=True)

            # total 是合并后、分页前的活动总量。
            total = len(activities)

            # 当前实现先读取各来源全部记录再内存切片；offset 是跨来源合并后的偏移量。
            paginated_activities = activities[offset:offset + limit]

            return {
                "items": paginated_activities,
                "total": total,
                "page": (offset // limit) + 1 if limit > 0 else 1,
                "size": limit
            }

        except Exception as e:
            logger.exception(f"获取最近活动记录失败: {e}")
            # 只有合并、排序或分页等外层流程整体失败才进入这里；
            # 单个来源失败已在内层降级。固定欢迎记录用于保持响应结构和首页可用性。
            default_activities = [
                {
                    "id": "1",
                    "type": "recruitment",
                    "icon": "Document",
                    "title": "欢迎使用HR助手系统",
                    "time": "刚刚"
                },
                {
                    "id": "2",
                    "type": "training",
                    "icon": "Reading",
                    "title": "开始创建您的第一个职位描述",
                    "time": "刚刚"
                },
                {
                    "id": "3",
                    "type": "assistant",
                    "icon": "ChatDotRound",
                    "title": "与AI助手进行首次对话",
                    "time": "刚刚"
                }
            ]

            return {
                "items": default_activities[:limit],
                "total": len(default_activities),
                "page": 1,
                "size": limit
            }

    def _format_time_diff(self, created_at) -> str:
        """把数据库时间转换成活动列表使用的相对时间文案。"""
        if not created_at:
            return "未知时间"

        now = datetime.utcnow()
        # 本项目以 UTC 比较；移除 ORM 时间值的时区信息，使其能与 naive UTC 时间相减。
        diff = now - created_at.replace(tzinfo=None)

        # 使用整除得到已经完整经过的分钟、小时和天数，再选择最大的可读单位。
        minutes = diff.total_seconds() // 60
        hours = minutes // 60
        days = hours // 24

        if days > 0:
            return f"{int(days)}天前"
        elif hours > 0:
            return f"{int(hours)}小时前"
        elif minutes > 0:
            return f"{int(minutes)}分钟前"
        else:
            return "刚刚"