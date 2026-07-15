"""
简历评价领域服务。

负责简历文件校验与解析、JD 自动匹配、外部 AI 评价与响应解析，以及评价记录的持久化和查询管理。
文件系统写入与数据库提交分别执行，调用方需要关注文件已落盘但记录保存失败的部分成功状态。
"""
import logging
import json
import re
import os
import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
import numpy as np
from app.models.user import User
import aiofiles
from datetime import datetime
from app.core.config import settings
import httpx

from app.models.resume_evaluation import ResumeEvaluation, ResumeStatus
# from app.models.job_description import JobDescription
from app.schemas.resume_evaluation import (
    ResumeEvaluationCreate,
    AIEvaluationResult,
    EvaluationMetric,
    ResumeEvaluationResponse,
    ResumeEvaluationListResponse
)
from app.schemas.job_description import JobDescriptionResponse
from app.services.dify_service import DifyService
from app.services.resume_parser_service import ResumeParserService
from app.services.llm_service import LLMService
from app.services.remote_service_client import remote_service_client
# 通过大模型进行JD匹配，而非embedding

logger = logging.getLogger(__name__)


class ResumeEvaluationService:
    """编排简历解析、职位匹配、AI 评分和评价记录管理。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.dify_service = DifyService()
        self.llm_service = LLMService()
        self.resume_parser = ResumeParserService()

    # ---------- 文件评价主流程 ----------

    async def evaluate_resume(
        self,
        user_id: UUID,
        file_content: bytes,
        filename: str,
        job_description_id: UUID,
        conversation_id: Optional[UUID] = None,
        email_id: Optional[str] = None,
        jd_user_id: Optional[UUID] = None
    ) -> Dict[str, Any]:
        """按指定 JD 评价简历并保存原文件和评价记录。

        文件先写入磁盘，随后评价记录在独立的数据库提交中保存；数据库保存失败只回滚会话，
        不会删除已经写入的文件，因此异常路径可能留下无对应记录的文件。

        Args:
            user_id: 评价记录归属的用户ID
            jd_user_id: JD所属的用户ID，用于查询JD详情。不传则使用user_id
        """
        try:
            # 1. 校验文件元信息并解析正文；这里只验证解析器覆盖的格式、大小和非空文本。
            is_valid, message = self.resume_parser.validate_file(filename, len(file_content))
            if not is_valid:
                raise ValueError(message)

            resume_text = await self.resume_parser.extract_text_from_file(file_content, filename)
            if not resume_text.strip():
                raise ValueError("无法从文件中提取到有效内容")

            file_info = self.resume_parser.get_file_info(filename, file_content)

            # 2. 从远程服务获取 JD，并读取关联评分模型；评分模型获取失败时使用本地默认文本。
            _jd_uid = jd_user_id or user_id
            jd = await self._get_job_description(job_description_id, _jd_uid)
            if not jd:
                raise ValueError("职位描述不存在")

            evaluation_model = await self._get_evaluation_model(job_description_id)

            # 3. 调用外部 Dify 工作流；调用失败会中止，响应解析失败则可能返回默认评价结果。
            ai_result, raw_response = await self._call_dify_evaluation(
                resume_text=resume_text,
                evaluation_model=evaluation_model,
                jd_info=jd
            )

            # 4. 先落盘原文件，再提交评价记录；两者不是同一事务，数据库回滚不会清理文件。
            saved_file_path = await self._save_uploaded_file_content(filename, file_content, user_id)
            logger.info(f"文件已保存到: {saved_file_path}")

            file_info['file_path'] = saved_file_path

            evaluation_record = await self._save_evaluation_result(
                user_id=user_id,
                created_by=user_id,
                file_info=file_info,
                resume_text=resume_text,
                ai_result=ai_result,
                job_description_id=job_description_id,
                conversation_id=conversation_id,
                email_id=email_id,
                raw_response=raw_response
            )

            logger.info(f"评价记录已保存，ID: {evaluation_record.id}, 文件路径: {evaluation_record.file_path}")

            # 数据库提交成功后组装对外结果。
            return {
                "id": evaluation_record.id,
                "evaluation_metrics": [metric.model_dump() for metric in ai_result.evaluation_metrics],
                "total_score": ai_result.total_score,
                "name": ai_result.name,
                "position": ai_result.position,
                "workYears": (self._parse_work_years_to_float(ai_result.workYears) or 0.0),
                "education": ai_result.教育水平,
                "age": ai_result.年龄,
                "sex": ai_result.sex,
                "school": ai_result.school,
                "resume_content": resume_text,
                "original_filename": file_info['filename'],
                "created_at": evaluation_record.created_at.isoformat(),
                "updated_at": evaluation_record.updated_at.isoformat()
            }

        except Exception as e:
            logger.error(f"简历评价失败: {e}")
            raise

    async def _save_uploaded_file_content(self, filename: str, file_content: bytes, user_id: UUID) -> str:
        """把已校验的上传内容保存到当前用户目录，并返回最终磁盘路径。"""
        try:
            # 用户 UUID 形成一级目录，避免不同用户同名文件直接冲突。
            upload_dir = os.path.join(settings.UPLOAD_DIR, str(user_id))
            os.makedirs(upload_dir, exist_ok=True)

            # 只取客户端文件名的最后一段，丢弃 ../ 或绝对路径等目录信息，
            # 防止文件被写到配置的上传目录之外。
            safe_filename = Path(filename).name
            file_path = os.path.join(upload_dir, safe_filename)

            # 保留已有文件：同名时依次追加 _1、_2，而不是覆盖历史上传内容。
            counter = 1
            base_name, extension = os.path.splitext(safe_filename)
            original_file_path = file_path

            while os.path.exists(file_path):
                new_filename = f"{base_name}_{counter}{extension}"
                file_path = os.path.join(upload_dir, new_filename)
                counter += 1

            # 异步写盘避免在事件循环中执行同步文件 I/O；写盘成功不代表数据库记录已提交。
            async with aiofiles.open(file_path, 'wb') as f:
                await f.write(file_content)

            return file_path
        except Exception as e:
            logger.error(f"保存上传文件内容失败: {e}")
            raise

    # ---------- 自动匹配 JD ----------

    async def evaluate_resume_auto(
            self,
            user_id: UUID,
            file_content: bytes,
            filename: str,
            subject: str,
            email_id: str = None,
            conversation_id: Optional[UUID] = None
        ) -> Dict[str, Any]:
            """在未指定 JD 时先执行启发式/模型匹配，再复用文件评价主流程。

            当前用户无 JD 时会尝试其他启用用户的 JD；匹配成功后，文件和评价记录均按
            JD 所属用户保存。匹配阶段只读取数据，真正的文件写入和数据库提交发生在
            ``evaluate_resume``，其部分成功边界也随之保留。
            """
            # 匹配前先解析一次；进入 evaluate_resume 后还会再次校验并解析同一文件。
            is_valid, message = self.resume_parser.validate_file(filename, len(file_content))
            if not is_valid:
                raise ValueError(message)
            resume_text = await self.resume_parser.extract_text_from_file(file_content, filename)
            if not resume_text or not resume_text.strip():
                raise ValueError("无法从简历中提取有效文本")
            # 主题优先匹配，未命中时再使用简历文本和候选 JD 列表。
            logger.info(f"开始自动匹配JD，邮件主题: {subject}")
            jd_id, jd_owner_user_id = await self._match_best_jd(subject=subject, resume_text= resume_text, create_by=str(user_id))
            if not jd_id:
                logger.error(f"未匹配到合适的职位描述，邮件主题: {subject}")
                raise ValueError("未匹配到合适的职位描述")
            logger.info(f"匹配到JD: {jd_id}, JD所属用户: {jd_owner_user_id}")
            # 记录归属使用 JD 所属用户，而不是最初传入的 user_id。
            return await self.evaluate_resume(
                user_id=jd_owner_user_id,
                file_content=file_content,
                filename=filename,
                job_description_id=jd_id,
                jd_user_id=jd_owner_user_id,
                conversation_id=conversation_id,
                email_id=email_id
            )

    async def evaluate_resume_text_auto(
        self,
        login_name: str,
        resume_text: str,
        filename: Optional[str] = None,
        subject: str = ""
    ) -> Dict[str, Any]:
        """将文本简历落盘后自动匹配 JD 并评价。

        该方法先按登录用户写入一个文本文件，自动评价内部还会按最终记录归属用户再次保存
        文件。任一后续步骤失败都不会删除已写文件；评价成功后的 file_path 回写单独提交，
        回写失败仅记录警告，不撤销已经提交的评价记录。

        Args:
            login_name: 用户登录名
            resume_text: 简历文本内容
            filename: 文件名（可选）
            subject: 投递邮件主题（可选）

        Returns:
            评价结果字典
        """

        # 验证用户是否存在
        if not login_name:
            raise ValueError("login_name不能为空")

        users = await self.db.execute(select(User).where(User.username == login_name))
        user = users.scalar_one_or_none()
        if not user:
            raise ValueError(f"用户{login_name}不存在")

        user_id = user.id

        # 验证简历文本
        resume_text = (resume_text or "").strip()
        if not resume_text:
            raise ValueError("简历文本内容不能为空")

        # 生成或使用提供的文件名
        safe_default_name = f"resume_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        filename = (filename or safe_default_name).strip() or safe_default_name
        # 简单清理可能的非法字符
        filename = Path(filename).name
        if not filename.lower().endswith('.txt'):
            filename = f"{filename}.txt"

        # 首次落盘发生在匹配和评价之前，后续失败不会清理该文件。
        user_dir = os.path.join(settings.UPLOAD_DIR, str(user_id))
        os.makedirs(user_dir, exist_ok=True)
        file_path = os.path.join(user_dir, filename)

        async with aiofiles.open(file_path, "wb") as f:
            await f.write(resume_text.encode("utf-8"))

        # 调用自动匹配与评分服务
        result = await self.evaluate_resume_auto(
            user_id=user_id,
            file_content=resume_text.encode("utf-8"),
            filename=filename,
            subject=subject or ""
        )

        # 尝试把记录路径改为首次落盘路径；该提交失败不会影响主评价结果。
        try:
            from app.models.resume_evaluation import ResumeEvaluation
            eval_id = result.get("id")
            if eval_id:
                stmt = select(ResumeEvaluation).where(
                    ResumeEvaluation.id == eval_id,
                    ResumeEvaluation.user_id == user_id,
                )
                db_result = await self.db.execute(stmt)
                evaluation = db_result.scalar_one_or_none()
                if evaluation:
                    evaluation.file_path = file_path
                    await self.db.commit()
        except Exception as e:
            # 不影响主流程，记录日志
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"评价记录回写 file_path 失败: {e}")

        return result

    async def _fetch_jds(self, user_id: UUID) -> List[JobDescriptionResponse]:
        """从远程服务读取一个用户的候选 JD，并校验为本地响应模型。"""
        # 自动匹配只考察远程列表的前 100 条；分页之外的 JD 不进入候选集合。
        result_data = await remote_service_client.get(
            endpoint="job-descriptions/",
            user_id=user_id,
            additional_params={"page": 1, "size": 100}
        )
        jd_items = result_data.get("items", [])
        # 每条远程 JSON 都经过 Schema 校验，字段不完整会使本次候选获取失败而不是静默参与匹配。
        return [JobDescriptionResponse(**jd_data) for jd_data in jd_items]

    async def _match_best_jd(self, subject:str ,resume_text: str,create_by: str) -> Tuple[Optional[UUID], Optional[UUID]]:
        """按主题、LLM 和关键词回退顺序选择一个候选 JD。

        先读取指定用户的远程 JD；为空时最多遍历 10 个本地启用用户并取首个有 JD 的用户。
        主题匹配使用包含关系和字符重叠，之后才调用 LLM；仅 LLM 调用抛错时进入固定关键词
        回退，整体获取失败或没有可识别 ID 时返回 ``(None, None)``。这些规则是启发式匹配，
        不代表已确认候选人与岗位在语义上适配。

        Returns:
            (jd_id, jd_owner_user_id) - 匹配的JD ID和该JD所属的用户ID
        """
        logger.info(f"开始查询职位描述，create_by: {create_by}")
        try:
            # 优先获取当前用户的远程 JD 列表。
            jds = await self._fetch_jds(UUID(create_by))
            jd_owner_user_id = UUID(create_by)  # 记录当前JD所属的用户ID

            # 当前用户无 JD 时，依次查询有限数量的其他启用用户。
            if not jds:
                logger.info(f"用户 {create_by} 没有JD，尝试查找其他本地用户的JD")
                try:
                    from sqlalchemy import select as sql_select
                    users_result = await self.db.execute(
                        sql_select(User).where(User.is_active == True).limit(10)
                    )
                    local_users = users_result.scalars().all()
                    for u in local_users:
                        if str(u.id) == create_by:
                            continue
                        logger.info(f"尝试查询用户 {u.id} 的JD")
                        jds = await self._fetch_jds(u.id)
                        if jds:
                            logger.info(f"从用户 {u.id} 找到 {len(jds)} 个JD")
                            jd_owner_user_id = u.id
                            break
                except Exception as e:
                    logger.warning(f"查找其他用户JD失败: {e}")

            logger.info(f"查询到 {len(jds)} 个职位描述")

            # 输出所有JD标题用于调试
            for jd in jds:
                logger.info(f"JD标题: {jd.title}")

            if not jds:
                return None, None

            # 新增逻辑：根据邮件主题关键词匹配职位描述
            # 主题格式：简历-职位名称-姓名
            if subject and "简历-" in subject:
                try:
                    # 解析主题中的职位名称
                    parts = subject.split("-")
                    if len(parts) >= 2:
                        position_name = parts[1].strip()  # 获取职位名称部分
                        logger.info(f"从邮件主题解析出职位名称: {position_name}")

                        # 在JD列表中查找匹配的职位名称
                        for jd in jds:
                            if jd.title:
                                # 使用更宽松的匹配策略，忽略大小写
                                jd_title_clean = jd.title.replace(" ", "").replace("-", "").lower()
                                position_name_clean = position_name.replace(" ", "").replace("-", "").lower()

                                logger.info(f"比较职位名称: '{position_name_clean}' 与 JD标题: '{jd_title_clean}'")

                                # 多种匹配方式
                                if (position_name_clean in jd_title_clean or
                                    jd_title_clean in position_name_clean):
                                    logger.info(f"通过邮件主题匹配到JD: {jd.title}")
                                    return jd.id, jd_owner_user_id

                        # 如果没有精确匹配，尝试模糊匹配
                        logger.info("未找到精确匹配，尝试模糊匹配...")
                        best_match = self._fuzzy_match_jd(position_name, jds)
                        if best_match:
                            logger.info(f"通过模糊匹配找到JD: {best_match.title}")
                            return best_match.id, jd_owner_user_id

                except Exception as e:
                    logger.error(f"邮件主题解析失败: {e}")

            # 只向模型提供候选 id 和标题，并限制候选 JSON、简历正文长度，控制提示词大小和调用成本。
            jd_options = [
                {
                    "id": str(jd.id),
                    "title": jd.title
                }
                for jd in jds
            ]
            # 提示词把模型输出限制为候选 id 或 None；模型结果仍是不可信文本，后面必须反查候选集合。
            prompt = (
                "你是一个职位匹配助手。"
                "1、如果候选人投递邮件的主题中包括了他要投递的岗位，则直接根据主题subject从给定的JD列表中选择最匹配的一项，匹配到直接输出jd id，不需要再根据简历内容匹配。若没有匹配到，则直接输出None"
                "2、如果投递邮件主题中没有要投递的岗位，则从候选人的简历内容，去从给定的JD列表中选择最匹配的一项。如果没有匹配到合适的岗位，则直接输出None"
                "输出要求：不要给出匹配理由，直接输出jd id的值,或者None"
            )
            try:
                # LLM 只负责从已有候选中推荐，不具备创建或授权 JD 的能力。
                import json as _json
                jd_compact = _json.dumps(jd_options, ensure_ascii=False)[:12000]
                llm_input = (
                    f"{prompt}\n\n候选人投递邮件主题：{subject}\n\n候选人简历：\n{resume_text[:8000]}\n\nJD列表(JSON)：\n{jd_compact}\n\n"
                    " "
                )
                jd_id_str = await self.llm_service.generate_response(message=llm_input)
                print(f"LLM JD匹配结果：{jd_id_str}")
                if not jd_id_str:
                    raise ValueError("匹配结果不包含jd_id")

                # 不能把模型输出直接转成 UUID 返回：只有原候选集合中确实存在的 id 才可信。
                for jd in jds:
                    if str(jd.id) in jd_id_str:
                        print(f"LLM JD匹配成功：{jd.title}")
                        return jd.id, jd_owner_user_id
                # 输出无法对应候选项时视作“未匹配”，不进入异常回退。
                return None, None
            except Exception as e:
                logger.error(f"LLM JD匹配失败，回退关键词匹配: {e}")
                # 回退仅计算固定关键词共现；即使全部为 0，也会选择列表中的第一项。
                keywords = ["Java", "Python", "前端", "后端", "AI", "算法", "产品", "测试"]
                scores = []
                lower_text = resume_text.lower()
                for jd in jds:
                    agg = " ".join([jd.title or "", jd.requirements or "", jd.skills or ""]).lower()
                    score = sum(1 for kw in keywords if kw.lower() in lower_text and kw.lower() in agg)
                    scores.append(score)
                best_idx = int(np.argmax(scores)) if scores else 0
                return (jds[best_idx].id, jd_owner_user_id) if jds else (None, None)
        except Exception as e:
            logger.error(f"获取所有JD失败: {e}")
            return None, None

    def _partial_match(self, term1: str, term2: str) -> bool:
        """用包含关系或任意三字符公共子串判断两个已规范化文本是否部分匹配。

        该启发式不计算语义相似度，短于三字符且互不包含的文本直接视为不匹配。
        """
        if not term1 or not term2:
            return False

        # 如果一个字符串包含另一个字符串
        if term1 in term2 or term2 in term1:
            return True

        # 检查是否有共同的子串（至少3个字符）
        min_len = min(len(term1), len(term2))
        if min_len >= 3:
            for i in range(min_len - 2):
                substr = term1[i:i+3] if len(term1[i:i+3]) == 3 else None
                if substr and substr in term2:
                    return True

        return False

    def _fuzzy_match_jd(self, position_name: str, jds: List[JobDescriptionResponse]) -> Optional[JobDescriptionResponse]:
        """按标题包含关系和字符集合重叠率返回第一个启发式匹配的 JD。

        输入会去除空格、连字符并转小写；包含关系优先，否则 Jaccard 字符重叠率超过 0.3
        即命中。方法按原列表顺序短路，不在多个命中项之间比较最佳分数。
        """
        if not position_name or not jds:
            return None

        position_name_clean = position_name.replace(" ", "").replace("-", "").lower()
        logger.info(f"开始模糊匹配职位名称: {position_name_clean}")

        # 尝试多种匹配方式
        for jd in jds:
            if jd.title:
                jd_title_clean = jd.title.replace(" ", "").replace("-", "").lower()
                logger.info(f"比较职位名称: '{position_name_clean}' 与 JD标题: '{jd_title_clean}'")

                # 1. 直接包含关系
                if position_name_clean in jd_title_clean or jd_title_clean in position_name_clean:
                    logger.info(f"模糊匹配成功: {position_name} 匹配到 {jd.title}")
                    return jd

                # 2. 关键词匹配
                position_keywords = set(position_name_clean)
                jd_keywords = set(jd_title_clean)

                # 如果有超过30%的字符重叠，认为匹配（降低阈值）
                union_len = len(position_keywords | jd_keywords)
                if union_len > 0:
                    overlap_ratio = len(position_keywords & jd_keywords) / union_len
                    logger.info(f"关键词重叠比例: {overlap_ratio}")
                    if overlap_ratio > 0.3:
                        logger.info(f"通过关键词匹配成功: {position_name} 匹配到 {jd.title}")
                        return jd

        return None

    # ---------- AI 调用与解析 ----------

    async def _get_job_description(self, jd_id: UUID, user_id: UUID) -> Optional[JobDescriptionResponse]:
        """在指定用户上下文中读取远程 JD，并用本地 Schema 校验响应。

        远程错误、无权限、未命中或字段校验失败都记录日志并返回 ``None``，上层评价流程统一
        将其视为“职位描述不存在”，本方法不写本地数据库。
        """
        try:
            result_data = await remote_service_client.get(
                endpoint=f"job-descriptions/{jd_id}",
                user_id=user_id
            )
            return JobDescriptionResponse(**result_data)
        except Exception as e:
            logger.error(f"获取职位描述失败: {e}")
            return None

    async def _get_evaluation_model(self, jd_id: UUID) -> str:
        """读取远程评分标准正文；任何不可用状态都降级为本地默认模板。

        该专用远程调用只按 JD ID 查询，没有附带当前用户 ID。非空 ``content`` 才会被接受，
        网络、状态码、响应结构和空内容异常均被吸收，不阻断后续 Dify 评价。
        """
        try:
            # 评分标准由远程 HR 服务维护。该专用接口只按 JD id 查询，
            # 与其他远程调用不同，这里没有附加 current user id，而是直接使用公共请求头。
            url = f"{remote_service_client.base_url}/scoring-criteria/by-jd/{jd_id}"
            headers = remote_service_client._get_headers()

            # 复用统一客户端的超时和响应处理规则，但为此专用路径单独发起 GET。
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=headers,
                    timeout=remote_service_client.timeout
                )

            result_data = remote_service_client._handle_response(response)

            # 只有非空 content 才能作为模型提示；空响应或缺字段都降级为本地默认评分维度。
            if result_data and result_data.get('content'):
                return result_data['content']

            return self._get_default_evaluation_model()

        except Exception as e:
            logger.error(f"获取评价模型失败: {e}")
            # 评分标准不可用不阻断简历评价，Dify 将收到本地内置模板。
            return self._get_default_evaluation_model()

    def _get_default_evaluation_model(self) -> str:
        """返回远程评分标准不可用时使用的固定维度与 JSON 输出契约。"""
        return """
        请根据以下职位要求对简历进行评价，并按照指定的JSON格式返回结果：

        评价维度：
        1. 学历匹配度 (0-20分)
        2. 工作经验匹配度 (0-25分)
        3. 技能匹配度 (0-25分)
        4. 项目经验匹配度 (0-20分)
        5. 综合素质 (0-10分)

        请提取简历中的以下信息：
        - 姓名
        - 应聘岗位
        - 工作年限
        - 教育水平
        - 年龄
        - 性别
        - 毕业院校

        返回格式必须是有效的JSON：
        {
          "evaluation_metrics": [
            {
              "name": "学历",
              "score": 15,
              "max": 20,
              "reason": "本科学历，符合岗位要求"
            }
          ],
          "total_score": 85,
          "name": "张三",
          "position": "前端开发工程师",
          "workYears": "3年",
          "education": "本科",
          "age": 28,
          "sex": "男",
          "school": "上海理工大学"
        }
        """

    async def _call_dify_evaluation(
        self,
        resume_text: str,
        evaluation_model: str,
        jd_info: JobDescriptionResponse
    ) -> tuple[AIEvaluationResult, str]:
        """调用 Dify 简历评价工作流并解析返回值。

        工作流请求是远程副作用，失败时转换为“服务暂时不可用”异常继续向上抛出；请求成功后，
        解析器可能用默认结果吸收格式错误。本方法不写文件或数据库。
        """
        try:
            # 工作流类型 3 固定对应简历评价：评分标准作为 query，简历正文与岗位名作为额外输入。
            # 这里只发送远程请求，不进行数据库或文件写入。
            response = await self.dify_service.call_workflow_sync(
                workflow_type=3,
                query=evaluation_model,
                additional_inputs={
                    "jianli": resume_text,
                    "jobName": jd_info.title
                }
            )

            # 结构化结果供数据库字段使用，原始响应同时保留用于追踪模型输出和排查解析问题。
            ai_result = self._parse_ai_response(response)
            raw_response = str(response)
            return ai_result, raw_response

        except Exception as e:
            logger.error(f"调用Dify API失败: {e}")
            # 远程调用失败与“响应格式错误”不同：前者中止评价，后者由解析器生成默认结果继续保存。
            raise Exception(f"AI评价服务暂时不可用: {str(e)}")

    def _parse_ai_response(self, response: Dict[str, Any]) -> AIEvaluationResult:
        """从常见 answer 位置提取 JSON，并兼容部分字段别名。

        这里只检查两个核心字段是否存在，再依赖 Schema 构造结果，并不验证评分业务合理性。
        JSON、字段或类型解析失败时返回固定默认评价，而不是抛出解析异常。
        """
        try:
            # Dify 版本/工作流可能把答案放在顶层或 data.answer；都不存在时把完整响应转成文本，
            # 让后续 JSON 提取仍有机会找到嵌套对象。
            answer_text = ""
            if "answer" in response:
                answer_text = response["answer"]
            elif "data" in response and "answer" in response["data"]:
                answer_text = response["data"]["answer"]
            else:
                answer_text = str(response)

            if not answer_text:
                raise ValueError("AI响应为空")

            # 按“纯 JSON → Markdown 代码块 → 首尾花括号片段”的顺序兼容常见模型输出。
            try:
                if answer_text.startswith('{') and answer_text.endswith('}'):
                    result_data = json.loads(answer_text)
                else:
                    if '```json' in answer_text:
                        start = answer_text.find('```json') + 7
                        end = answer_text.find('```', start)
                        json_str = answer_text[start:end].strip()
                    elif '```' in answer_text:
                        start = answer_text.find('```') + 3
                        end = answer_text.find('```', start)
                        json_str = answer_text[start:end].strip()
                    else:
                        # 对带解释文字的答案，仅截取最外层第一个 { 到最后一个 }。
                        json_start = answer_text.find('{')
                        json_end = answer_text.rfind('}') + 1
                        if json_start != -1 and json_end > json_start:
                            json_str = answer_text[json_start:json_end]
                        else:
                            raise ValueError("No valid JSON found in response")

                    # 解析JSON
                    result_data = json.loads(json_str)

                # 两个顶层字段是继续构造结果的最低要求；缺失会进入外层默认评分分支。
                if 'evaluation_metrics' not in result_data:
                    raise ValueError("缺少evaluation_metrics字段")

                if 'total_score' not in result_data:
                    raise ValueError("缺少total_score字段")

                # 将模型的自由字典逐项收窄成 EvaluationMetric；缺失字段使用稳定默认值。
                # 当前指标名称读取中文键“评价指标”，模型若只返回 name 会得到空名称。
                metrics = []
                for metric_data in result_data.get('evaluation_metrics', []):
                    metric = EvaluationMetric(
                        name=metric_data.get('评价指标', ''),
                        score=metric_data.get('score', 0),
                        max=metric_data.get('max', 100),
                        reason=metric_data.get('reason', '')
                    )
                    metrics.append(metric)

                # 规范化字段别名，兼容不同返回命名
                normalized_work_years = (
                    result_data.get('workYears')
                    or result_data.get('work_years')
                    or result_data.get('work_experience')
                    or result_data.get('工作年限')
                    or result_data.get('工作经验')
                )
                # 处理workYears字段，确保能正确转换为float
                try:
                    if normalized_work_years is not None and normalized_work_years != '' and normalized_work_years != '未知':
                        normalized_work_years = float(normalized_work_years)
                    else:
                        normalized_work_years = 0.0
                except (ValueError, TypeError):
                    normalized_work_years = 0.0
                normalized_education = (
                    result_data.get('education')
                    or result_data.get('education_level')
                    or result_data.get('学历')
                    or result_data.get('教育水平')
                )
                try:
                    normalized_age = int(result_data.get('age', result_data.get('年龄', 0)))
                except (ValueError, TypeError):
                    normalized_age = 0

                normalized_sex = (
                    result_data.get('sex')
                    or result_data.get('gender')
                    or result_data.get('性别')
                )
                normalized_school = (
                    result_data.get('school')
                    or result_data.get('毕业院校')
                    or result_data.get('院校')
                    or result_data.get('学校')
                )

                # 构建AI评价结果
                ai_result = AIEvaluationResult(
                    evaluation_metrics=metrics,
                    total_score=result_data.get('total_score', 0),
                    name=result_data.get('name', ''),
                    position=result_data.get('position', ''),
                    workYears=normalized_work_years,
                    教育水平=normalized_education or '',
                    年龄=normalized_age,
                    sex=normalized_sex or '',
                    school=normalized_school or ''
                )

                return ai_result

            except json.JSONDecodeError as e:
                logger.error(f"JSON解析失败: {e}, 原始响应: {answer_text}")
                # 返回默认结果
                return self._create_default_result(answer_text)

        except Exception as e:
            logger.error(f"解析AI响应失败: {e}")
            return self._create_default_result(str(e))

    def _create_default_result(self, raw_response: str) -> AIEvaluationResult:
        """在模型响应无法解析时构造可持久化的固定 60 分结果。

        ``raw_response`` 当前仅用于保留调用签名，默认字段不从失败文本推断候选信息；原始响应
        会由上层另行写入评价记录，便于排查而不污染结构化字段。
        """
        return AIEvaluationResult(
            evaluation_metrics=[
                EvaluationMetric(
                    name="综合评价",
                    score=60,
                    max=100,
                    reason="AI解析失败，给出默认评分"
                )
            ],
            total_score=60,
            name="未知",
            position="未知",
            workYears=0.0,
            教育水平="未知",
            年龄=None,
            sex="未知",
            school="未知"
        )

    # ---------- 持久化与查询管理 ----------

    async def _save_evaluation_result(
        self,
        user_id: UUID,
        created_by: UUID,
        file_info: Dict[str, Any],
        resume_text: str,
        ai_result: AIEvaluationResult,
        job_description_id: UUID,
        raw_response: str = "",
        email_id: Optional[str] = None,
        conversation_id: Optional[UUID] = None
    ) -> ResumeEvaluation:
        """使用已落盘文件的信息创建评价记录并立即提交。

        提交或刷新失败时只回滚当前数据库会话；若提交已成功而刷新失败，回滚也不能撤销
        已提交记录。传入路径对应文件已由上游写入，此处不会删除文件或撤销远程 AI 调用。
        """
        try:
            # 把文件元信息、解析正文和 AI 结构化结果汇总为单条 ORM 记录。
            # evaluation_metrics 从 Pydantic 对象转为普通字典列表，以便写入 JSON 字段。
            evaluation = ResumeEvaluation(
                user_id=user_id,
                created_by = created_by,
                updated_by= created_by,
                email_id =  email_id,
                original_filename=file_info['filename'],
                file_path=file_info.get('file_path'),
                file_type=file_info['file_type'],
                file_size=file_info['file_size'],
                resume_content=resume_text,
                candidate_name=ai_result.name,
                candidate_position=ai_result.position,
                candidate_age=ai_result.年龄,
                candidate_gender=ai_result.sex,
                work_years=(self._parse_work_years_to_float(ai_result.workYears) or 0.0),
                education_level=ai_result.教育水平,
                school=ai_result.school,
                total_score=ai_result.total_score,
                evaluation_metrics=[metric.model_dump() for metric in ai_result.evaluation_metrics],
                job_description_id=job_description_id,
                conversation_id=str(conversation_id) if conversation_id else None,
                ai_response=raw_response
            )
            self.db.add(evaluation)
            # commit 才使记录对其他事务可见；refresh 再取回数据库生成的 id 和时间戳。
            await self.db.commit()
            await self.db.refresh(evaluation)

            return evaluation

        except Exception as e:
            # 回滚只作用于尚未提交的数据库变更，不会撤销上游文件写入和 Dify 调用。
            await self.db.rollback()
            logger.error(f"保存评价结果失败: {e}")
            raise

    async def get_evaluation_history(
        self,
        user_id: UUID,
        skip: int = 0,
        limit: int = 20,
        status: Optional['ResumeStatus'] = None
    ) -> tuple[List[ResumeEvaluation], int]:
        """按当前用户和可选状态查询一页评价，并同时返回相同条件下的总数。"""
        try:
            # user_id 是查询基线，确保后续状态过滤、计数和分页始终处于同一用户范围。
            query = select(ResumeEvaluation).where(
                ResumeEvaluation.user_id == user_id
            ).order_by(ResumeEvaluation.created_at.desc())

            if status:
                query = query.where(ResumeEvaluation.status == status)

            # 在添加 offset/limit 之前包装为子查询，因此 total 表示全部匹配数而不是当前页数量。
            count_query = select(func.count()).select_from(query.subquery())
            count_result = await self.db.execute(count_query)
            total = count_result.scalar()

            # 只在实体查询上应用分页，排序保证翻页顺序稳定。
            query = query.offset(skip).limit(limit)

            result = await self.db.execute(query)
            evaluations = result.scalars().all()

            # 添加调试日志
            logger.info(f"查询到 {len(evaluations)} 个评价记录，总记录数: {total}")
            if evaluations:
                logger.info(f"第一个评价记录类型: {type(evaluations[0])}")
                if hasattr(evaluations[0], 'id'):
                    logger.info(f"第一个评价记录ID: {evaluations[0].id}")
                else:
                    logger.info("第一个评价记录没有id属性")

            return evaluations, total

        except Exception as e:
            logger.error(f"获取评价历史失败: {e}", exc_info=True)
            # 历史列表属于可降级读取：查询异常被转换为空页，调用方不会收到数据库异常。
            return [], 0

    async def get_evaluation_by_id(
        self,
        evaluation_id: UUID,
        user_id: UUID
    ) -> Optional[ResumeEvaluation]:
        """按评价 id 和用户 id 联合读取，未命中或查询失败均返回 None。"""
        try:
            # 将资源 id 与归属用户写在同一条 SQL 中，避免先查资源后在应用层遗漏权限判断。
            result = await self.db.execute(
                select(ResumeEvaluation)
                .where(
                    ResumeEvaluation.id == evaluation_id,
                    ResumeEvaluation.user_id == user_id
                )
            )
            return result.scalar_one_or_none()

        except Exception as e:
            logger.error(f"获取评价结果失败: {e}")
            return None

    def _parse_work_years_to_float(self, text: Optional[str]) -> Optional[float]:
        """将工作年限字符串解析为数字（年）。支持格式如"3年"、"1.5年"、"1-3年"、"约2年"。
        - 解析到范围时取平均值；
        - 仅提取到一个数值时使用该数值；
        - 解析失败返回None。
        """
        if not text:
            return None
        s = str(text).strip().lower()
        # 常见非数值占位统一视为0（回退到调用处做 or 0.0）
        if s in {"未知", "不详", "none", "null", "n/a", "na", "--", "-", "", "应届", "应届生", "fresh"}:
            return 0.0
        # 匹配范围 "a-b" 或 "a – b"
        m_range = re.search(r"(\d+(?:\.\d+)?)\s*[\-~–—]\s*(\d+(?:\.\d+)?)", s)
        if m_range:
            try:
                a = float(m_range.group(1))
                b = float(m_range.group(2))
                return (a + b) / 2.0
            except Exception:
                pass
        # 提取第一个数字
        m_single = re.search(r"(\d+(?:\.\d+)?)", s)
        if m_single:
            try:
                return float(m_single.group(1))
            except Exception:
                return None
        return None

    # 静态方法用于参数验证
    @staticmethod
    async def validate_uuid_param(param: str, param_name: str) -> UUID:
        """把路径字符串收窄为 UUID，并用业务参数名包装格式错误。"""
        try:
            return UUID(param)
        except ValueError:
            raise ValueError(f"无效的{param_name}格式")

    @staticmethod
    async def validate_evaluation_params(
        job_description_id: str,
        conversation_id: Optional[str] = None
    ) -> Tuple[UUID, Optional[UUID]]:
        """一次性规范化必填 JD ID 和可选会话 ID，任一非法都拒绝整组参数。"""
        # 必填 JD ID 先校验，失败时不会继续处理可选会话 ID。
        try:
            jd_uuid = UUID(job_description_id)
        except ValueError:
            raise ValueError("无效的职位描述ID格式")

        # 验证conversation_id格式（如果提供）
        conv_uuid = None
        if conversation_id:
            try:
                conv_uuid = UUID(conversation_id)
            except ValueError:
                raise ValueError("无效的对话ID格式")

        return jd_uuid, conv_uuid

    @staticmethod
    async def validate_status_param(status: Optional[str]) -> Optional[ResumeStatus]:
        """把可选状态字符串转换为枚举；空值表示不应用状态过滤。"""
        if not status:
            return None

        try:
            return ResumeStatus(status)
        except ValueError:
            raise ValueError("无效的状态值，支持的状态: pending, rejected, interview")

    @staticmethod
    async def get_supported_formats() -> Dict[str, Any]:
        """返回上传接口公开的静态文件类型和大小能力描述，不访问解析器或数据库。"""
        return {
            "supported_extensions": [".pdf", ".txt", ".doc", ".docx"],
            "max_file_size": "10MB",
            "description": "支持PDF、TXT、DOC、DOCX格式的简历文件"
        }

    async def get_evaluation_history_with_pagination(
        self,
        user_id: UUID,
        skip: int = 0,
        limit: int = 20,
        status: Optional[ResumeStatus] = None
    ) -> ResumeEvaluationListResponse:
        """把用户范围内的 ORM 分页结果转换为公开列表 Schema。

        底层查询同时返回当前页记录与相同过滤条件下的总数；本方法逐项选择公开字段，并根据
        ``skip``、``limit`` 计算页码。底层查询已将读取异常降级为空页，因此这里保持稳定形状。
        """
        evaluations, total = await self.get_evaluation_history(
            user_id=user_id,
            skip=skip,
            limit=limit,
            status=status
        )

        # ORM 实体逐项转换为公开 Schema，显式控制 API 可见字段，避免直接序列化整张表。
        evaluation_responses = []
        for evaluation in evaluations:
            response = ResumeEvaluationResponse(
                id=evaluation.id,
                original_filename=evaluation.original_filename,
                file_type=evaluation.file_type,
                resume_content=evaluation.resume_content,
                candidate_name=evaluation.candidate_name,
                candidate_position=evaluation.candidate_position,
                candidate_age=evaluation.candidate_age,
                candidate_gender=evaluation.candidate_gender,
                work_years=evaluation.work_years,
                education_level=evaluation.education_level,
                school=evaluation.school,
                total_score=evaluation.total_score,
                evaluation_metrics=evaluation.evaluation_metrics,
                job_description_id=evaluation.job_description_id,
                scoring_criteria_id=evaluation.scoring_criteria_id,
                user_id=evaluation.user_id,
                created_at=evaluation.created_at,
                updated_at=evaluation.updated_at
            )
            evaluation_responses.append(response)

        # skip 是偏移量而不是页码；用整数除法还原当前页，并用向上取整计算总页数。
        # limit<=0 时使用保护值，避免除零。
        page = (skip // limit) + 1 if limit > 0 else 1
        pages = (total + limit - 1) // limit if limit > 0 else 1
        return ResumeEvaluationListResponse(
            items=evaluation_responses,
            total=total,
            page=page,
            size=limit,
            pages=pages
        )

    async def get_evaluation_detail(
        self,
        evaluation_id: UUID,
        user_id: UUID
    ) -> Optional[Dict[str, Any]]:
        """读取当前用户的一条评价，并映射为详情响应使用的扁平字典。

        用户归属在底层联合查询中校验；未命中返回 ``None``。方法还会读取关联远程 JD，但
        当前响应并未使用该结果，因此远程读取只产生诊断价值，不改变返回字段。
        """
        evaluation = await self.get_evaluation_by_id(
            evaluation_id=evaluation_id,
            user_id=user_id
        )

        if not evaluation:
            return None

        # 获取关联的职位描述信息（保留但不参与返回，避免schema不匹配）
        jd = await self._get_job_description(evaluation.job_description_id, user_id)

        # 构建扁平化的结果，符合 ResumeEvaluationResult schema
        result = {
            "id": evaluation.id,
            "evaluation_metrics": evaluation.evaluation_metrics,
            "total_score": evaluation.total_score,
            "name": evaluation.candidate_name,
            "position": evaluation.candidate_position,
            "workYears": evaluation.work_years,
            "education": evaluation.education_level,
            "age": evaluation.candidate_age,
            "sex": evaluation.candidate_gender,
            "school": evaluation.school,
            "resume_content": evaluation.resume_content,
            "original_filename": evaluation.original_filename,
            "created_at": evaluation.created_at.isoformat(),
            "updated_at": evaluation.updated_at.isoformat()
        }

        return result

    async def delete_evaluation(
        self,
        evaluation_id: UUID,
        user_id: UUID
    ) -> bool:
        """删除当前用户的评价，并先清理远程面试方案关联。"""
        try:
            # 复用联合 id/user_id 查询，未命中既可能是不存在，也可能是不属于当前用户。
            evaluation = await self.get_evaluation_by_id(
                evaluation_id=evaluation_id,
                user_id=user_id
            )

            if not evaluation:
                return False

            # 面试方案在远程服务、评价记录在本地数据库，无法放入同一原子事务。
            # 先删远程关联，成功后才删除本地评价，避免评价消失后留下无法定位的关联方案。
            try:
                result_data = await remote_service_client.delete(
                    endpoint=f"/interview-plans/delete-by-evaluation/{evaluation_id}",
                    user_id=user_id
                )
                logger.info(f"成功删除与评价 {evaluation_id} 关联的面试方案")
            except Exception as e:
                # 远程清理失败会直接中止；本地评价仍保留，调用方可安全重试整条删除链路。
                # raise 之后的日志语句不会执行，真正的异常日志由外层 except 记录。
                raise e
                logger.exception(f"删除关联面试方案失败: {e}")

            # 远程关联已清理后，再提交本地删除。
            await self.db.delete(evaluation)
            await self.db.commit()

            return True

        except Exception as e:
            logger.exception(f"删除评价记录失败: {e}")
            # 这里只能回滚尚未提交的本地删除；已经成功的远程面试方案删除无法撤销。
            await self.db.rollback()
            raise

    async def update_evaluation_status(
        self,
        evaluation_id: UUID,
        user_id: UUID,
        new_status: ResumeStatus
    ) -> Optional[Dict[str, Any]]:
        """在用户归属校验后更新评价状态，并提交单条本地事务。"""
        try:
            evaluation = await self.get_evaluation_by_id(
                evaluation_id=evaluation_id,
                user_id=user_id
            )

            if not evaluation:
                return None

            # new_status 已由 ResumeStatus 枚举限制合法取值；修改 ORM 后 commit 才会持久化。
            evaluation.status = new_status
            await self.db.commit()

            return {
                "message": "状态更新成功",
                "evaluation_id": str(evaluation.id),
                "status": new_status.value
            }

        except Exception as e:
            logger.error(f"更新简历状态失败: {e}")
            await self.db.rollback()
            raise


    def _build_evaluation_prompt(
        self,
        evaluation_model: str,
        jd_info: JobDescriptionResponse,
        resume_text: str,
        candidate_name: Optional[str] = None
    ) -> str:
        """把评分标准、JD 字段、候选人信息和简历正文组装为评价提示词。

        方法只做字符串拼接，不截断简历、不调用模型，也不验证最终 JSON；调用方负责控制
        输入长度并把生成结果交给结构化解析器。候选人姓名仅在提供时追加。
        """
        base_prompt = f"""
        {evaluation_model}

        职位信息：
        - 职位名称: {jd_info.title}
        - 部门: {jd_info.department or "未知部门"}
        - 职位要求: {jd_info.requirements}
        - 技能要求: {jd_info.skills}
        - 教育要求: {jd_info.education}
        - 工作经验要求: {jd_info.experience_level or "不限"}

        简历内容：
        """

        if candidate_name:
            base_prompt += f"候选人：{candidate_name}\n"

        base_prompt += f"简历内容：\n{resume_text}\n\n请严格按照JSON格式返回评价结果。"

        return base_prompt
