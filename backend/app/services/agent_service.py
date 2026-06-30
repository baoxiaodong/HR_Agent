"""
轻量 HR Agent 服务
    第一版聚焦工具编排：理解用户 HR 需求，并按意图调用 JD 生成、简历筛选等工具。
"""
import json
import logging
import hashlib
import os
import re
import asyncio
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.conversation import Conversation, Message, MessageRole
from app.models.document import Document
from app.models.resume_evaluation import ResumeEvaluation, ResumeStatus
from app.schemas.agent import AgentArtifact, AgentChatResponse, AgentStep
from app.schemas.interview_plan import InterviewPlanCreate
from app.schemas.job_description import JobDescriptionCreate, JobDescriptionUpdate
from app.schemas.scoring_criteria import ScoringCriteriaCreate, ScoringCriteriaUpdate
from app.services.email_service import EmailSendService
from app.services.dify_service import DifyService
from app.services.agent_skills import AgentSkillBundle, AgentSkillDispatcher, build_default_skill_dispatcher
from app.services.exam_service import ExamService
from app.services.interview_plan_service import InterviewPlanService
from app.services.intent_service import IntentService
from app.services.job_description_service import JobDescriptionService
from app.services.kb_selection_service import KBSelectionService
from app.services.llm_service import LLMService
from app.services.resume_evaluation_service import ResumeEvaluationService
from app.services.scoring_criteria_service import ScoringCriteriaService
from app.utils.text_utils import extract_text_content

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentToolSpec:
    """Agent 可规划调用的工具声明"""

    name: str
    intent: str
    route: Optional[str]
    description: str
    prerequisites: List[str]


@dataclass(frozen=True)
class ReActDecision:
    """ReAct 单轮决策结果。

        thought 是给产品侧展示的简短推理摘要，不承载模型的完整隐藏推理过程。
    """

    mode: str
    intent: str
    action: str
    thought: str
    observation: str
    confidence: Optional[float] = None
    reply: Optional[str] = None
    source: str = "react"


class AgentService:
    """HR Agent 编排服务"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.dify_service = DifyService()
        self.intent_service = IntentService(db)
        self.skill_dispatcher = self._build_skill_dispatcher()
        self.tool_registry = self._build_tool_registry()
        self.email_send_service = EmailSendService(db)
        self.llm_service = None

    def _build_tool_registry(self) -> Dict[str, AgentToolSpec]:
        """声明 Agent 能自主规划的工具和前置条件"""
        tools = [
            AgentToolSpec(
                name="generate_jd",
                intent="jd",
                route="/recruitment/jd-generator",
                description="生成岗位 JD，并自动生成简历评分标准",
                prerequisites=["确认岗位名称、地点、薪资、经验、学历"],
            ),
            AgentToolSpec(
                name="edit_jd",
                intent="jd_edit",
                route="/recruitment/jd-generator",
                description="修改最近生成或保存的岗位 JD",
                prerequisites=["定位要修改的 JD", "明确修改要求"],
            ),
            AgentToolSpec(
                name="edit_scoring_criteria",
                intent="criteria_edit",
                route="/recruitment/resume-screening",
                description="修改最近生成或保存的简历评分标准",
                prerequisites=["定位要修改的评分标准", "明确修改要求"],
            ),
            AgentToolSpec(
                name="evaluate_resume",
                intent="resume_screening",
                route="/recruitment/resume-screening",
                description="基于 JD 批量评分简历",
                prerequisites=["上传 PDF/DOC/DOCX 简历", "选择用于匹配的 JD"],
            ),
            AgentToolSpec(
                name="generate_interview_plan",
                intent="interview_plan",
                route="/recruitment/smart-interview",
                description="基于已评分简历和 JD 生成面试计划",
                prerequisites=["选择一位已评分候选人"],
            ),
            AgentToolSpec(
                name="generate_exam",
                intent="exam_generate",
                route="/training/exam-generator",
                description="基于上传文档生成考试试卷",
                prerequisites=["上传参考文档", "确认试卷配置"],
            ),
            AgentToolSpec(
                name="delete_resource",
                intent="resource_delete",
                route=None,
                description="按用户描述删除已生成的 JD、简历评分记录、面试方案或试卷",
                prerequisites=["明确要删除的资源类型和名称/候选人/标题"],
            ),
        ]
        registry = {tool.intent: tool for tool in tools}
        for bundle in self.skill_dispatcher.bundles.values():
            registry[bundle.intent] = AgentToolSpec(
                name=bundle.metadata.name,
                intent=bundle.intent,
                route=bundle.metadata.route,
                description=bundle.metadata.description or f"执行 {bundle.bundle_name} skill",
                prerequisites=list(bundle.metadata.prerequisites),
            )
        return registry

    def _build_skill_dispatcher(self) -> AgentSkillDispatcher:
        """构建运行时 skill dispatcher。"""
        return build_default_skill_dispatcher()

    def _classify_agent_intent(self, message: str, attachments: List[Dict[str, Any]]) -> str:
        """轻量意图分类：优先规则和附件上下文，不为路由单独调用大模型。"""
        lowered = message.lower()
        if re.search(r"删除|删掉|移除|清理", message) and re.search(r"jd|职位|岗位|简历|候选人|面试|试卷|考试", lowered):
            return "resource_delete"
        if self._is_criteria_edit_request(message):
            return "criteria_edit"
        if self._is_jd_edit_request(message):
            return "jd_edit"

        fast_intent = self.intent_service.classify_intent_fast(message)
        if fast_intent in self.tool_registry:
            return fast_intent

        if self._filter_attachments(attachments, {"pdf", "doc", "docx"}) and re.search(r"评分|筛选|匹配|简历|候选人", message):
            return "resume_screening"
        if self._filter_attachments(attachments, {"pdf", "doc", "docx", "txt", "md"}) and re.search(r"试卷|考试|出题|题目|笔试", message):
            return "exam_generate"
        if self._is_followup_resume_screening_loop_request(message) or self._is_followup_interview_exam_request(message):
            return "interview_plan" if self._is_followup_resume_screening_loop_request(message) else "exam_generate"
        if re.search(r"试卷|考试|出题|题目|笔试", message):
            return "exam_generate"
        if re.search(r"招聘|岗位|职位|jd|job", lowered):
            return "jd"
        if re.search(r"面试|候选人|邀约|通知|邮件", message):
            return "email_notification" if re.search(r"邮件|通知|邀约|邀请", message) else "interview_plan"
        return "general"

    def _build_rule_agent_plan(
        self,
        message: str,
        attachments: List[Dict[str, Any]],
        memory_context: str = "",
    ) -> Optional[Dict[str, Any]]:
        if self._classify_agent_intent(message, attachments) == "resource_delete":
            return {
                "mode": "tool",
                "intent": "resource_delete",
                "reason": "用户明确要求删除招聘相关产物，调用删除资源工具。",
                "reply": None,
                "source": "delete_rule",
            }
        if self._is_criteria_edit_request(message) or self._is_criteria_edit_followup(message, memory_context):
            return {
                "mode": "tool",
                "intent": "criteria_edit",
                "reason": "用户要求修改评分标准，先定位最近评分标准并确认修改要求。",
                "reply": None,
                "source": "criteria_edit_rule",
            }
        if self._is_jd_edit_request(message) or self._is_jd_edit_followup(message, memory_context):
            return {
                "mode": "tool",
                "intent": "jd_edit",
                "reason": "用户要求修改已有 JD，先定位最近 JD 并确认修改要求。",
                "reply": None,
                "source": "jd_edit_rule",
            }
        rule_intent = self._classify_agent_intent(message, attachments)
        if rule_intent in {"jd", "resume_screening", "interview_plan", "exam_generate", "email_notification"}:
            return {
                "mode": "tool",
                "intent": rule_intent,
                "reason": "本地规则已明确识别到 HR 工具任务，优先进入对应业务链路。",
                "reply": None,
                "source": "rule_intent",
            }
        return None

    def _allowed_intents(self) -> List[str]:
        return sorted({*self.tool_registry.keys(), "general"})

    def _route_for_intent(self, intent: str, message: str) -> Dict[str, Any]:
        tool = self.tool_registry.get(intent)
        return {
            "intent": intent,
            "route": tool.route if tool else None,
            "query": message,
            "kb_id": None,
        }

    def _normalize_attachments(self, attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized = []
        for item in attachments:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            normalized.append({
                "name": name,
                "size": item.get("size"),
                "content_type": item.get("content_type"),
                "extension": name.rsplit(".", 1)[-1].lower() if "." in name else "",
            })
        return normalized

    def _filter_attachments(self, attachments: List[Dict[str, Any]], extensions: set[str]) -> List[Dict[str, Any]]:
        return [item for item in attachments if item.get("extension") in extensions]

    def _planning_step(self, tool: Optional[AgentToolSpec], detail: str) -> AgentStep:
        if not tool:
            return AgentStep(id="plan", title="判断处理方式", status="completed", detail=detail)
        prerequisites = "；".join(tool.prerequisites)
        return AgentStep(
            id="plan",
            title="选择执行工具",
            status="completed",
            detail=f"选择工具：{tool.description}。前置条件：{prerequisites}。{detail}",
            tool=tool.name,
        )

    def _decision_to_plan(self, decision: ReActDecision) -> Dict[str, Any]:
        return {
            "mode": decision.mode,
            "intent": decision.intent,
            "action": decision.action,
            "confidence": decision.confidence,
            "reason": decision.thought,
            "observation": decision.observation,
            "reply": decision.reply,
            "source": decision.source,
        }

    async def _plan_agent_action(
        self,
        message: str,
        attachments: List[Dict[str, Any]],
        memory_context: str = "",
    ) -> Dict[str, Any]:
        """使用受控 ReAct 决策选择下一步动作。

        这里的 ReAct 是有限动作空间版本：模型只能在 chat / use_tool / ask_user
        中选择，并且 use_tool 只能选择 tool_registry 声明过的业务工具。
        """
        attachment_text = "无"
        if attachments:
            attachment_text = "、".join(
                f"{item.get('name')}({item.get('extension') or '未知格式'})"
                for item in attachments
            )
        tool_text = "\n".join(
            f"- {tool.intent}: {tool.name}，{tool.description}；前置条件：{'、'.join(tool.prerequisites)}"
            for tool in self.tool_registry.values()
        )
        allowed_intents = "、".join(self._allowed_intents())
        prompt = (
            "你是 HR Agent 的 ReAct 控制器。你要按 Thought -> Action -> Observation 的方式，"
            "为当前用户消息选择下一步动作。\n"
            "请只输出一个 JSON 对象，不要输出 Markdown，不要解释 JSON 之外的内容。\n\n"
            "可用工具：\n"
            f"{tool_text}\n\n"
            "动作空间：\n"
            "- chat: 普通对话、打招呼、解释能力边界，不调用工具。\n"
            "- use_tool: 用户明确要执行 HR 任务，选择一个工具 intent。\n"
            "- ask_user: 用户目标明确但缺少关键前置条件，选择对应工具 intent，并由业务链路追问。\n\n"
            "决策规则：\n"
            "1. 不确定时优先 chat，避免误触发工具。\n"
            "2. 生成 JD、修改 JD、修改评分标准、简历筛选/评分、面试计划、试卷生成、邮件草稿应选择对应工具。\n"
            "3. 缺少附件、JD、候选人、考试配置等前置条件时，选择 ask_user 或 use_tool，后续工具链会生成表单/选择器。\n"
            "4. 邮件只能生成草稿，不允许自动发送。\n"
            f"5. intent 只能是这些值之一：{allowed_intents}。\n"
            "6. 用户要求删除 JD、简历记录、面试方案、试卷时，选择 resource_delete。\n"
            "7. 用户说“改改上次那个 JD、修改刚才的职位描述、把这个 JD 的薪资改成...”时，选择 jd_edit。\n"
            "8. 用户说“修改评分标准、调整简历评分规则、把技能匹配改成40分”时，选择 criteria_edit。\n"
            "9. thought 只写一句简短推理摘要，不要展开内部推理。\n"
            "10. 用户说“继续、这个、刚才、上一个、按前面”等指代时，优先结合历史对话记忆理解。\n\n"
            "返回 JSON 字段：\n"
            "{\n"
            "  \"thought\": \"一句推理摘要\",\n"
            "  \"action\": \"chat/use_tool/ask_user\",\n"
            "  \"intent\": \"...\",\n"
            "  \"tool\": \"工具名或 null\",\n"
            "  \"action_input\": {},\n"
            "  \"observation\": \"预期观察或已知前置条件\",\n"
            "  \"confidence\": 0.0,\n"
            "  \"reply\": \"action=chat 时的自然回复，否则为 null\"\n"
            "}\n\n"
            f"{self._format_memory_for_prompt(memory_context)}"
            f"用户消息：{message}\n"
            f"附件：{attachment_text}"
        )
        try:
            if self.llm_service is None:
                self.llm_service = LLMService()
            response = await self.llm_service.generate_response(prompt)
            planned = self._safe_json_loads(response)
            action = str(planned.get("action") or planned.get("mode") or "").lower()
            intent = str(planned.get("intent") or "").strip()
            if action == "use_tool":
                mode = "tool"
            elif action == "ask_user":
                mode = "tool"
            else:
                mode = "chat"
                action = "chat"
            if mode in {"chat", "tool"} and intent in self._allowed_intents():
                decision = ReActDecision(
                    mode=mode,
                    intent=intent,
                    action=action,
                    thought=self._clean_optional_value(planned.get("thought"))
                    or self._clean_optional_value(planned.get("reason"))
                    or "已完成 ReAct 决策。",
                    observation=self._clean_optional_value(planned.get("observation"))
                    or "等待执行动作后观察结果。",
                    confidence=planned.get("confidence"),
                    reply=planned.get("reply"),
                    source="react_llm",
                )
                return self._decision_to_plan(decision)
        except Exception as exc:
            logger.warning("Agent ReAct 决策失败，使用规则兜底: %s", exc)

        fallback_intent = self._classify_agent_intent(message, attachments)
        fallback_action = "use_tool" if fallback_intent in self.tool_registry else "chat"
        decision = ReActDecision(
            mode="tool" if fallback_intent in self.tool_registry else "chat",
            intent=fallback_intent,
            action=fallback_action,
            thought="ReAct 决策失败后，使用本地关键词和附件规则选择下一步。",
            observation="本地规则已给出可执行意图。" if fallback_action == "use_tool" else "未匹配到需要调用的工具。",
            source="fallback",
        )
        return self._decision_to_plan(decision)

    async def chat(
        self,
        message: str,
        user_id: UUID,
        conversation_id: Optional[str] = None,
        auto_execute: bool = True,
        confirmed_requirements: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        agent_plan: Optional[Dict[str, Any]] = None,
    ) -> AgentChatResponse:
        """处理用户消息并执行招聘 Agent 任务"""
        normalized_attachments = self._normalize_attachments(attachments or [])
        memory_context = await self._build_conversation_memory(conversation_id, user_id, message)
        if confirmed_requirements:
            confirmation_action = str(confirmed_requirements.get("action") or "").strip()
            confirmed_skill = self.skill_dispatcher.match_confirmation_action(confirmation_action) if confirmation_action else None
            if confirmed_skill:
                intent = confirmed_skill.intent
                agent_plan = {
                    "mode": "tool",
                    "intent": confirmed_skill.intent,
                    "reason": f"用户已确认 {confirmed_skill.bundle_name} 所需信息，继续执行 skill。",
                    "reply": None,
                    "source": "confirmed_skill_action",
                }
            else:
                intent = "jd"
                agent_plan = {
                    "mode": "tool",
                    "intent": "jd",
                    "reason": "用户已确认 JD 生成信息，继续执行 JD 工具链。",
                    "reply": None,
                    "source": "confirmed_requirements",
                }
        else:
            agent_plan = agent_plan or self._build_rule_agent_plan(message, normalized_attachments, memory_context)
            agent_plan = agent_plan or await self._plan_agent_action(message, normalized_attachments, memory_context)
            intent = agent_plan["intent"]
        route_result = self._route_for_intent(intent, message)
        selected_tool = self.tool_registry.get(intent)

        if agent_plan["mode"] == "chat" or intent == "general":
            reply = self._clean_optional_value(agent_plan.get("reply")) or self._fallback_message("general", message)
            return AgentChatResponse(
                message=reply,
                intent="general",
                route=None,
                steps=[
                    AgentStep(
                        id="understand",
                        title="完成需求判断",
                        status="completed",
                        detail=agent_plan.get("reason") or "判断为普通对话，不调用招聘工具。",
                    )
                ],
                suggestions=["生成 JD", "评分简历", "基于文档生成试卷"],
            )

        if intent == "resource_delete":
            return await self._handle_resource_delete(message, user_id, selected_tool, conversation_id)

        if intent == "jd_edit":
            return await self._handle_jd_edit(message, user_id, selected_tool, conversation_id, memory_context)

        if intent == "criteria_edit":
            return await self._handle_criteria_edit(message, user_id, selected_tool, conversation_id, memory_context)

        if intent in self.skill_dispatcher.bundles:
            return await self._handle_skill_intent(
                intent=intent,
                message=message,
                user_id=user_id,
                route=route_result.get("route"),
                selected_tool=selected_tool,
                memory_context=memory_context,
                confirmed_requirements=confirmed_requirements,
            )

        if intent == "resume_screening":
            has_resume_files = bool(self._filter_attachments(normalized_attachments, {"pdf", "doc", "docx"}))
            if has_resume_files:
                resume_count = len(self._filter_attachments(normalized_attachments, {"pdf", "doc", "docx"}))
                return AgentChatResponse(
                    message=f"我已经识别到 {resume_count} 份简历附件。下一步请选择用于匹配的 JD，确认后我会调用简历评分工具逐份评分并保存结果。",
                    intent=intent,
                    route=route_result.get("route"),
                    steps=[
                        self._planning_step(selected_tool, "已选择简历评分工具，并检查到简历附件。"),
                        AgentStep(id="upload", title="接收简历文件", status="completed", detail=f"已检测到 {resume_count} 份简历附件"),
                        AgentStep(id="select_jd", title="选择匹配 JD", status="running", detail="请选择用于评分匹配的 JD。"),
                        AgentStep(id="evaluate", title="执行 AI 评分", status="pending", detail="选择 JD 后开始逐份评分。", tool="evaluate_resume"),
                    ],
                    artifacts=[
                        AgentArtifact(
                            type="resume_upload_request",
                            title="选择 JD 并开始评分",
                            content={"requires_job_description": True, "file_count": resume_count},
                        )
                    ],
                    suggestions=[],
                )
            return AgentChatResponse(
                message="我规划了一下：这个需求需要调用简历评分工具，但当前消息里还没有简历附件。请先在底部消息框点击“上传文件”，添加 PDF、DOC 或 DOCX 简历后再发送；收到简历后我再让你选择用于匹配的 JD。",
                intent=intent,
                route=route_result.get("route"),
                steps=[
                    self._planning_step(selected_tool, "需要先满足前置条件：上传简历文件。"),
                    AgentStep(id="upload", title="等待上传简历", status="pending", detail="请先上传 PDF、DOC 或 DOCX 简历。"),
                    AgentStep(id="select_jd", title="选择匹配 JD", status="pending", detail="收到简历后再选择用于匹配的 JD。"),
                    AgentStep(id="evaluate", title="执行 AI 评分", status="pending", detail="选择 JD 后开始逐份评分。", tool="evaluate_resume"),
                ],
                artifacts=[
                    AgentArtifact(
                        type="resume_upload_request",
                        title="简历筛选工具",
                        content={"requires_job_description": True, "accepted_formats": ["pdf", "doc", "docx"]},
                    )
                ],
                suggestions=[],
            )

        if intent == "interview_plan":
            followup_resume_action = await self._resolve_resume_screening_followup_action(message, user_id, conversation_id)
            if followup_resume_action.get("action") in {"generate_interview", "confirm_delete_low_scores"}:
                return self._build_resume_screening_followup_response(
                    followup_resume_action,
                    route_result.get("route"),
                    selected_tool,
                )
            candidate_resolution = await self._resolve_interview_candidate(message, user_id, memory_context)
            if candidate_resolution.get("status") == "matched" and candidate_resolution.get("candidate_id"):
                candidate_name = candidate_resolution.get("candidate_name") or "该候选人"
                return AgentChatResponse(
                    message=candidate_resolution.get("reply") or f"我已经找到候选人「{candidate_name}」，现在直接调用面试计划工具生成方案。",
                    intent=intent,
                    route=route_result.get("route"),
                    steps=[
                        self._planning_step(selected_tool, "用户已指定候选人，且已匹配到已评分简历。"),
                        AgentStep(id="select_resume", title="匹配候选人", status="completed", detail=f"已匹配：{candidate_name}。"),
                        AgentStep(id="generate_plan", title="生成面试计划", status="running", detail="正在调用面试计划工具。", tool="generate_interview_plan"),
                        AgentStep(id="save_plan", title="保存面试计划", status="pending", detail="生成后自动保存。"),
                    ],
                    artifacts=[
                        AgentArtifact(
                            type="interview_plan_execute",
                            title="直接生成面试计划",
                            content={
                                "resume_evaluation_id": candidate_resolution["candidate_id"],
                                "candidate_name": candidate_name,
                            },
                        )
                    ],
                    suggestions=["生成面试计划", "查看该候选人评分", "重新筛选简历"],
                )
            if candidate_resolution.get("status") == "no_match":
                requested_name = candidate_resolution.get("requested_name") or "该候选人"
                return AgentChatResponse(
                    message=candidate_resolution.get("reply") or f"我没有在已评分候选人中找到「{requested_name}」。请先上传他的简历并完成评分，然后我再基于评分结果生成面试计划。",
                    intent=intent,
                    route=route_result.get("route"),
                    steps=[
                        self._planning_step(selected_tool, "用户指定了候选人，但当前没有匹配的已评分简历。"),
                        AgentStep(id="match_resume", title="匹配候选人", status="failed", detail=f"未找到：{requested_name}。"),
                        AgentStep(id="upload_resume", title="上传候选人简历", status="running", detail="请上传该候选人的 PDF、DOC 或 DOCX 简历。"),
                        AgentStep(id="evaluate", title="完成简历评分", status="pending", detail="评分完成后再生成面试计划。", tool="evaluate_resume"),
                    ],
                    artifacts=[
                        AgentArtifact(
                            type="resume_upload_request",
                            title="上传候选人简历",
                            content={"candidate_name": requested_name, "accepted_formats": ["pdf", "doc", "docx"]},
                        )
                    ],
                    suggestions=[],
                )
            if candidate_resolution.get("status") == "ambiguous":
                return AgentChatResponse(
                    message=candidate_resolution.get("reply") or "我找到了多个可能匹配的候选人，还不能确定你指的是哪一位。请在这条消息里选择候选人后，我再生成面试计划。",
                    intent=intent,
                    route=route_result.get("route"),
                    steps=[
                        self._planning_step(selected_tool, "用户指定的候选人存在多个可能匹配项，需要用户确认。"),
                        AgentStep(id="select_resume", title="确认候选人", status="running", detail="请选择一条已评分简历。"),
                        AgentStep(id="generate_plan", title="生成面试计划", status="pending", detail="选择后开始生成。", tool="generate_interview_plan"),
                    ],
                    artifacts=[
                        AgentArtifact(
                            type="interview_plan_request",
                            title="选择候选人生成面试计划",
                            content={
                                "requires_resume_evaluation": True,
                                "candidates": candidate_resolution.get("candidates") or [],
                            },
                        )
                    ],
                    suggestions=[],
                )
            return AgentChatResponse(
                message="我规划了一下：生成面试计划需要先选中一位已评分候选人，然后调用面试计划工具生成并保存方案。请在这条消息里选择候选人。",
                intent=intent,
                route=route_result.get("route"),
                steps=[
                    self._planning_step(selected_tool, "需要先满足前置条件：选择已评分候选人。"),
                    AgentStep(id="select_resume", title="选择候选人", status="pending", detail="请选择一条已评分简历。"),
                    AgentStep(id="generate_plan", title="生成面试计划", status="pending", detail="选择后开始生成。", tool="generate_interview_plan"),
                    AgentStep(id="save_plan", title="保存面试计划", status="pending", detail="生成后自动保存。"),
                ],
                artifacts=[
                    AgentArtifact(
                        type="interview_plan_request",
                        title="选择候选人生成面试计划",
                        content={"requires_resume_evaluation": True},
                    )
                ],
                suggestions=[],
            )

        if intent == "exam_generate":
            interview_exam_context = await self._resolve_interview_exam_followup(message, user_id, conversation_id)
            if interview_exam_context.get("matched"):
                parsed_exam = await self._parse_exam_requirements(
                    interview_exam_context.get("message") or message,
                    conversation_id,
                    memory_context,
                )
                return AgentChatResponse(
                    message=(
                        f"可以。我会基于最近的面试方案「{interview_exam_context.get('title')}」来组织笔试范围。"
                        "不过当前消息里还没有参考文档，请先在底部消息框上传岗位资料、题库材料或技术文档，上传后我再让你确认试卷配置。"
                    ),
                    intent=intent,
                    route=route_result.get("route"),
                    steps=[
                        self._planning_step(selected_tool, "识别到用户想从当前面试方案继续生成笔试试卷。"),
                        AgentStep(
                            id="load_interview_plan",
                            title="读取面试方案上下文",
                            status="completed",
                            detail=f"已匹配：{interview_exam_context.get('title')}",
                        ),
                        AgentStep(id="upload_docs", title="等待上传参考文档", status="running", detail="请上传用于出题的参考文档。"),
                        AgentStep(id="confirm_exam", title="确认考试配置", status="pending", detail="上传文档后确认题型、题量和分值。"),
                        AgentStep(id="generate_exam", title="基于文档生成试卷", status="pending", tool="generate_exam"),
                    ],
                    artifacts=[
                        AgentArtifact(
                            type="exam_document_upload_request",
                            title="上传出题参考文档",
                            content={
                                **parsed_exam,
                                "requires_document_upload": True,
                                "interview_plan_context": interview_exam_context,
                            },
                        )
                    ],
                    suggestions=[],
                    requires_confirmation=False,
                    missing_fields=[field for field in ["title", "subject"] if not parsed_exam.get(field)],
                )
            parsed_exam = await self._parse_exam_requirements(message, conversation_id, memory_context)
            has_exam_docs = bool(self._filter_attachments(normalized_attachments, {"pdf", "doc", "docx", "txt", "md"}))
            if has_exam_docs:
                document_count = len(self._filter_attachments(normalized_attachments, {"pdf", "doc", "docx", "txt", "md"}))
                return AgentChatResponse(
                    message=f"我已经识别到 {document_count} 个参考文档附件。下一步先确认试卷标题、考察方向、题型、题量和分值，再调用文档出题工具生成试卷。",
                    intent=intent,
                    route=route_result.get("route"),
                    steps=[
                        self._planning_step(selected_tool, "已选择基于文档生成试卷工具，并检查到参考文档。"),
                        AgentStep(id="upload_docs", title="接收参考文档", status="completed", detail=f"已检测到 {document_count} 个参考文档"),
                        AgentStep(id="confirm_exam", title="确认考试配置", status="running", detail="请确认试卷标题、题型、题量和分值。"),
                        AgentStep(id="generate_exam", title="基于文档生成试卷", status="pending", tool="generate_exam"),
                        AgentStep(id="save_exam", title="保存试卷", status="pending", detail="生成后保存到考试管理。"),
                    ],
                    artifacts=[
                        AgentArtifact(
                            type="exam_generate_request",
                            title="确认考试配置",
                            content={**parsed_exam, "document_count": document_count},
                        )
                    ],
                    suggestions=[],
                    missing_fields=[field for field in ["title", "subject"] if not parsed_exam.get(field)],
                )
            matched_knowledge_files = await self._select_exam_knowledge_files(parsed_exam, message, user_id)
            if matched_knowledge_files:
                matched_names = "、".join(file.get("fileName") or "知识库文档" for file in matched_knowledge_files)
                return AgentChatResponse(
                    message=f"我已经按你的描述提取了试卷配置，并在知识库中匹配到参考文档：{matched_names}。请确认配置后即可生成试卷。",
                    intent=intent,
                    route=route_result.get("route"),
                    steps=[
                        self._planning_step(selected_tool, "已选择基于文档生成试卷工具，并匹配到知识库参考文档。"),
                        AgentStep(id="select_docs", title="匹配参考文档", status="completed", detail=f"已匹配：{matched_names}"),
                        AgentStep(id="confirm_exam", title="确认考试配置", status="running", detail="请确认试卷标题、题型、题量和分值。"),
                        AgentStep(id="generate_exam", title="基于文档生成试卷", status="pending", tool="generate_exam"),
                        AgentStep(id="save_exam", title="保存试卷", status="pending", detail="生成后保存到考试管理。"),
                    ],
                    artifacts=[
                        AgentArtifact(
                            type="exam_generate_request",
                            title="确认考试配置",
                            content={**parsed_exam, "knowledge_files": matched_knowledge_files},
                        )
                    ],
                    suggestions=[],
                    missing_fields=[field for field in ["title", "subject"] if not parsed_exam.get(field)],
                )
            return AgentChatResponse(
                message="我规划了一下：基于文档出题需要先拿到参考文档，但当前消息里还没有可用附件。请先在底部消息框点击“上传文件”添加附件，然后再发送生成试卷的需求。",
                intent=intent,
                route=route_result.get("route"),
                steps=[
                    self._planning_step(selected_tool, "需要先满足前置条件：上传出题参考文档。"),
                    AgentStep(id="upload_docs", title="等待上传参考文档", status="pending", detail="请上传 PDF、DOC、DOCX、TXT 或 MD 文档。"),
                    AgentStep(id="confirm_exam", title="确认考试配置", status="pending", detail="上传文档后再配置试卷。"),
                    AgentStep(id="generate_exam", title="基于文档生成试卷", status="pending", tool="generate_exam"),
                    AgentStep(id="save_exam", title="保存试卷", status="pending", detail="生成后保存到考试管理。"),
                ],
                artifacts=[
                    AgentArtifact(
                        type="exam_document_upload_request",
                        title="上传出题参考文档",
                        content={**parsed_exam, "requires_document_upload": True},
                    )
                ],
                suggestions=[],
                requires_confirmation=False,
                missing_fields=[field for field in ["title", "subject"] if not parsed_exam.get(field)],
            )

        if intent != "jd":
            return AgentChatResponse(
                message=self._fallback_message(intent, message),
                intent=intent,
                route=route_result.get("route"),
                steps=[
                    AgentStep(
                        id="plan",
                        title="理解需求并规划",
                        status="completed",
                        detail="当前没有匹配到可安全自动执行的招聘工具，我先给出说明或建议。",
                    )
                ],
                suggestions=self._suggestions_for_intent(intent),
            )

        if not auto_execute:
            return AgentChatResponse(
                message="我识别到这是 JD 生成任务。确认信息后我会调用 JD 生成工具并保存到 JD 管理。",
                intent=intent,
                route=route_result.get("route"),
                steps=[
                    self._planning_step(selected_tool, "JD 生成需要先确认结构化招聘信息。"),
                    AgentStep(id="confirm", title="等待确认", status="pending", detail="确认后调用 JD 生成工具。"),
                ],
                suggestions=[],
                requires_confirmation=True,
            )

        if confirmed_requirements:
            parsed = self._normalize_requirements(confirmed_requirements, message)
            return await self._run_recruitment_agent(
                message,
                conversation_id,
                route_result.get("route"),
                parsed_requirements=parsed,
                user_id=user_id,
            )

        parsed = await self._parse_requirements(message, conversation_id, memory_context)
        missing_fields = self._missing_required_fields(parsed)
        if missing_fields:
            return AgentChatResponse(
                message="我先补齐 JD 生成的必要信息，再生成会更稳。请在弹窗里确认或补充这些字段。",
                intent=intent,
                route=route_result.get("route"),
                steps=[
                    self._planning_step(selected_tool, "已选择 JD 生成工具；先补齐必要字段再执行。"),
                    AgentStep(id="parse", title="解析招聘需求", status="completed", detail=self._brief_requirements(parsed), tool="parse_requirements"),
                    AgentStep(id="confirm", title="确认招聘信息", status="pending", detail=f"待补充：{'、'.join(self._field_label(field) for field in missing_fields)}"),
                    AgentStep(id="jd", title="生成岗位 JD", status="pending", tool="generate_jd"),
                ],
                artifacts=[AgentArtifact(type="requirements", title="待确认招聘需求", content=parsed)],
                suggestions=[],
                requires_confirmation=True,
                missing_fields=missing_fields,
            )

        return AgentChatResponse(
            message="我已提取到 JD 生成所需信息。请在弹窗中确认，确认后我再调用 JD 生成工具。",
            intent=intent,
            route=route_result.get("route"),
            steps=[
                self._planning_step(selected_tool, "已选择 JD 生成工具；等待用户确认后执行。"),
                AgentStep(id="parse", title="解析招聘需求", status="completed", detail=self._brief_requirements(parsed), tool="parse_requirements"),
                AgentStep(id="confirm", title="确认招聘信息", status="pending", detail="等待用户确认后执行生成。"),
                AgentStep(id="jd", title="生成岗位 JD", status="pending", tool="generate_jd"),
            ],
            artifacts=[AgentArtifact(type="requirements", title="待确认招聘需求", content=parsed)],
            suggestions=[],
            requires_confirmation=True,
        )

    async def _run_recruitment_agent(
        self,
        message: str,
        conversation_id: Optional[str],
        route: Optional[str],
        parsed_requirements: Optional[Dict[str, Any]] = None,
        user_id: Optional[UUID] = None,
    ) -> AgentChatResponse:
        steps: List[AgentStep] = [
            self._planning_step(self.tool_registry.get("jd"), "招聘信息已确认，开始执行生成链路。"),
            AgentStep(id="parse", title="解析招聘需求", status="running", tool="parse_requirements"),
            AgentStep(id="jd", title="生成岗位 JD", status="pending", tool="generate_jd"),
            AgentStep(id="criteria", title="生成简历评分标准", status="pending", tool="generate_scoring_criteria"),
            AgentStep(id="next", title="规划下一步", status="pending"),
        ]
        artifacts: List[AgentArtifact] = []

        parsed = parsed_requirements or await self._parse_requirements(message, conversation_id)
        steps[1] = steps[1].model_copy(update={"status": "completed", "detail": self._brief_requirements(parsed)})
        artifacts.append(AgentArtifact(type="requirements", title="结构化招聘需求", content=parsed))

        steps[2] = steps[2].model_copy(update={"status": "running"})
        jd_content = await self._generate_jd(message, parsed, conversation_id)
        saved_jd = None
        if user_id:
            saved_jd = await self._save_job_description(jd_content, parsed, message, user_id, conversation_id)
        jd_detail = "已生成可编辑岗位 JD。"
        if saved_jd:
            jd_detail = f"已生成并保存到 JD 列表，ID：{saved_jd.id}"
        steps[2] = steps[2].model_copy(update={"status": "completed", "detail": jd_detail})
        artifacts.append(AgentArtifact(
            type="job_description",
            title=f"{parsed.get('job_title') or '岗位'} JD",
            content=jd_content,
            metadata={
                "job_title": parsed.get("job_title"),
                "route": "/recruitment/jd-generator",
                "saved_jd_id": str(saved_jd.id) if saved_jd else None,
            },
        ))

        saved_criteria = None
        steps[3] = steps[3].model_copy(update={"status": "running", "detail": "正在基于 JD 生成评分标准..."})
        criteria_content = await self._generate_scoring_criteria(jd_content, parsed, conversation_id)
        if user_id:
            saved_criteria = await self._save_scoring_criteria(
                criteria_content,
                parsed,
                user_id,
                conversation_id,
                saved_jd.id if saved_jd else None,
            )
        criteria_detail = "已生成简历评分标准。"
        if saved_criteria:
            criteria_detail = f"已生成并保存评分标准，ID：{saved_criteria.id}"
        steps[3] = steps[3].model_copy(update={"status": "completed", "detail": criteria_detail})
        artifacts.append(AgentArtifact(
            type="scoring_criteria",
            title=f"{parsed.get('job_title') or '岗位'}评分标准",
            content=criteria_content,
            metadata={
                "job_title": parsed.get("job_title"),
                "saved_criteria_id": str(saved_criteria.id) if saved_criteria else None,
                "saved_jd_id": str(saved_jd.id) if saved_jd else None,
            },
        ))

        steps[4] = steps[4].model_copy(update={"status": "completed", "detail": "如需筛选简历，可以继续告诉我“筛选简历”。"})

        job_title = parsed.get("job_title") or "这个岗位"
        return AgentChatResponse(
            message=f"我已经按「{job_title}」生成并保存了 JD，同时生成了简历评分标准。你可以继续让我筛选简历、生成面试题或修改 JD。",
            intent="jd",
            route=route,
            steps=steps,
            artifacts=artifacts,
            suggestions=[
                "筛选这个 JD 的简历",
                "基于 JD 生成面试题",
                "继续修改 JD",
            ],
        )

    async def stream_chat_agent(
        self,
        message: str,
        user_id: UUID,
        conversation_id: Optional[str] = None,
        auto_execute: bool = True,
        confirmed_requirements: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """流式处理普通 Agent 聊天：先推送思考状态，再返回最终规划/回复。"""
        thinking_response = AgentChatResponse(
            message="我正在理解你的需求。",
            intent="thinking",
            route=None,
            steps=[
                AgentStep(
                    id="understand",
                    title="理解用户需求",
                    status="running",
                    detail="正在判断是否需要调用 HR 工具。",
                ),
                AgentStep(
                    id="plan",
                    title="选择下一步动作",
                    status="pending",
                    detail="识别完成后选择 chat、ask_user 或 use_tool。",
                ),
            ],
            suggestions=[],
        )
        yield {"type": "thinking", "response": thinking_response.model_dump()}

        normalized_attachments = self._normalize_attachments(attachments or [])
        memory_context = await self._build_conversation_memory(conversation_id, user_id, message)
        agent_plan = None
        if not confirmed_requirements:
            agent_plan = self._build_rule_agent_plan(message, normalized_attachments, memory_context)
            if not agent_plan:
                agent_plan = await self._plan_agent_action(message, normalized_attachments, memory_context)
            if agent_plan.get("mode") == "chat" or agent_plan.get("intent") == "general":
                chat_response = AgentChatResponse(
                    message="",
                    intent="general",
                    route=None,
                    steps=[
                        AgentStep(
                            id="understand",
                            title="完成需求判断",
                            status="completed",
                            detail=agent_plan.get("reason") or "判断为普通对话，不调用招聘工具。",
                        )
                    ],
                    suggestions=["生成 JD", "评分简历", "基于文档生成试卷"],
                )
                yield {"type": "plan", "response": chat_response.model_dump()}

                full_text = ""
                try:
                    if self.llm_service is None:
                        self.llm_service = LLMService()
                    chat_prompt = self._build_memory_augmented_prompt(message, memory_context)
                    async for delta in self.llm_service.stream_response(chat_prompt):
                        full_text += delta
                        yield {"type": "delta", "delta": delta}
                except Exception as exc:
                    logger.warning("Agent 普通聊天原生流式失败，使用规划回复兜底: %s", exc)
                    fallback_reply = self._clean_optional_value(agent_plan.get("reply")) or self._fallback_message("general", message)
                    async for delta in self._stream_text(fallback_reply):
                        full_text += delta
                        yield {"type": "delta", "delta": delta}

                final_response = chat_response.model_copy(update={"message": full_text.strip() or self._fallback_message("general", message)})
                yield {"type": "final", "response": final_response.model_dump()}
                return

            if agent_plan.get("intent") == "jd_edit":
                async for event in self._stream_jd_edit_response(
                    message=message,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    memory_context=memory_context,
                ):
                    yield event
                return

            if agent_plan.get("intent") == "criteria_edit":
                async for event in self._stream_criteria_edit_response(
                    message=message,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    memory_context=memory_context,
                ):
                    yield event
                return

            if agent_plan.get("intent") in self.skill_dispatcher.bundles:
                if agent_plan.get("intent") == "email_notification":
                    async for event in self._stream_skill_draft_response(
                        intent="email_notification",
                        message=message,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        route=self._route_for_intent("email_notification", message).get("route"),
                        memory_context=memory_context,
                        confirmed_requirements=confirmed_requirements,
                    ):
                        yield event
                    return
                response = await self.chat(
                    message=message,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    auto_execute=auto_execute,
                    confirmed_requirements=confirmed_requirements,
                    attachments=attachments,
                    agent_plan=agent_plan,
                )
                yield {
                    "type": "plan",
                    "response": response.model_copy(update={"message": ""}).model_dump(),
                }
                async for delta in self._stream_text(response.message):
                    yield {"type": "delta", "delta": delta}
                yield {"type": "final", "response": response.model_dump()}
                return

        response = await self.chat(
            message=message,
            user_id=user_id,
            conversation_id=conversation_id,
            auto_execute=auto_execute,
            confirmed_requirements=confirmed_requirements,
            attachments=attachments,
            agent_plan=agent_plan,
        )
        yield {
            "type": "plan",
            "response": response.model_copy(update={"message": ""}).model_dump(),
        }
        async for delta in self._stream_text(response.message):
            yield {"type": "delta", "delta": delta}
        yield {"type": "final", "response": response.model_dump()}

    async def _stream_jd_edit_response(
        self,
        message: str,
        user_id: UUID,
        conversation_id: Optional[str],
        memory_context: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        selected_tool = self.tool_registry.get("jd_edit")
        edit_request = await self._parse_jd_edit_request(message, memory_context)
        steps = [
            self._planning_step(selected_tool, "识别到 JD 修改请求，先定位目标 JD。"),
            AgentStep(id="locate_jd", title="定位要修改的 JD", status="running", detail="优先查找当前对话最近生成的 JD。", tool="edit_jd"),
            AgentStep(id="collect_changes", title="确认修改要求", status="pending", detail="拿到明确修改点后再改写。"),
            AgentStep(id="rewrite_jd", title="改写 JD 内容", status="pending", detail="基于原 JD 和修改要求生成新版。"),
            AgentStep(id="save_jd", title="保存修改", status="pending", detail="更新原 JD 记录。"),
        ]
        artifacts: List[AgentArtifact] = []
        yield self._jd_edit_stream_event("plan", "正在定位要修改的 JD。", steps, artifacts)

        candidates = await self._resolve_recent_jd_candidates(user_id, conversation_id, edit_request.get("keyword") or "")
        if not candidates:
            steps[1] = steps[1].model_copy(update={"status": "failed", "detail": "没有找到可修改的 JD。"})
            response = AgentChatResponse(
                message="我还没有找到可以修改的 JD。请先生成或保存一个 JD，或者明确告诉我要修改哪个岗位的 JD。",
                intent="jd_edit",
                route="/recruitment/jd-generator",
                steps=steps,
                artifacts=artifacts,
                suggestions=["生成一个新的 JD", "说明要修改的岗位名称", "去 JD 管理查看职位"],
            )
            yield {"type": "final", "response": response.model_dump()}
            return

        if len(candidates) > 1 and edit_request.get("keyword"):
            matches = self._match_delete_resources(candidates, edit_request.get("keyword") or "")
            if matches:
                candidates = matches

        if len(candidates) > 1:
            steps[1] = steps[1].model_copy(update={"status": "running", "detail": f"找到 {len(candidates)} 个可能的 JD，需要确认。"})
            artifacts.append(AgentArtifact(type="jd_edit_candidates", title="待确认 JD", content={"candidates": candidates[:5]}))
            response = AgentChatResponse(
                message=(
                    "我找到了多个可能要修改的 JD。为避免改错，请告诉我具体是哪一个：\n"
                    + "\n".join(f"- {self._format_jd_candidate_label(item)}" for item in candidates[:5])
                ),
                intent="jd_edit",
                route="/recruitment/jd-generator",
                steps=steps,
                artifacts=artifacts,
                suggestions=["修改最新的那个 JD", "说明岗位名称", "去 JD 管理查看职位"],
            )
            yield {"type": "final", "response": response.model_dump()}
            return

        target = candidates[0]
        steps[1] = steps[1].model_copy(update={"status": "completed", "detail": f"已定位：{target.get('name') or '最近的 JD'}。"})
        yield self._jd_edit_stream_event("progress", f"已定位「{target.get('name') or '最近的 JD'}」。", steps, artifacts)

        edit_instructions = self._clean_optional_value(edit_request.get("changes"))
        if not edit_instructions:
            steps[2] = steps[2].model_copy(update={"status": "pending", "detail": "等待用户说明要修改哪些内容。"})
            artifacts.append(AgentArtifact(
                type="jd_edit_request",
                title="等待修改要求",
                content={"job_description_id": target.get("id"), "title": target.get("name")},
            ))
            response = AgentChatResponse(
                message=f"可以，我找到了「{target.get('name') or '最近的 JD'}」。你想具体改哪些内容？比如薪资、地点、职责、技能要求或福利。",
                intent="jd_edit",
                route="/recruitment/jd-generator",
                steps=steps,
                artifacts=artifacts,
                suggestions=["薪资改成 20-30K", "增加 React 和性能优化要求", "把工作地点改成长沙"],
            )
            yield {"type": "final", "response": response.model_dump()}
            return

        steps[2] = steps[2].model_copy(update={"status": "completed", "detail": edit_instructions})
        steps[3] = steps[3].model_copy(update={"status": "running", "detail": "正在读取原 JD 并生成修改版。"})
        yield self._jd_edit_stream_event("progress", "已收到修改要求，正在改写 JD。", steps, artifacts)

        try:
            service = JobDescriptionService(self.db)
            original_jd = await service.get_job_description(str(target["id"]), user_id)
            original_data = original_jd.model_dump(mode="json")
            original_content = original_data.get("content") or ""
            updated_content = await self._rewrite_jd_content(original_content, edit_instructions)
            steps[3] = steps[3].model_copy(update={"status": "completed", "detail": "已生成修改后的 JD。"})
            yield self._jd_edit_stream_event("progress", "JD 修改版已生成，正在保存。", steps, artifacts)

            update_data = self._build_jd_update_payload(original_data, updated_content, edit_instructions, edit_request)
            steps[4] = steps[4].model_copy(update={"status": "running", "detail": "正在保存到原 JD。"})
            yield self._jd_edit_stream_event("progress", "正在保存修改。", steps, artifacts)
            updated_jd = await service.update_job_description(str(target["id"]), update_data, user_id)
            steps[4] = steps[4].model_copy(update={"status": "completed", "detail": f"已更新 JD：{updated_jd.id}"})
        except Exception as exc:
            logger.warning("Agent 流式修改 JD 失败: %s", exc, exc_info=True)
            steps[4] = steps[4].model_copy(update={"status": "failed", "detail": str(exc)})
            response = AgentChatResponse(
                message=f"我找到了 JD，但保存修改时失败了：{str(exc)}",
                intent="jd_edit",
                route="/recruitment/jd-generator",
                steps=steps,
                artifacts=artifacts,
                suggestions=["稍后重试", "去 JD 管理手动编辑", "重新生成 JD"],
            )
            yield {"type": "final", "response": response.model_dump()}
            return

        artifacts.append(AgentArtifact(
            type="job_description",
            title=f"{updated_jd.title} JD",
            content=updated_jd.content,
            metadata={
                "job_title": updated_jd.title,
                "saved_jd_id": str(updated_jd.id),
                "edit_instructions": edit_instructions,
                "route": "/recruitment/jd-generator",
            },
        ))
        response = AgentChatResponse(
            message=f"已按你的要求更新「{updated_jd.title}」。",
            intent="jd_edit",
            route="/recruitment/jd-generator",
            steps=steps,
            artifacts=artifacts,
            suggestions=["继续修改 JD", "基于这个 JD 筛选简历", "去 JD 管理查看职位"],
        )
        yield {"type": "final", "response": response.model_dump()}

    def _jd_edit_stream_event(
        self,
        event_type: str,
        message: str,
        steps: List[AgentStep],
        artifacts: List[AgentArtifact],
    ) -> Dict[str, Any]:
        return {
            "type": event_type,
            "response": AgentChatResponse(
                message=message,
                intent="jd_edit",
                route="/recruitment/jd-generator",
                steps=steps,
                artifacts=artifacts,
                suggestions=[],
            ).model_dump(),
        }

    async def _stream_criteria_edit_response(
        self,
        message: str,
        user_id: UUID,
        conversation_id: Optional[str],
        memory_context: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        selected_tool = self.tool_registry.get("criteria_edit")
        edit_request = await self._parse_criteria_edit_request(message, memory_context)
        steps = [
            self._planning_step(selected_tool, "识别到评分标准修改请求，先定位目标评分标准。"),
            AgentStep(id="locate_criteria", title="定位评分标准", status="running", detail="优先查找当前对话最近生成的评分标准。", tool="edit_scoring_criteria"),
            AgentStep(id="collect_changes", title="确认修改要求", status="pending", detail="拿到明确修改点后再改写。"),
            AgentStep(id="rewrite_criteria", title="改写评分标准", status="pending", detail="基于原评分标准和修改要求生成新版。"),
            AgentStep(id="save_criteria", title="保存修改", status="pending", detail="更新原评分标准。"),
        ]
        artifacts: List[AgentArtifact] = []
        yield self._criteria_edit_stream_event("plan", "正在定位要修改的评分标准。", steps, artifacts)

        candidates = await self._resolve_recent_criteria_candidates(user_id, conversation_id, edit_request.get("keyword") or "")
        if not candidates:
            steps[1] = steps[1].model_copy(update={"status": "failed", "detail": "没有找到可修改的评分标准。"})
            response = AgentChatResponse(
                message="我还没有找到可以修改的评分标准。请先生成并保存评分标准，或者明确告诉我要修改哪个岗位的评分标准。",
                intent="criteria_edit",
                route="/recruitment/resume-screening",
                steps=steps,
                artifacts=artifacts,
                suggestions=["生成评分标准", "说明岗位名称", "查看简历筛选"],
            )
            yield {"type": "final", "response": response.model_dump()}
            return

        if len(candidates) > 1 and edit_request.get("keyword"):
            matches = self._match_delete_resources(candidates, edit_request.get("keyword") or "")
            if matches:
                candidates = matches
        if len(candidates) > 1:
            steps[1] = steps[1].model_copy(update={"status": "running", "detail": f"找到 {len(candidates)} 个可能的评分标准，需要确认。"})
            artifacts.append(AgentArtifact(type="criteria_edit_candidates", title="待确认评分标准", content={"candidates": candidates[:5]}))
            response = AgentChatResponse(
                message="我找到了多个可能要修改的评分标准。请告诉我具体是哪一个：\n" + "\n".join(f"- {item['name']}" for item in candidates[:5]),
                intent="criteria_edit",
                route="/recruitment/resume-screening",
                steps=steps,
                artifacts=artifacts,
                suggestions=["修改最新的评分标准", "说明岗位名称", "查看评分标准列表"],
            )
            yield {"type": "final", "response": response.model_dump()}
            return

        target = candidates[0]
        steps[1] = steps[1].model_copy(update={"status": "completed", "detail": f"已定位：{target.get('name') or '最近的评分标准'}。"})
        yield self._criteria_edit_stream_event("progress", f"已定位「{target.get('name') or '最近的评分标准'}」。", steps, artifacts)

        edit_instructions = self._clean_optional_value(edit_request.get("changes"))
        if not edit_instructions:
            steps[2] = steps[2].model_copy(update={"status": "pending", "detail": "等待用户说明要修改哪些评分规则。"})
            artifacts.append(AgentArtifact(
                type="criteria_edit_request",
                title="等待修改要求",
                content={"criteria_id": target.get("id"), "title": target.get("name")},
            ))
            response = AgentChatResponse(
                message=f"可以，我找到了「{target.get('name') or '最近的评分标准'}」。你想具体改哪些评分规则？比如分值、维度、淘汰项或加分项。",
                intent="criteria_edit",
                route="/recruitment/resume-screening",
                steps=steps,
                artifacts=artifacts,
                suggestions=["技能匹配改成 40 分", "增加大模型项目经验加分项", "降低学历权重"],
            )
            yield {"type": "final", "response": response.model_dump()}
            return

        steps[2] = steps[2].model_copy(update={"status": "completed", "detail": edit_instructions})
        steps[3] = steps[3].model_copy(update={"status": "running", "detail": "正在读取原评分标准并生成修改版。"})
        yield self._criteria_edit_stream_event("progress", "已收到修改要求，正在改写评分标准。", steps, artifacts)

        try:
            service = ScoringCriteriaService(self.db)
            original = await service.get_scoring_criteria(str(target["id"]), user_id)
            original_data = original.model_dump(mode="json")
            updated_content = await self._rewrite_criteria_content(original_data.get("content") or "", edit_instructions)
            steps[3] = steps[3].model_copy(update={"status": "completed", "detail": "已生成修改后的评分标准。"})
            yield self._criteria_edit_stream_event("progress", "评分标准修改版已生成，正在保存。", steps, artifacts)

            update_data = self._build_criteria_update_payload(original_data, updated_content, edit_instructions)
            steps[4] = steps[4].model_copy(update={"status": "running", "detail": "正在保存到原评分标准。"})
            yield self._criteria_edit_stream_event("progress", "正在保存修改。", steps, artifacts)
            updated = await service.update_scoring_criteria(str(target["id"]), update_data, user_id)
            steps[4] = steps[4].model_copy(update={"status": "completed", "detail": f"已更新评分标准：{updated.id}"})
        except Exception as exc:
            logger.warning("Agent 流式修改评分标准失败: %s", exc, exc_info=True)
            steps[4] = steps[4].model_copy(update={"status": "failed", "detail": str(exc)})
            response = AgentChatResponse(
                message=f"我找到了评分标准，但保存修改时失败了：{str(exc)}",
                intent="criteria_edit",
                route="/recruitment/resume-screening",
                steps=steps,
                artifacts=artifacts,
                suggestions=["稍后重试", "重新生成评分标准", "查看简历筛选"],
            )
            yield {"type": "final", "response": response.model_dump()}
            return

        artifacts.append(AgentArtifact(
            type="scoring_criteria",
            title=updated.title,
            content=updated.content,
            metadata={
                "job_title": updated.job_title,
                "saved_criteria_id": str(updated.id),
                "edit_instructions": edit_instructions,
                "route": "/recruitment/resume-screening",
            },
        ))
        response = AgentChatResponse(
            message=f"已按你的要求更新「{updated.title}」。",
            intent="criteria_edit",
            route="/recruitment/resume-screening",
            steps=steps,
            artifacts=artifacts,
            suggestions=["继续修改评分标准", "基于这个标准筛选简历", "重新生成评分标准"],
        )
        yield {"type": "final", "response": response.model_dump()}

    def _criteria_edit_stream_event(
        self,
        event_type: str,
        message: str,
        steps: List[AgentStep],
        artifacts: List[AgentArtifact],
    ) -> Dict[str, Any]:
        return {
            "type": event_type,
            "response": AgentChatResponse(
                message=message,
                intent="criteria_edit",
                route="/recruitment/resume-screening",
                steps=steps,
                artifacts=artifacts,
                suggestions=[],
            ).model_dump(),
        }

    async def _stream_text(self, text: str) -> AsyncGenerator[str, None]:
        """将完整文本拆成小片段输出，供前端呈现流式打字效果。"""
        if not text:
            return
        parts = re.findall(r".{1,4}(?:\s+|$)|[^\s]{1,4}", text, flags=re.S)
        for part in parts:
            if not part:
                continue
            yield part
            await asyncio.sleep(0.035)

    async def _stream_skill_draft_response(
        self,
        intent: str,
        message: str,
        user_id: UUID,
        conversation_id: Optional[str],
        route: Optional[str],
        memory_context: str,
        confirmed_requirements: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        bundle = self.skill_dispatcher.get_bundle(intent)
        selected_tool = self.tool_registry.get(intent)
        plan_response = AgentChatResponse(
            message="",
            intent=intent,
            route=route,
            steps=[
                self._planning_step(selected_tool, f"已选择 {bundle.bundle_name} skill，开始流式生成草稿。"),
                AgentStep(id="draft", title="生成邮件草稿", status="running", detail="正在逐步生成邮件草稿。", tool="draft"),
                AgentStep(id="confirm_send", title="等待人工确认", status="pending", detail="草稿生成后请确认收件人、主题和正文。"),
                AgentStep(id="send_email", title="提交邮件", status="pending", detail="确认后才会提交给 SMTP 服务器。", tool="send"),
            ],
            suggestions=[],
        )
        yield {"type": "plan", "response": plan_response.model_dump()}

        full_text = ""
        try:
            if self.llm_service is None:
                self.llm_service = LLMService()
            phase = bundle.get_phase("draft")
            skill_markdown = phase.load_skill_instructions()
            prompt = self._build_skill_email_draft_prompt(message, memory_context, skill_markdown)
            async for delta in self.llm_service.stream_response(prompt):
                full_text += delta
                yield {"type": "delta", "delta": delta}
        except Exception as exc:
            logger.warning("Agent 邮件草稿真实流式生成失败，使用模板兜底: %s", exc)
            phase = bundle.get_phase("draft")
            fallback_result = await phase.run(
                {
                    "message": message,
                    "memory_context": memory_context,
                    "confirmed_requirements": confirmed_requirements or {},
                    "llm_service": self.llm_service,
                    "email_service": self.email_send_service,
                    "user_id": user_id,
                    "confirmation_action": bundle.metadata.confirmation_action,
                }
            )
            full_text = str(fallback_result.get("message") or "")
            async for delta in self._stream_text(full_text):
                yield {"type": "delta", "delta": delta}

        response = await self._handle_skill_intent(
            intent=intent,
            message=message,
            user_id=user_id,
            route=route,
            selected_tool=selected_tool,
            memory_context=memory_context,
            confirmed_requirements={
                **(confirmed_requirements or {}),
                "__draft_text": full_text.strip(),
            },
        )
        response = response.model_copy(update={"message": full_text.strip() or response.message})
        yield {"type": "final", "response": response.model_dump()}

    def _build_skill_email_draft_prompt(self, message: str, memory_context: str, skill_markdown: str) -> str:
        memory_block = f"历史上下文：\n{memory_context}\n" if memory_context else ""
        return (
            "你正在执行 hr-agent-email skill。请先遵循下面的 SKILL.md 工作流，再完成当前任务。\n\n"
            f"{skill_markdown}\n\n"
            "当前执行阶段：draft phase。\n"
            "请根据 skill 工作流生成一封中文候选人邮件草稿。\n"
            "要求：包含主题、称呼、正文、下一步动作、署名占位；如果关键信息缺失，用【待补充】标注，不要编造具体时间地点。\n"
            f"{memory_block}"
            f"用户需求：{message}"
        )

    async def _handle_skill_intent(
        self,
        intent: str,
        message: str,
        user_id: UUID,
        route: Optional[str],
        selected_tool: Optional[AgentToolSpec],
        memory_context: str,
        confirmed_requirements: Optional[Dict[str, Any]] = None,
    ) -> AgentChatResponse:
        bundle = self.skill_dispatcher.get_bundle(intent)
        phase_name = bundle.resolve_phase(confirmed_requirements)
        phase = bundle.get_phase(phase_name)
        result = await phase.run(
            {
                "message": message,
                "memory_context": memory_context,
                "confirmed_requirements": confirmed_requirements or {},
                "llm_service": self.llm_service,
                "email_service": self.email_send_service,
                "user_id": user_id,
                "confirmation_action": bundle.metadata.confirmation_action,
                "draft_text": (confirmed_requirements or {}).get("__draft_text"),
            }
        )
        return self._build_skill_response(
            bundle=bundle,
            result=result,
            route=route,
            selected_tool=selected_tool,
            phase_name=phase_name,
            confirmed_requirements=confirmed_requirements,
        )

    def _build_skill_response(
        self,
        bundle: AgentSkillBundle,
        result: Dict[str, Any],
        route: Optional[str],
        selected_tool: Optional[AgentToolSpec],
        phase_name: str,
        confirmed_requirements: Optional[Dict[str, Any]] = None,
    ) -> AgentChatResponse:
        raw_steps = result.get("steps") or []
        steps = [AgentStep.model_validate(step) for step in raw_steps]
        if steps and selected_tool:
            summary = (
                "用户已确认 skill 所需信息，开始执行下一阶段。"
                if confirmed_requirements
                else f"已选择 {bundle.bundle_name} skill，当前执行 {phase_name} 阶段。"
            )
            steps = [self._planning_step(selected_tool, summary), *steps]

        artifacts = [AgentArtifact.model_validate(artifact) for artifact in (result.get("artifacts") or [])]
        return AgentChatResponse(
            message=str(result.get("message") or ""),
            intent=bundle.intent,
            route=route,
            steps=steps,
            artifacts=artifacts,
            suggestions=[str(item) for item in (result.get("suggestions") or [])],
            requires_confirmation=bool(result.get("requires_confirmation")),
            missing_fields=[str(item) for item in (result.get("missing_fields") or [])],
        )

    async def stream_recruitment_agent(
        self,
        message: str,
        user_id: UUID,
        conversation_id: Optional[str],
        confirmed_requirements: Dict[str, Any],
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """流式执行确认后的招聘 Agent 任务，向前端推送真实阶段进度"""
        parsed = self._normalize_requirements(confirmed_requirements, message)
        route = "/recruitment/jd-generator"
        steps: List[AgentStep] = [
            self._planning_step(self.tool_registry.get("jd"), "招聘信息已确认，按 JD → 评分标准的工具链执行。"),
            AgentStep(id="parse", title="确认招聘需求", status="completed", detail=self._brief_requirements(parsed)),
            AgentStep(id="jd", title="生成岗位 JD", status="pending", detail="等待调用大模型", tool="generate_jd"),
            AgentStep(id="save_jd", title="保存 JD", status="pending", detail="等待 JD 生成完成"),
            AgentStep(id="criteria", title="生成简历评分标准", status="pending", detail="等待 JD 保存完成", tool="generate_scoring_criteria"),
            AgentStep(id="save_criteria", title="保存评分标准", status="pending", detail="等待评分标准生成完成"),
        ]
        artifacts: List[AgentArtifact] = [
            AgentArtifact(type="requirements", title="结构化招聘需求", content=parsed)
        ]

        yield self._stream_event("progress", "已确认招聘需求，准备生成 JD。", steps, artifacts)

        steps[2] = steps[2].model_copy(update={"status": "running", "detail": "正在调用大模型生成 JD..."})
        yield self._stream_event("progress", "正在生成岗位 JD。", steps, artifacts)
        yield {"type": "delta", "delta": "## 岗位 JD\n\n", "section": "jd"}
        jd_content = ""
        async for delta in self._generate_jd_stream(message, parsed, conversation_id):
            jd_content += delta
            yield {"type": "delta", "delta": delta, "section": "jd"}
        jd_content = jd_content.strip()
        steps[2] = steps[2].model_copy(update={"status": "completed", "detail": "JD 已生成。"})
        artifacts.append(AgentArtifact(
            type="job_description",
            title=f"{parsed.get('job_title') or '岗位'} JD",
            content=jd_content,
            metadata={"job_title": parsed.get("job_title"), "route": route},
        ))
        yield self._stream_event("artifact", "JD 已生成，正在保存到 JD 管理。", steps, artifacts)

        steps[3] = steps[3].model_copy(update={"status": "running", "detail": "正在保存 JD 记录..."})
        yield self._stream_event("progress", "正在保存 JD。", steps, artifacts)
        saved_jd = await self._save_job_description(jd_content, parsed, message, user_id, conversation_id)
        steps[3] = steps[3].model_copy(update={"status": "completed", "detail": f"已保存到 JD 列表，ID：{saved_jd.id}"})
        artifacts[-1].metadata["saved_jd_id"] = str(saved_jd.id)

        steps[4] = steps[4].model_copy(update={"status": "running", "detail": "正在基于 JD 生成简历评分标准..."})
        yield self._stream_event("progress", "JD 已保存，正在生成简历评分标准。", steps, artifacts)
        yield {"type": "delta", "delta": "\n\n## 简历评分标准\n\n", "section": "scoring_criteria"}
        criteria_content = ""
        async for delta in self._generate_scoring_criteria_stream(jd_content, parsed, conversation_id):
            criteria_content += delta
            yield {"type": "delta", "delta": delta, "section": "scoring_criteria"}
        criteria_content = criteria_content.strip()
        steps[4] = steps[4].model_copy(update={"status": "completed", "detail": "评分标准已生成。"})
        artifacts.append(AgentArtifact(
            type="scoring_criteria",
            title=f"{parsed.get('job_title') or '岗位'}评分标准",
            content=criteria_content,
            metadata={"job_title": parsed.get("job_title"), "saved_jd_id": str(saved_jd.id)},
        ))
        yield self._stream_event("artifact", "评分标准已生成，正在保存。", steps, artifacts)

        steps[5] = steps[5].model_copy(update={"status": "running", "detail": "正在保存评分标准..."})
        yield self._stream_event("progress", "正在保存评分标准。", steps, artifacts)
        saved_criteria = await self._save_scoring_criteria(
            criteria_content,
            parsed,
            user_id,
            conversation_id,
            saved_jd.id,
        )
        steps[5] = steps[5].model_copy(update={"status": "completed", "detail": f"已保存评分标准，ID：{saved_criteria.id}"})
        artifacts[-1].metadata["saved_criteria_id"] = str(saved_criteria.id)

        job_title = parsed.get("job_title") or "这个岗位"
        final_response = AgentChatResponse(
            message=f"我已经按「{job_title}」生成并保存了 JD，同时生成并保存了简历评分标准。需要筛选简历时，告诉我“筛选简历”，我会提醒你上传简历并调用评分工具。",
            intent="jd",
            route=route,
            steps=steps,
            artifacts=artifacts,
            suggestions=["筛选这个 JD 的简历", "去 JD 管理查看职位", "基于 JD 生成面试题"],
        )
        yield {"type": "final", "response": final_response.model_dump()}

    def _stream_event(
        self,
        event_type: str,
        message: str,
        steps: List[AgentStep],
        artifacts: List[AgentArtifact],
    ) -> Dict[str, Any]:
        return {
            "type": event_type,
            "response": AgentChatResponse(
                message=message,
                intent="jd",
                route="/recruitment/jd-generator",
                steps=steps,
                artifacts=artifacts,
                suggestions=[],
            ).model_dump(),
        }

    async def stream_resume_screening(
        self,
        user_id: UUID,
        job_description_id: UUID,
        files: List[Dict[str, Any]],
        conversation_id: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """批量筛选简历并推送真实进度"""
        total = len(files)
        results: List[Dict[str, Any]] = []
        steps = [
            AgentStep(id="upload", title="接收简历文件", status="completed", detail=f"已接收 {total} 份简历"),
            AgentStep(id="evaluate", title="批量 AI 评分", status="running", detail="准备开始评分", tool="evaluate_resume"),
            AgentStep(id="summary", title="生成筛选摘要", status="pending", detail="等待评分完成"),
        ]

        yield self._resume_stream_event(
            "progress",
            f"已收到 {total} 份简历，开始按当前 JD 批量评分。",
            steps,
            results,
        )
        yield {"type": "delta", "delta": "## 简历评分结果\n\n", "section": "resume_screening"}

        evaluation_service = ResumeEvaluationService(self.db)
        for index, file_info in enumerate(files, start=1):
            filename = file_info["filename"]
            steps[1] = steps[1].model_copy(update={
                "status": "running",
                "detail": f"正在评分第 {index}/{total} 份：{filename}",
            })
            yield self._resume_stream_event(
                "progress",
                f"正在评分：{filename}",
                steps,
                results,
            )

            try:
                result = await evaluation_service.evaluate_resume(
                    user_id=user_id,
                    file_content=file_info["content"],
                    filename=filename,
                    job_description_id=job_description_id,
                    conversation_id=conversation_id,
                )
                result_data = {
                    "id": str(result.get("id")),
                    "filename": filename,
                    "name": result.get("name") or filename,
                    "score": result.get("total_score") or 0,
                    "position": result.get("position"),
                    "education": result.get("education"),
                    "work_years": result.get("workYears"),
                    "metrics": result.get("evaluation_metrics") or [],
                    "status": "completed",
                }
            except Exception as exc:
                logger.exception("Agent 批量筛选简历失败: %s", filename)
                result_data = {
                    "filename": filename,
                    "name": filename,
                    "score": 0,
                    "status": "failed",
                    "error": str(exc),
                }

            results.append(result_data)
            yield {
                "type": "delta",
                "delta": self._format_resume_result_delta(result_data, index, total),
                "section": "resume_screening",
                "result": result_data,
            }
            yield self._resume_stream_event(
                "artifact",
                f"已完成 {index}/{total}：{filename}",
                steps,
                results,
            )

        failed_count = len([item for item in results if item.get("status") == "failed"])
        success_count = total - failed_count
        steps[1] = steps[1].model_copy(update={"status": "completed", "detail": f"完成 {success_count} 份，失败 {failed_count} 份"})
        steps[2] = steps[2].model_copy(update={"status": "completed", "detail": "已生成批量筛选结果"})

        sorted_results = sorted(results, key=lambda item: item.get("score") or 0, reverse=True)
        loop_summary = self._build_resume_screening_loop_summary(sorted_results, threshold=60)
        final_response = AgentChatResponse(
            message=(
                f"批量简历筛选完成：成功 {success_count} 份，失败 {failed_count} 份。结果已保存到简历筛选列表。"
                f"{loop_summary['message_suffix']}"
            ),
            intent="resume_screening",
            route="/recruitment/resume-screening",
            steps=steps,
            artifacts=[
                AgentArtifact(
                    type="resume_screening_results",
                    title="批量简历筛选结果",
                    content=sorted_results,
                    metadata={
                        "job_description_id": str(job_description_id),
                        "total": total,
                        "qualified_threshold": 60,
                    },
                ),
                AgentArtifact(
                    type="resume_screening_followup",
                    title="招聘闭环建议",
                    content=loop_summary,
                    metadata={"threshold": 60},
                )
            ],
            suggestions=loop_summary["suggestions"],
        )
        yield {"type": "final", "response": final_response.model_dump()}

    def _format_resume_result_delta(self, result: Dict[str, Any], index: int, total: int) -> str:
        filename = result.get("filename") or "未知文件"
        name = result.get("name") or filename
        if result.get("status") == "failed":
            return f"{index}. {filename}：评分失败（{result.get('error') or '未知错误'}）\n\n"

        score = result.get("score") or 0
        position = result.get("position") or "未识别岗位"
        education = result.get("education") or "学历未识别"
        work_years = result.get("work_years")
        work_years_text = f"{work_years} 年经验" if work_years not in (None, "", 0) else "经验未识别"
        metrics = result.get("metrics") or []
        metric_summary = self._summarize_resume_metrics(metrics)

        lines = [
            f"{index}. {name}（{filename}）",
            f"- 综合评分：{score} 分",
            f"- 候选信息：{position}，{education}，{work_years_text}",
        ]
        if metric_summary:
            lines.append(f"- 评分摘要：{metric_summary}")
        lines.append("")
        return "\n".join(lines)

    def _summarize_resume_metrics(self, metrics: List[Dict[str, Any]]) -> str:
        if not metrics:
            return ""
        summaries = []
        for metric in metrics[:3]:
            name = metric.get("name") or "指标"
            score = metric.get("score")
            max_score = metric.get("max")
            reason = (metric.get("reason") or "").strip()
            score_text = f"{score}/{max_score}" if max_score is not None else str(score)
            summaries.append(f"{name} {score_text}" + (f"（{reason[:40]}）" if reason else ""))
        return "；".join(summaries)

    async def stream_interview_plan(
        self,
        user_id: UUID,
        resume_evaluation_id: UUID,
        conversation_id: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """根据已评分简历生成并保存面试计划"""
        steps = [
            AgentStep(id="load_resume", title="读取候选人资料", status="running", detail="正在读取简历评分记录"),
            AgentStep(id="generate_plan", title="生成面试计划", status="pending", tool="generate_interview_plan"),
            AgentStep(id="save_plan", title="保存面试计划", status="pending"),
        ]
        yield self._tool_stream_event("progress", "我正在读取候选人的简历评分结果。", "interview_plan", "/recruitment/smart-interview", steps, [])

        resume = await self._get_resume_evaluation(user_id, resume_evaluation_id)
        job_description = await JobDescriptionService(self.db).get_job_description(
            jd_id=str(resume.job_description_id),
            user_id=user_id,
        )
        steps[0] = steps[0].model_copy(update={"status": "completed", "detail": f"候选人：{resume.candidate_name or resume.original_filename}"})
        steps[1] = steps[1].model_copy(update={"status": "running", "detail": "正在结合 JD、简历和评分结果生成面试计划..."})
        yield self._tool_stream_event("progress", "资料已就绪，开始生成面试计划。", "interview_plan", "/recruitment/smart-interview", steps, [])

        yield {"type": "delta", "delta": "## 面试计划\n\n", "section": "interview_plan"}
        plan_content = ""
        async for delta in self._generate_interview_plan_stream(resume, job_description.content, conversation_id):
            plan_content += delta
            yield {"type": "delta", "delta": delta, "section": "interview_plan"}
        plan_content = plan_content.strip()
        steps[1] = steps[1].model_copy(update={"status": "completed", "detail": "面试计划已生成。"})
        artifacts = [
            AgentArtifact(
                type="interview_plan",
                title=f"{resume.candidate_name or '候选人'} 面试计划",
                content=plan_content,
                metadata={"resume_evaluation_id": str(resume.id)},
            )
        ]
        yield self._tool_stream_event("artifact", "面试计划已生成，正在保存。", "interview_plan", "/recruitment/smart-interview", steps, artifacts)

        steps[2] = steps[2].model_copy(update={"status": "running", "detail": "正在保存到面试计划列表..."})
        saved_plan = await InterviewPlanService(self.db).create_interview_plan(
            user_id=user_id,
            plan_data=InterviewPlanCreate(
                resume_evaluation_id=resume.id,
                candidate_name=resume.candidate_name or resume.original_filename,
                candidate_position=resume.candidate_position or getattr(job_description, "title", "候选岗位"),
                content=plan_content,
            ),
        )
        resume.status = ResumeStatus.INTERVIEW
        await self.db.commit()
        steps[2] = steps[2].model_copy(update={"status": "completed", "detail": f"已保存，ID：{saved_plan.id}；候选人状态已同步为面试"})
        artifacts[0].metadata["saved_plan_id"] = str(saved_plan.id)
        artifacts[0].metadata["candidate_name"] = resume.candidate_name or resume.original_filename
        artifacts[0].metadata["candidate_position"] = resume.candidate_position or getattr(job_description, "title", "候选岗位")
        response = AgentChatResponse(
            message=(
                f"面试计划已生成并保存。建议围绕「{resume.candidate_name or '该候选人'}」的高匹配项做深挖，"
                "同时对评分中较弱的能力设计追问，避免只凭简历亮点做判断。候选人状态也已推进到“面试”，刷新面试计划页面后会出现在列表中。\n\n"
                "如果你想把招聘链路继续往前推，可以上传岗位资料、技术文档或题库材料，然后告诉我“基于当前面试方案生成笔试试卷”。"
            ),
            intent="interview_plan",
            route="/recruitment/smart-interview",
            steps=steps,
            artifacts=artifacts,
            suggestions=["基于当前面试方案生成笔试试卷", "写面试邀约邮件", "查看面试计划列表"],
        )
        yield {"type": "final", "response": response.model_dump()}

    async def stream_exam_generation(
        self,
        user_id: UUID,
        exam_requirements: Dict[str, Any],
        conversation_id: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """生成并保存考试试卷"""
        exam = self._normalize_exam_requirements(exam_requirements)
        exam = await self._hydrate_exam_interview_context(exam, user_id, conversation_id)
        steps = [
            AgentStep(id="confirm_exam", title="确认考试配置", status="completed", detail=self._brief_exam_requirements(exam)),
            AgentStep(id="generate_exam", title="生成考试试卷", status="running", detail="正在调用考试生成工具...", tool="generate_exam"),
            AgentStep(id="save_exam", title="保存试卷", status="pending", detail="等待生成完成"),
        ]
        yield self._tool_stream_event("progress", "考试配置已确认，开始生成试卷。", "exam_generate", "/training/exam-generator", steps, [])

        exam_service = ExamService(self.db)
        yield {"type": "delta", "delta": "## 考试试卷\n\n", "section": "exam_generate"}
        raw_exam_content = ""
        display_exam_content = ""
        pending_exam_buffer = ""
        question_index = 1
        async for raw_delta in self._generate_exam_content_stream(exam_service, exam, user_id, conversation_id):
            raw_exam_content += raw_delta
            pending_exam_buffer += raw_delta
            display_delta, pending_exam_buffer, question_index = self._drain_exam_display_buffer(
                pending_exam_buffer,
                question_index,
            )
            if display_delta:
                display_exam_content += display_delta
                yield {"type": "delta", "delta": display_delta, "section": "exam_generate"}

        display_delta, pending_exam_buffer, question_index = self._drain_exam_display_buffer(
            pending_exam_buffer,
            question_index,
            force=True,
        )
        if display_delta:
            display_exam_content += display_delta
            yield {"type": "delta", "delta": display_delta, "section": "exam_generate"}

        repair_content = await self._generate_missing_exam_questions(
            exam_service=exam_service,
            exam=exam,
            existing_content=raw_exam_content,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if repair_content:
            if raw_exam_content.strip():
                raw_exam_content = f"{raw_exam_content.rstrip()}\n***\n{repair_content.strip()}"
            else:
                raw_exam_content = repair_content.strip()
            display_delta, _, question_index = self._drain_exam_display_buffer(
                repair_content,
                question_index,
                force=True,
            )
            if display_delta:
                display_exam_content += display_delta
                yield {"type": "delta", "delta": display_delta, "section": "exam_generate"}

        exam_content = raw_exam_content.strip()
        steps[1] = steps[1].model_copy(update={"status": "completed", "detail": "试卷已生成。"})
        artifacts = [
            AgentArtifact(
                type="exam_paper",
                title=exam["title"],
                content=display_exam_content.strip() or exam_content,
                metadata={"subject": exam["subject"]},
            )
        ]
        yield self._tool_stream_event("artifact", "试卷已生成，正在保存到考试管理。", "exam_generate", "/training/exam-generator", steps, artifacts)

        steps[2] = steps[2].model_copy(update={"status": "running", "detail": "正在保存试卷..."})
        saved_exam = await exam_service.save_exam({**exam, "content": exam_content}, str(user_id))
        saved_exam_id = saved_exam.get("id")
        share_url = f"/exam-share/{saved_exam_id}" if saved_exam_id else None
        steps[2] = steps[2].model_copy(update={"status": "completed", "detail": f"已保存，ID：{saved_exam.get('id')}"})
        artifacts[0].metadata["saved_exam_id"] = saved_exam_id
        artifacts[0].metadata["share_url"] = share_url
        share_message = f"\n\n分享链接：[打开试卷分享页]({share_url})\n\n也可以复制：`{share_url}`" if share_url else ""
        response = AgentChatResponse(
            message=f"试卷「{exam['title']}」已生成并保存。你可以把它作为候选人的笔试题，也可以继续让我生成考试邀请邮件。{share_message}",
            intent="exam_generate",
            route="/training/exam-generator",
            steps=steps,
            artifacts=artifacts,
            suggestions=["写考试邀请邮件", "去考试管理查看"],
        )
        yield {"type": "final", "response": response.model_dump()}

    async def _generate_exam_content_stream(
        self,
        exam_service: ExamService,
        exam: Dict[str, Any],
        user_id: UUID,
        conversation_id: Optional[str],
    ) -> AsyncGenerator[str, None]:
        full_text = ""
        special_requirements = self._build_exam_special_requirements(exam)
        try:
            stream = await exam_service.generate_exam(
                title=exam["title"],
                subject=exam["subject"],
                total_score=exam["total_score"],
                user_id=str(user_id),
                description=exam.get("description"),
                difficulty=exam.get("difficulty"),
                duration=exam.get("duration"),
                question_types=exam.get("question_types"),
                question_counts=exam.get("question_counts"),
                knowledge_files=exam.get("knowledge_files"),
                special_requirements=special_requirements,
                conversation_id=None,
                stream=True,
            )
            async for chunk in stream:
                raw_delta = self._extract_stream_delta(self._normalize_sse_chunk(chunk))
                delta = self._dedupe_stream_delta(raw_delta, full_text)
                if not delta:
                    continue
                full_text += delta
                yield delta
        except Exception as exc:
            logger.warning("Agent 试卷流式生成失败，回退同步生成: %s", exc)

        if not full_text.strip():
            result = await exam_service.generate_exam(
                title=exam["title"],
                subject=exam["subject"],
                total_score=exam["total_score"],
                user_id=str(user_id),
                description=exam.get("description"),
                difficulty=exam.get("difficulty"),
                duration=exam.get("duration"),
                question_types=exam.get("question_types"),
                question_counts=exam.get("question_counts"),
                knowledge_files=exam.get("knowledge_files"),
                special_requirements=special_requirements,
                conversation_id=None,
                stream=False,
            )
            fallback = self._extract_answer(result).strip()
            async for delta in self._stream_text(fallback):
                yield delta

    async def _generate_missing_exam_questions(
        self,
        exam_service: ExamService,
        exam: Dict[str, Any],
        existing_content: str,
        user_id: UUID,
        conversation_id: Optional[str],
    ) -> str:
        missing_counts = self._missing_exam_question_counts(exam_service, exam, existing_content)
        if not any(missing_counts.values()):
            return ""

        logger.warning("试卷题量不足，尝试补齐缺失题目: %s", missing_counts)
        repair_exam = {
            **exam,
            "title": f"{exam.get('title') or '试卷'}补题",
            "question_counts": missing_counts,
            "question_types": [key for key, value in missing_counts.items() if value > 0],
            "total_score": sum(
                sum(scores)
                for scores in self._missing_exam_question_scores(exam, existing_content, missing_counts).values()
            ) or exam.get("total_score") or 100,
            "special_requirements": "\n".join([
                self._clean_optional_value(exam.get("special_requirements")) or "",
                "这是补题请求。只生成缺失题目，不要重复已生成题目。",
                "已生成题目如下，请避开相同题干和知识点：",
                existing_content[:3000],
            ]).strip(),
        }
        result = await exam_service.generate_exam(
            title=repair_exam["title"],
            subject=repair_exam["subject"],
            total_score=repair_exam["total_score"],
            user_id=str(user_id),
            description=repair_exam.get("description"),
            difficulty=repair_exam.get("difficulty"),
            duration=repair_exam.get("duration"),
            question_types=repair_exam.get("question_types"),
            question_counts=repair_exam.get("question_counts"),
            knowledge_files=repair_exam.get("knowledge_files"),
            special_requirements=repair_exam.get("special_requirements"),
            conversation_id=None,
            stream=False,
        )
        repair_content = self._extract_answer(result).strip()
        repair_questions = exam_service._parse_exam_content(repair_content)
        expected_repair_count = sum(missing_counts.values())
        if len(repair_questions) < expected_repair_count:
            logger.warning("试卷补题仍不足，期望 %s 题，实际 %s 题", expected_repair_count, len(repair_questions))
        return repair_content

    def _missing_exam_question_counts(
        self,
        exam_service: ExamService,
        exam: Dict[str, Any],
        content: str,
    ) -> Dict[str, int]:
        expected = {
            "single_choice": int((exam.get("question_counts") or {}).get("single_choice") or 0),
            "multiple_choice": int((exam.get("question_counts") or {}).get("multiple_choice") or 0),
            "short_answer": int((exam.get("question_counts") or {}).get("short_answer") or 0),
        }
        if not any(expected.values()):
            return {"single_choice": 0, "multiple_choice": 0, "short_answer": 0}

        actual = {"single_choice": 0, "multiple_choice": 0, "short_answer": 0}
        type_map = {
            "单选题": "single_choice",
            "单选": "single_choice",
            "多选题": "multiple_choice",
            "多选": "multiple_choice",
            "简答题": "short_answer",
            "简答": "short_answer",
        }
        for question in exam_service._parse_exam_content(content):
            key = type_map.get(str(question.get("type") or "").strip())
            if key:
                actual[key] += 1

        return {
            key: max(0, expected.get(key, 0) - actual.get(key, 0))
            for key in expected
        }

    def _missing_exam_question_scores(
        self,
        exam: Dict[str, Any],
        content: str,
        missing_counts: Dict[str, int],
    ) -> Dict[str, List[int]]:
        question_scores = ExamService(self.db)._allocate_question_scores(
            int(exam.get("total_score") or 100),
            exam.get("question_counts") or {},
        )
        return {
            key: values[-missing_counts[key]:] if missing_counts.get(key) else []
            for key, values in question_scores.items()
        }

    def _drain_exam_display_buffer(
        self,
        buffer: str,
        start_index: int,
        force: bool = False,
    ) -> tuple[str, str, int]:
        if not buffer:
            return "", "", start_index

        parts = buffer.split("***")
        if force:
            complete_parts = parts
            remaining = ""
        elif len(parts) <= 1:
            return "", buffer, start_index
        else:
            complete_parts = parts[:-1]
            remaining = parts[-1]

        markdown_parts: List[str] = []
        question_index = start_index
        for part in complete_parts:
            block = part.strip()
            if not block:
                continue
            markdown = self._format_exam_question_block(block, question_index)
            markdown_parts.append(markdown)
            question_index += 1

        return "".join(markdown_parts), remaining, question_index

    def _format_exam_question_block(self, block: str, question_index: int) -> str:
        parts = [part.strip() for part in block.split("|")]
        if len(parts) != 6:
            return f"{block}\n\n"

        question_text, question_type, options_text, correct_answers, score, explanation = parts
        question_type_label = {
            "single_choice": "单选题",
            "multiple_choice": "多选题",
            "short_answer": "简答题",
            "single": "单选题",
            "multiple": "多选题",
            "short": "简答题",
        }.get(question_type, question_type)
        lines = [
            f"### {question_index}. {question_type_label}（{score}分）",
            "",
            question_text,
        ]
        if options_text:
            option_items = [
                option.strip()
                for option in re.split(r"[;；]", options_text)
                if option.strip()
            ]
            if option_items:
                lines.extend(["", "**选项：**"])
                for option_index, option in enumerate(option_items):
                    label = chr(65 + option_index)
                    option_text = re.sub(r"^[A-ZＡ-Ｚ][\\.．、:：]\\s*", "", option)
                    lines.append(f"- {label}. {option_text}")

        lines.extend([
            "",
            f"**参考答案：** {correct_answers or '略'}",
            f"**解析：** {explanation or '略'}",
            "",
        ])
        return "\n".join(lines)

    async def _hydrate_exam_interview_context(
        self,
        exam: Dict[str, Any],
        user_id: UUID,
        conversation_id: Optional[str],
    ) -> Dict[str, Any]:
        if exam.get("interview_plan_context"):
            return exam
        text = " ".join([
            str(exam.get("special_requirements") or ""),
            str(exam.get("description") or ""),
            str(exam.get("title") or ""),
        ])
        if not self._is_followup_interview_exam_request(text):
            return exam
        context = await self._resolve_interview_exam_followup(text, user_id, conversation_id)
        if not context.get("matched"):
            return exam
        return {
            **exam,
            "interview_plan_context": context,
        }

    def _build_exam_special_requirements(self, exam: Dict[str, Any]) -> str:
        base_requirements = self._clean_optional_value(exam.get("special_requirements")) or ""
        context = exam.get("interview_plan_context") if isinstance(exam.get("interview_plan_context"), dict) else None
        if not context:
            return base_requirements

        context_text = self._clean_optional_value(context.get("content")) or ""
        context_title = self._clean_optional_value(context.get("title")) or "当前面试方案"
        parts = []
        if base_requirements:
            parts.append(base_requirements)
        parts.append(
            "请按 8:2 配比生成笔试试卷：约 80% 题目来自上传文档知识点，约 20% 题目来自面试方案中的能力验证点、风险点或追问方向。"
            "文档题要依据上传文档；面试方案导向题可以设计情境题或简答题，但要尽量结合文档业务背景。"
            "不要把面试流程、面试安排、候选人评价本身直接改写成题目。"
        )
        if context_text:
            parts.append(f"当前面试方案《{context_title}》（仅作为出题方向和权重参考，不作为题目事实来源）：\n{context_text[:1500]}")
        return "\n\n".join(parts)

    async def stream_exam_generation_with_documents(
        self,
        user_id: UUID,
        exam_requirements: Dict[str, Any],
        files: List[Dict[str, Any]],
        conversation_id: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """上传并处理文档后，基于文档生成并保存考试试卷"""
        exam = self._normalize_exam_requirements(exam_requirements)
        total = len(files)
        steps = [
            AgentStep(id="upload_docs", title="接收参考文档", status="completed", detail=f"已接收 {total} 个文档"),
            AgentStep(id="process_docs", title="解析文档内容", status="running", detail="正在提取文档文本"),
            AgentStep(id="generate_exam", title="基于文档生成试卷", status="pending", tool="generate_exam"),
            AgentStep(id="save_exam", title="保存试卷", status="pending"),
        ]
        artifacts: List[AgentArtifact] = []
        yield self._tool_stream_event("progress", f"已收到 {total} 个参考文档，开始解析文档内容。", "exam_generate", "/training/exam-generator", steps, artifacts)

        exam = await self._hydrate_exam_interview_context(exam, user_id, conversation_id)
        if exam.get("interview_plan_context"):
            steps[0] = steps[0].model_copy(update={
                "detail": f"已接收 {total} 个文档，并关联当前面试方案",
            })
            artifacts.append(AgentArtifact(
                type="exam_interview_plan_context",
                title="试卷参考面试方案",
                content={
                    "title": exam["interview_plan_context"].get("title"),
                    "plan_id": exam["interview_plan_context"].get("plan_id"),
                },
            ))
            yield self._tool_stream_event(
                "artifact",
                f"已关联面试方案「{exam['interview_plan_context'].get('title')}」，出题会同时参考文档和面试方案。",
                "exam_generate",
                "/training/exam-generator",
                steps,
                artifacts,
            )

        knowledge_files: List[Dict[str, Any]] = []
        for index, file_info in enumerate(files, start=1):
            filename = file_info["filename"]
            steps[1] = steps[1].model_copy(update={"status": "running", "detail": f"正在解析第 {index}/{total} 个文档：{filename}"})
            yield self._tool_stream_event("progress", f"正在解析文档：{filename}", "exam_generate", "/training/exam-generator", steps, artifacts)

            document = await self._save_exam_source_document(
                filename=filename,
                content=file_info["content"],
                user_id=user_id,
            )
            if not document.extracted_content:
                extracted_content = await extract_text_content(document.file_path, document.mime_type)
                if not extracted_content:
                    raise ValueError(f"{filename} 未能解析出文本内容，请上传 PDF、DOCX、TXT 或 MD 格式的文本型文档")
                document.extracted_content = extracted_content
                await self.db.commit()
                await self.db.refresh(document)
            knowledge_files.append({"id": str(document.id), "fileName": document.filename})

        steps[1] = steps[1].model_copy(update={"status": "completed", "detail": f"已完成 {len(knowledge_files)} 个文档解析"})
        steps[2] = steps[2].model_copy(update={"status": "running", "detail": "正在基于文档内容生成试卷..."})
        artifacts.append(AgentArtifact(
            type="exam_source_documents",
            title="试卷参考文档",
            content=knowledge_files,
        ))
        yield self._tool_stream_event("artifact", "文档解析完成，开始基于文档生成试卷。", "exam_generate", "/training/exam-generator", steps, artifacts)

        async for event in self.stream_exam_generation(
            user_id=user_id,
            exam_requirements={**exam, "knowledge_files": knowledge_files},
            conversation_id=conversation_id,
        ):
            if event.get("response") and event["response"].get("steps"):
                response = event["response"]
                response["steps"] = [step.model_dump() for step in steps[:2]] + response["steps"][1:]
                response["artifacts"] = [artifact.model_dump() for artifact in artifacts] + response.get("artifacts", [])
                if event.get("type") == "final":
                    response["message"] = f"已基于 {len(knowledge_files)} 个上传文档生成并保存试卷。{response['message']}"
            yield event

    def _tool_stream_event(
        self,
        event_type: str,
        message: str,
        intent: str,
        route: str,
        steps: List[AgentStep],
        artifacts: List[AgentArtifact],
    ) -> Dict[str, Any]:
        return {
            "type": event_type,
            "response": AgentChatResponse(
                message=message,
                intent=intent,
                route=route,
                steps=steps,
                artifacts=artifacts,
                suggestions=[],
            ).model_dump(),
        }

    def _resume_stream_event(
        self,
        event_type: str,
        message: str,
        steps: List[AgentStep],
        results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "type": event_type,
            "response": AgentChatResponse(
                message=message,
                intent="resume_screening",
                route="/recruitment/resume-screening",
                steps=steps,
                artifacts=[
                    AgentArtifact(
                        type="resume_screening_results",
                        title="批量简历筛选结果",
                        content=results,
                    )
                ] if results else [],
                suggestions=[],
            ).model_dump(),
        }

    async def _build_conversation_memory(
        self,
        conversation_id: Optional[str],
        user_id: UUID,
        current_message: str,
        limit: int = 12,
        max_chars: int = 6000,
    ) -> str:
        if not conversation_id:
            return ""
        try:
            conversation_uuid = UUID(str(conversation_id))
        except Exception:
            return ""

        try:
            conversation_result = await self.db.execute(
                select(Conversation).where(
                    Conversation.id == conversation_uuid,
                    Conversation.user_id == user_id,
                )
            )
            if not conversation_result.scalar_one_or_none():
                return ""

            result = await self.db.execute(
                select(Message)
                .where(Message.conversation_id == conversation_uuid)
                .order_by(Message.created_at.desc())
                .limit(limit + 3)
            )
            messages = list(reversed(result.scalars().all()))
        except Exception as exc:
            logger.warning("读取 Agent 对话记忆失败: %s", exc)
            return ""

        normalized_current = self._normalize_memory_text(current_message)
        memory_lines: List[str] = []
        for item in messages:
            content = self._normalize_memory_text(item.content)
            if not content:
                continue
            if item.role == MessageRole.USER and normalized_current and content.startswith(normalized_current):
                continue
            role_label = "用户" if item.role == MessageRole.USER else "助手"
            if item.role == MessageRole.SYSTEM:
                role_label = "系统"
            memory_lines.append(f"{role_label}: {content[:900]}")

        memory = "\n".join(memory_lines)
        if len(memory) > max_chars:
            memory = memory[-max_chars:]
        return memory.strip()

    def _normalize_memory_text(self, text: Optional[str]) -> str:
        if not text:
            return ""
        normalized = re.sub(r"\s+", " ", str(text)).strip()
        return normalized

    def _format_memory_for_prompt(self, memory_context: str) -> str:
        if not memory_context:
            return ""
        return (
            "历史对话记忆（仅用于理解上下文和指代，不要逐字复述）：\n"
            f"{memory_context}\n\n"
        )

    def _build_memory_augmented_prompt(self, message: str, memory_context: str) -> str:
        if not memory_context:
            return message
        return (
            "请结合以下历史对话记忆回答当前用户消息。"
            "如果用户使用“继续、这个、刚才、上一个”等指代，请从历史中解析；"
            "如果历史无关，不要强行引用。\n\n"
            f"{self._format_memory_for_prompt(memory_context)}"
            f"当前用户消息：{message}"
        )

    async def _resolve_interview_candidate(
        self,
        message: str,
        user_id: UUID,
        memory_context: str = "",
    ) -> Dict[str, Any]:
        candidates = await self._recent_resume_candidates(user_id)
        candidate_text = "\n".join(
            (
                f"- id={item['id']}；姓名={item['name'] or '未提取'}；"
                f"文件={item['filename']}；岗位={item['position'] or '未提及'}；"
                f"分数={item['score'] if item['score'] is not None else '未评分'}"
            )
            for item in candidates
        ) or "无已评分候选人"
        prompt = (
            "你是 HR Agent 的候选人解析器。请判断用户是否指定了要生成面试计划的候选人，"
            "并在已评分候选人列表中匹配。只返回 JSON，不要解释。\n\n"
            "规则：\n"
            "1. 如果用户没有明确候选人姓名/文件名/“他、这位、刚才那位”等指代，返回 status=no_specific。\n"
            "2. 如果用户指定了候选人，并且候选人列表里唯一匹配，返回 status=matched 和 candidate_id。\n"
            "3. 如果用户指定了候选人，但候选人列表没有匹配，返回 status=no_match，并在 reply 中说明未找到该候选人，提醒上传他的简历并先评分。\n"
            "4. 如果匹配多个候选人，返回 status=ambiguous，并给出 candidates。\n"
            "5. reply 要自然、简洁，不能假装已找到不存在的候选人。\n\n"
            "返回 JSON 字段：\n"
            "{\n"
            "  \"status\": \"no_specific/matched/no_match/ambiguous\",\n"
            "  \"requested_name\": \"用户指定的人名或null\",\n"
            "  \"candidate_id\": \"匹配到的id或null\",\n"
            "  \"candidate_name\": \"匹配到的姓名或null\",\n"
            "  \"candidates\": [],\n"
            "  \"reply\": \"给用户的自然回复或null\"\n"
            "}\n\n"
            f"{self._format_memory_for_prompt(memory_context)}"
            f"当前用户消息：{message}\n\n"
            f"已评分候选人列表：\n{candidate_text}"
        )
        try:
            if self.llm_service is None:
                self.llm_service = LLMService()
            response = await self.llm_service.generate_response(prompt)
            parsed = self._safe_json_loads(response)
            return self._normalize_candidate_resolution(parsed, candidates, message)
        except Exception as exc:
            logger.warning("解析面试计划候选人失败，使用本地规则兜底: %s", exc)
            return self._fallback_candidate_resolution(message, candidates)

    async def _recent_resume_candidates(self, user_id: UUID, limit: int = 50) -> List[Dict[str, Any]]:
        result = await self.db.execute(
            select(ResumeEvaluation)
            .where(ResumeEvaluation.user_id == user_id)
            .order_by(ResumeEvaluation.created_at.desc())
            .limit(limit)
        )
        candidates = []
        for resume in result.scalars().all():
            candidates.append({
                "id": str(resume.id),
                "name": resume.candidate_name,
                "filename": resume.original_filename,
                "position": resume.candidate_position,
                "score": resume.total_score,
            })
        return candidates

    def _normalize_candidate_resolution(
        self,
        parsed: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        message: str,
    ) -> Dict[str, Any]:
        status = str(parsed.get("status") or "").strip().lower()
        candidate_id = self._clean_optional_value(parsed.get("candidate_id"))
        candidate_lookup = {item["id"]: item for item in candidates}
        if candidate_id and candidate_id in candidate_lookup:
            matched = candidate_lookup[candidate_id]
            return {
                "status": "matched",
                "candidate_id": candidate_id,
                "candidate_name": matched.get("name") or matched.get("filename"),
                "requested_name": self._clean_optional_value(parsed.get("requested_name")),
                "reply": self._clean_optional_value(parsed.get("reply")),
            }
        if status in {"no_match", "ambiguous", "no_specific"}:
            return {
                "status": status,
                "requested_name": self._clean_optional_value(parsed.get("requested_name")),
                "reply": self._clean_optional_value(parsed.get("reply")),
                "candidates": parsed.get("candidates") if isinstance(parsed.get("candidates"), list) else [],
            }
        return self._fallback_candidate_resolution(message, candidates)

    def _fallback_candidate_resolution(self, message: str, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        normalized_message = self._normalize_candidate_match_text(message)
        matches = [
            item for item in candidates
            if any(
                name and (name in normalized_message or normalized_message in name)
                for name in [
                    self._normalize_candidate_match_text(item.get("name")),
                    self._normalize_candidate_match_text(item.get("filename")),
                ]
            )
        ]
        if len(matches) == 1:
            return {
                "status": "matched",
                "candidate_id": matches[0]["id"],
                "candidate_name": matches[0].get("name") or matches[0].get("filename"),
            }
        if len(matches) > 1:
            return {"status": "ambiguous", "candidates": matches[:5]}
        has_specific_name = bool(re.search(r"生成(.{1,20}?)(?:的)?面试|(.{1,20}?)(?:的)?面试(?:计划|方案)", message))
        return {"status": "no_match" if has_specific_name else "no_specific", "requested_name": None}

    def _normalize_candidate_match_text(self, text: Optional[str]) -> str:
        return re.sub(r"[\s_\-()[\]【】（）.。·、,，]+", "", str(text or "").lower())

    def _is_followup_resume_screening_loop_request(self, message: str) -> bool:
        return bool(
            re.search(r"高分|通过|合格|大于\s*60|超过\s*60|60\s*分以上|分数高|低分|不合格|小于\s*60|低于\s*60", message)
            and re.search(r"面试|方案|计划|删除|淘汰|候选人|简历", message)
        )

    def _is_followup_interview_exam_request(self, message: str) -> bool:
        return bool(
            re.search(r"当前|这个|该|刚才|刚生成|上一个|面试方案|面试计划", message)
            and re.search(r"试卷|考试|笔试|出题|题目|测评", message)
        )

    def _build_resume_screening_loop_summary(
        self,
        results: List[Dict[str, Any]],
        threshold: int = 60,
    ) -> Dict[str, Any]:
        completed = [item for item in results if item.get("status") != "failed"]
        qualified = [item for item in completed if float(item.get("score") or 0) >= threshold]
        low_scores = [item for item in completed if float(item.get("score") or 0) < threshold]
        top_candidates = qualified[:3]

        message_parts = []
        if qualified:
            names = "、".join(self._resume_candidate_label(item) for item in top_candidates)
            message_parts.append(f"{len(qualified)} 位候选人达到 {threshold} 分及以上（{names}），建议进入面试计划阶段。")
        if low_scores:
            names = "、".join(self._resume_candidate_label(item) for item in low_scores[:3])
            message_parts.append(f"{len(low_scores)} 位候选人低于 {threshold} 分（{names}），建议标记淘汰或删除记录前先人工复核。")

        suggestions = []
        if qualified:
            suggestions.append("为高分候选人生成面试计划")
        if low_scores:
            suggestions.append("删除低分候选人的简历记录")
        suggestions.extend(["查看完整评分详情", "继续上传更多简历"])

        return {
            "threshold": threshold,
            "qualified_candidates": qualified,
            "low_score_candidates": low_scores,
            "message_suffix": ("\n\n下一步建议：" + " ".join(message_parts)) if message_parts else "",
            "suggestions": suggestions[:4],
        }

    def _resume_candidate_label(self, item: Dict[str, Any]) -> str:
        name = item.get("name") or item.get("filename") or "候选人"
        score = item.get("score")
        return f"{name} {score}分" if score is not None else str(name)

    async def _resolve_resume_screening_followup_action(
        self,
        message: str,
        user_id: UUID,
        conversation_id: Optional[str],
    ) -> Dict[str, Any]:
        if not self._is_followup_resume_screening_loop_request(message):
            return {"action": None}
        groups = await self._recent_resume_screening_groups(conversation_id, user_id, threshold=60)
        if not groups.get("has_context"):
            return {"action": "missing_context"}
        if re.search(r"删除|删掉|移除|清理|淘汰", message) and re.search(r"低分|不合格|小于\s*60|低于\s*60|没通过|未通过", message):
            return {"action": "confirm_delete_low_scores", **groups}
        if re.search(r"面试|方案|计划", message) and re.search(r"高分|通过|合格|大于\s*60|超过\s*60|60\s*分以上|分数高", message):
            return {"action": "generate_interview", **groups}
        return {"action": None}

    async def _recent_resume_screening_groups(
        self,
        conversation_id: Optional[str],
        user_id: UUID,
        threshold: int = 60,
    ) -> Dict[str, Any]:
        resources = await self._recent_generated_resources("resume", user_id, conversation_id, limit=20)
        candidate_ids = []
        for item in resources:
            if item.get("id") and item["id"] not in candidate_ids:
                candidate_ids.append(item["id"])

        if not candidate_ids:
            return {"has_context": False, "qualified_candidates": [], "low_score_candidates": []}

        result = await self.db.execute(
            select(ResumeEvaluation).where(
                ResumeEvaluation.user_id == user_id,
                ResumeEvaluation.id.in_([UUID(item) for item in candidate_ids]),
            )
        )
        evaluations = list(result.scalars().all())
        order_map = {candidate_id: index for index, candidate_id in enumerate(candidate_ids)}
        evaluations.sort(key=lambda item: order_map.get(str(item.id), 999))

        qualified = []
        low_scores = []
        for evaluation in evaluations:
            item = self._resume_evaluation_to_candidate_dict(evaluation)
            if float(evaluation.total_score or 0) >= threshold:
                qualified.append(item)
            else:
                low_scores.append(item)
        return {
            "has_context": True,
            "threshold": threshold,
            "qualified_candidates": qualified,
            "low_score_candidates": low_scores,
        }

    def _resume_evaluation_to_candidate_dict(self, evaluation: ResumeEvaluation) -> Dict[str, Any]:
        return {
            "id": str(evaluation.id),
            "name": evaluation.candidate_name or evaluation.original_filename,
            "filename": evaluation.original_filename,
            "score": evaluation.total_score or 0,
            "position": evaluation.candidate_position,
            "education": evaluation.education_level,
            "status": evaluation.status.value if evaluation.status else None,
        }

    def _build_resume_screening_followup_response(
        self,
        action: Dict[str, Any],
        route: Optional[str],
        selected_tool: Optional[AgentToolSpec],
    ) -> AgentChatResponse:
        threshold = action.get("threshold") or 60
        qualified = action.get("qualified_candidates") or []
        low_scores = action.get("low_score_candidates") or []

        if action.get("action") == "generate_interview":
            if not qualified:
                return AgentChatResponse(
                    message=f"最近这批评分里没有达到 {threshold} 分及以上的候选人，我不建议直接生成面试计划。你可以重新评分、调整 JD，或指定某个候选人继续生成。",
                    intent="interview_plan",
                    route=route,
                    steps=[
                        self._planning_step(selected_tool, "识别到基于评分结果推进面试计划的请求。"),
                        AgentStep(id="check_scores", title="检查高分候选人", status="failed", detail=f"未找到 {threshold} 分及以上候选人。"),
                    ],
                    suggestions=["重新筛选简历", "指定候选人生成面试计划", "调整评分标准"],
                )
            if len(qualified) == 1:
                candidate = qualified[0]
                return AgentChatResponse(
                    message=f"最近评分结果里，{candidate['name']} 达到 {candidate['score']} 分，符合进入面试的阈值。我已准备好直接生成他的面试计划。",
                    intent="interview_plan",
                    route=route,
                    steps=[
                        self._planning_step(selected_tool, "用户要求为高分候选人生成面试计划。"),
                        AgentStep(id="select_resume", title="锁定高分候选人", status="completed", detail=self._resume_candidate_label(candidate)),
                        AgentStep(id="generate_plan", title="生成面试计划", status="running", detail="正在调用面试计划工具。", tool="generate_interview_plan"),
                    ],
                    artifacts=[
                        AgentArtifact(
                            type="interview_plan_execute",
                            title="直接生成面试计划",
                            content={"resume_evaluation_id": candidate["id"], "candidate_name": candidate["name"]},
                        )
                    ],
                    suggestions=["生成面试计划", "查看该候选人评分", "删除低分候选人"],
                )
            return AgentChatResponse(
                message=(
                    f"最近这批有 {len(qualified)} 位候选人达到 {threshold} 分及以上。为避免一次生成太多方案，"
                    "请先选择要推进的候选人；如果你明确说“全部生成”，我再逐个生成。"
                ),
                intent="interview_plan",
                route=route,
                steps=[
                    self._planning_step(selected_tool, "用户要求为高分候选人生成面试计划，但存在多个候选人。"),
                    AgentStep(id="select_resume", title="选择高分候选人", status="running", detail=f"候选人数量：{len(qualified)}"),
                    AgentStep(id="generate_plan", title="生成面试计划", status="pending", tool="generate_interview_plan"),
                ],
                artifacts=[
                    AgentArtifact(
                        type="interview_plan_request",
                        title="选择高分候选人生成面试计划",
                        content={"requires_resume_evaluation": True, "candidates": qualified},
                    )
                ],
                suggestions=[f"生成{qualified[0]['name']}的面试计划", "全部生成面试计划", "先查看评分详情"],
            )

        if action.get("action") == "confirm_delete_low_scores":
            if not low_scores:
                return AgentChatResponse(
                    message=f"最近这批评分里没有低于 {threshold} 分的候选人，暂时不需要删除低分简历记录。",
                    intent="resume_screening",
                    route="/recruitment/resume-screening",
                    steps=[
                        self._planning_step(self.tool_registry.get("resume_screening"), "识别到低分候选人处理请求。"),
                        AgentStep(id="check_scores", title="检查低分候选人", status="completed", detail="没有低分候选人。"),
                    ],
                    suggestions=["为高分候选人生成面试计划", "继续上传简历", "查看评分详情"],
                )
            names = "、".join(self._resume_candidate_label(item) for item in low_scores[:5])
            return AgentChatResponse(
                message=(
                    f"我找到了 {len(low_scores)} 位低于 {threshold} 分的候选人：{names}。"
                    "为避免误删，我不会自动删除；如果确认删除，请回复“确认删除这些低分候选人”。"
                ),
                intent="resource_delete",
                route="/recruitment/resume-screening",
                steps=[
                    self._planning_step(self.tool_registry.get("resource_delete"), "识别到低分候选人删除建议。"),
                    AgentStep(id="locate_resource", title="定位低分候选人", status="completed", detail=f"匹配到 {len(low_scores)} 位低分候选人。"),
                    AgentStep(id="delete_resource", title="等待确认删除", status="pending", detail="用户确认后再删除。"),
                ],
                artifacts=[
                    AgentArtifact(
                        type="delete_resource_candidates",
                        title="低分候选人待确认",
                        content={"resource_type": "resume", "keyword": "低分候选人", "candidates": low_scores},
                    )
                ],
                suggestions=["确认删除这些低分候选人", "暂不删除，查看评分详情", "为高分候选人生成面试计划"],
            )

        return AgentChatResponse(
            message="我还没有找到最近一轮简历评分结果。请先完成简历评分，再让我继续生成面试计划或处理低分候选人。",
            intent="resume_screening",
            route="/recruitment/resume-screening",
            steps=[self._planning_step(self.tool_registry.get("resume_screening"), "缺少可用于闭环推进的评分上下文。")],
            suggestions=["上传简历评分", "选择 JD 后开始评分", "查看历史评分"],
        )

    async def _handle_jd_edit(
        self,
        message: str,
        user_id: UUID,
        selected_tool: Optional[AgentToolSpec],
        conversation_id: Optional[str],
        memory_context: str,
    ) -> AgentChatResponse:
        edit_request = await self._parse_jd_edit_request(message, memory_context)
        steps = [
            self._planning_step(selected_tool, "识别到 JD 修改请求，先定位目标 JD。"),
            AgentStep(id="locate_jd", title="定位要修改的 JD", status="running", detail="优先查找当前对话最近生成的 JD。", tool="edit_jd"),
            AgentStep(id="collect_changes", title="确认修改要求", status="pending", detail="拿到明确修改点后再改写。"),
            AgentStep(id="rewrite_jd", title="改写 JD 内容", status="pending", detail="基于原 JD 和修改要求生成新版。"),
            AgentStep(id="save_jd", title="保存修改", status="pending", detail="更新原 JD 记录。"),
        ]

        candidates = await self._resolve_recent_jd_candidates(user_id, conversation_id, edit_request.get("keyword") or "")
        if not candidates:
            steps[1] = steps[1].model_copy(update={"status": "failed", "detail": "没有找到可修改的 JD。"})
            return AgentChatResponse(
                message="我还没有找到可以修改的 JD。请先生成或保存一个 JD，或者明确告诉我要修改哪个岗位的 JD。",
                intent="jd_edit",
                route="/recruitment/jd-generator",
                steps=steps,
                artifacts=[],
                suggestions=["生成一个新的 JD", "说明要修改的岗位名称", "去 JD 管理查看职位"],
            )

        if len(candidates) > 1 and edit_request.get("keyword"):
            matches = self._match_delete_resources(candidates, edit_request.get("keyword") or "")
            if matches:
                candidates = matches

        if len(candidates) > 1:
            steps[1] = steps[1].model_copy(update={"status": "running", "detail": f"找到 {len(candidates)} 个可能的 JD，需要确认。"})
            return AgentChatResponse(
                message=(
                    "我找到了多个可能要修改的 JD。为避免改错，请告诉我具体是哪一个：\n"
                    + "\n".join(f"- {self._format_jd_candidate_label(item)}" for item in candidates[:5])
                ),
                intent="jd_edit",
                route="/recruitment/jd-generator",
                steps=steps,
                artifacts=[
                    AgentArtifact(
                        type="jd_edit_candidates",
                        title="待确认 JD",
                        content={"candidates": candidates[:5]},
                    )
                ],
                suggestions=["修改最新的那个 JD", "说明岗位名称", "去 JD 管理查看职位"],
            )

        target = candidates[0]
        steps[1] = steps[1].model_copy(update={"status": "completed", "detail": f"已定位：{target.get('name') or '最近的 JD'}。"})

        edit_instructions = self._clean_optional_value(edit_request.get("changes"))
        if not edit_instructions:
            steps[2] = steps[2].model_copy(update={"status": "pending", "detail": "等待用户说明要修改哪些内容。"})
            return AgentChatResponse(
                message=f"可以，我找到了「{target.get('name') or '最近的 JD'}」。你想具体改哪些内容？比如薪资、地点、职责、技能要求或福利。",
                intent="jd_edit",
                route="/recruitment/jd-generator",
                steps=steps,
                artifacts=[
                    AgentArtifact(
                        type="jd_edit_request",
                        title="等待修改要求",
                        content={"job_description_id": target.get("id"), "title": target.get("name")},
                    )
                ],
                suggestions=["薪资改成 20-30K", "增加 React 和性能优化要求", "把工作地点改成长沙"],
            )

        steps[2] = steps[2].model_copy(update={"status": "completed", "detail": edit_instructions})
        try:
            service = JobDescriptionService(self.db)
            original_jd = await service.get_job_description(str(target["id"]), user_id)
            original_data = original_jd.model_dump(mode="json")
            original_content = original_data.get("content") or ""
            steps[3] = steps[3].model_copy(update={"status": "running", "detail": "正在生成修改后的 JD。"})
            updated_content = await self._rewrite_jd_content(original_content, edit_instructions)
            steps[3] = steps[3].model_copy(update={"status": "completed", "detail": "已生成修改后的 JD。"})

            update_data = self._build_jd_update_payload(original_data, updated_content, edit_instructions, edit_request)
            steps[4] = steps[4].model_copy(update={"status": "running", "detail": "正在保存到原 JD。"})
            updated_jd = await service.update_job_description(str(target["id"]), update_data, user_id)
            steps[4] = steps[4].model_copy(update={"status": "completed", "detail": f"已更新 JD：{updated_jd.id}"})
        except Exception as exc:
            logger.warning("Agent 修改 JD 失败: %s", exc, exc_info=True)
            steps[4] = steps[4].model_copy(update={"status": "failed", "detail": str(exc)})
            return AgentChatResponse(
                message=f"我找到了 JD，但保存修改时失败了：{str(exc)}",
                intent="jd_edit",
                route="/recruitment/jd-generator",
                steps=steps,
                artifacts=[],
                suggestions=["稍后重试", "去 JD 管理手动编辑", "重新生成 JD"],
            )

        return AgentChatResponse(
            message=f"已按你的要求更新「{updated_jd.title}」。",
            intent="jd_edit",
            route="/recruitment/jd-generator",
            steps=steps,
            artifacts=[
                AgentArtifact(
                    type="job_description",
                    title=f"{updated_jd.title} JD",
                    content=updated_jd.content,
                    metadata={
                        "job_title": updated_jd.title,
                        "saved_jd_id": str(updated_jd.id),
                        "edit_instructions": edit_instructions,
                        "route": "/recruitment/jd-generator",
                    },
                )
            ],
            suggestions=["继续修改 JD", "基于这个 JD 筛选简历", "去 JD 管理查看职位"],
        )

    async def _handle_criteria_edit(
        self,
        message: str,
        user_id: UUID,
        selected_tool: Optional[AgentToolSpec],
        conversation_id: Optional[str],
        memory_context: str,
    ) -> AgentChatResponse:
        final_response = None
        async for event in self._stream_criteria_edit_response(message, user_id, conversation_id, memory_context):
            if event.get("type") == "final" and event.get("response"):
                final_response = AgentChatResponse.model_validate(event["response"])
        if final_response:
            return final_response
        return AgentChatResponse(
            message="评分标准修改没有返回结果，请稍后重试。",
            intent="criteria_edit",
            route="/recruitment/resume-screening",
            steps=[self._planning_step(selected_tool, "评分标准修改流程未返回最终结果。")],
            suggestions=["稍后重试", "重新生成评分标准"],
        )

    async def _parse_jd_edit_request(self, message: str, memory_context: str = "") -> Dict[str, Any]:
        prompt = (
            "你是 HR Agent 的 JD 修改请求解析器。请严格返回 JSON，不要解释。\n"
            "字段：keyword, changes, title, department, location, salary_range, experience_level, education, job_type, skills。\n"
            "keyword 填用户用于定位 JD 的岗位名、地点、薪资、ID短码或序号，例如“前端”“AI产品经理”“长沙”“15K”“第一个”；没有则返回 null。\n"
            "changes 填用户明确提出的修改要求；如果用户只是说“帮我改改上次那个JD”但没有说明改什么，返回 null。\n"
            "如果用户明确要求修改薪资、地点、经验、学历、职位名称、部门、工作类型或技能，请同步填入对应结构化字段；未提到返回 null，skills 未提到返回 null。\n"
            "如果上一轮助手已经询问“你想具体改哪些内容”，当前用户消息通常就是 changes。\n"
            f"{self._format_memory_for_prompt(memory_context)}"
            f"当前用户消息：{message}\n"
            "返回格式：{\"keyword\":null,\"changes\":null,\"title\":null,\"department\":null,\"location\":null,\"salary_range\":null,\"experience_level\":null,\"education\":null,\"job_type\":null,\"skills\":null}"
        )
        try:
            if self.llm_service is None:
                self.llm_service = LLMService()
            response = await self.llm_service.generate_response(prompt)
            parsed = self._safe_json_loads(response)
            changes = self._clean_optional_value(parsed.get("changes"))
            if changes and self._is_generic_jd_edit_request(changes):
                changes = None
            if not changes and self._is_jd_edit_followup(message, memory_context):
                changes = message.strip()
            return {
                "keyword": self._clean_optional_value(parsed.get("keyword")),
                "changes": changes,
                "title": self._clean_optional_value(parsed.get("title")),
                "department": self._clean_optional_value(parsed.get("department")),
                "location": self._clean_optional_value(parsed.get("location")),
                "salary_range": self._clean_optional_value(parsed.get("salary_range")),
                "experience_level": self._clean_optional_value(parsed.get("experience_level")),
                "education": self._clean_optional_value(parsed.get("education")),
                "job_type": self._clean_optional_value(parsed.get("job_type")),
                "skills": self._normalize_optional_list(parsed.get("skills")),
                "source": "llm",
            }
        except Exception as exc:
            logger.warning("JD 修改请求解析失败，使用规则兜底: %s", exc)
        return self._fallback_parse_jd_edit_request(message, memory_context)

    def _fallback_parse_jd_edit_request(self, message: str, memory_context: str = "") -> Dict[str, Any]:
        keyword = None
        title_match = re.search(r"([\u4e00-\u9fa5A-Za-z0-9+#]+(?:开发|测试|算法|产品|运营|销售|人事|财务|行政)?(?:工程师|经理|专员|主管|顾问|设计师|架构师))", message)
        if title_match:
            keyword = title_match.group(1)
        if not keyword:
            ordinal_match = re.search(r"(第[一二三四五六七八九十1-9]\s*个|第[一二三四五六七八九十1-9]\s*条|最新|最近|上一个|最后一个)", message)
            if ordinal_match:
                keyword = ordinal_match.group(1)
        if not keyword:
            for city in ["北京", "上海", "深圳", "广州", "杭州", "南京", "成都", "重庆", "苏州", "武汉", "西安", "长沙", "济南"]:
                if city in message:
                    keyword = city
                    break
        if not keyword:
            salary_match = re.search(r"\d+(?:\.\d+)?\s*[kK万]?", message)
            if salary_match:
                keyword = salary_match.group(0).replace(" ", "")
        changes = re.sub(r"请|帮我|麻烦|一下|上次|刚才|刚刚|这个|这份|这条|那个|JD|jd|职位描述|岗位说明书", "", message)
        changes = re.sub(r"改改|修改|调整|优化|更新|编辑|润色", "", changes).strip(" ，,。")
        if changes and self._is_generic_jd_edit_request(changes):
            changes = ""
        if not changes and self._is_jd_edit_followup(message, memory_context):
            changes = message.strip()
        return {
            "keyword": keyword,
            "changes": changes or None,
            **self._fallback_parse_jd_edit_fields(message),
            "source": "fallback",
        }

    async def _parse_criteria_edit_request(self, message: str, memory_context: str = "") -> Dict[str, Any]:
        prompt = (
            "你是 HR Agent 的评分标准修改请求解析器。请严格返回 JSON，不要解释。\n"
            "字段：keyword, changes。\n"
            "keyword 填用户用于定位评分标准的岗位名或关键词，例如“前端”“AI产品经理”；没有则返回 null。\n"
            "changes 填用户明确提出的评分标准修改要求；如果用户只是说“帮我改改评分标准”但没有说明改什么，返回 null。\n"
            "如果上一轮助手已经询问“你想具体改哪些评分规则”，当前用户消息通常就是 changes。\n"
            f"{self._format_memory_for_prompt(memory_context)}"
            f"当前用户消息：{message}\n"
            "返回格式：{\"keyword\":null,\"changes\":null}"
        )
        try:
            if self.llm_service is None:
                self.llm_service = LLMService()
            response = await self.llm_service.generate_response(prompt)
            parsed = self._safe_json_loads(response)
            changes = self._clean_optional_value(parsed.get("changes"))
            if changes and self._is_generic_criteria_edit_request(changes):
                changes = None
            if not changes and self._is_criteria_edit_followup(message, memory_context):
                changes = message.strip()
            return {
                "keyword": self._clean_optional_value(parsed.get("keyword")),
                "changes": changes,
                "source": "llm",
            }
        except Exception as exc:
            logger.warning("评分标准修改请求解析失败，使用规则兜底: %s", exc)
        return self._fallback_parse_criteria_edit_request(message, memory_context)

    def _fallback_parse_criteria_edit_request(self, message: str, memory_context: str = "") -> Dict[str, Any]:
        keyword = None
        title_match = re.search(r"([\u4e00-\u9fa5A-Za-z0-9+#]+(?:开发|测试|算法|产品|运营|销售|人事|财务|行政)?(?:工程师|经理|专员|主管|顾问|设计师|架构师))", message)
        if title_match:
            keyword = title_match.group(1)
        changes = re.sub(r"请|帮我|麻烦|一下|上次|刚才|刚刚|这个|这份|这条|那个|评分标准|评分规则|打分标准|筛选标准|简历评分", "", message)
        changes = re.sub(r"改改|修改|调整|优化|更新|编辑|润色", "", changes).strip(" ，,。")
        if changes and self._is_generic_criteria_edit_request(changes):
            changes = ""
        if not changes and self._is_criteria_edit_followup(message, memory_context):
            changes = message.strip()
        return {
            "keyword": keyword,
            "changes": changes or None,
            "source": "fallback",
        }

    def _fallback_parse_jd_edit_fields(self, message: str) -> Dict[str, Any]:
        salary_match = re.search(r"(\d+(?:\.\d+)?\s*[kK万]?)(?:\s*[-~到至]\s*(\d+(?:\.\d+)?\s*[kK万]?))?", message)
        salary = None
        if salary_match and re.search(r"薪资|工资|月薪|待遇|年薪|薪酬|package", message, re.I):
            salary = salary_match.group(0).replace(" ", "")
        location = None
        for city in ["北京", "上海", "深圳", "广州", "杭州", "南京", "成都", "重庆", "苏州", "武汉", "西安", "长沙", "济南"]:
            if city in message and re.search(r"地点|城市|工作地|base|改成|改为|换成", message, re.I):
                location = city
                break
        experience = None
        experience_match = re.search(r"经验(?:改成|改为|换成)?\s*(不限|\d+\s*-\s*\d+年|\d+年以上|\d+年\+?)|工作经验(?:改成|改为|换成)?\s*(不限|\d+\s*-\s*\d+年|\d+年以上|\d+年\+?)", message)
        if experience_match:
            experience = next((group for group in experience_match.groups() if group), experience_match.group(0))
        education = None
        education_match = re.search(r"学历(?:改成|改为|换成)?\s*(不限|大专|专科|本科|硕士|博士)|(?:不限|大专|专科|本科|硕士|博士)(?:及以上)?", message)
        if education_match and re.search(r"学历|大专|专科|本科|硕士|博士", message):
            education = education_match.group(1) or education_match.group(0)
        skill_candidates = ["Java", "Python", "Go", "Vue", "React", "TypeScript", "JavaScript", "Spring", "MySQL", "Redis", "Docker", "Kubernetes", "AI", "大模型"]
        skills = [skill for skill in skill_candidates if skill.lower() in message.lower()]
        return {
            "title": None,
            "department": None,
            "location": location,
            "salary_range": salary,
            "experience_level": experience,
            "education": education,
            "job_type": None,
            "skills": skills or None,
        }

    async def _resolve_recent_jd_candidates(
        self,
        user_id: UUID,
        conversation_id: Optional[str],
        keyword: str = "",
    ) -> List[Dict[str, Any]]:
        pending_target = await self._recent_pending_jd_edit_target(user_id, conversation_id)
        if pending_target and not keyword:
            return [pending_target]

        context_matches = await self._recent_generated_resources("jd", user_id, conversation_id, limit=20)
        pending_candidates = await self._recent_pending_jd_edit_candidates(user_id, conversation_id)
        if pending_candidates and keyword:
            selected = self._match_jd_selection_candidates(pending_candidates, keyword)
            if selected:
                return selected

        resources = await self._list_deletable_resources("jd", user_id, keyword)
        if not resources:
            if keyword and context_matches:
                return self._match_delete_resources(context_matches, keyword)
            return context_matches[:1] if context_matches else []
        if keyword:
            matched = [
                *self._match_delete_resources(context_matches, keyword),
                *self._match_delete_resources(resources, keyword),
            ]
            matched = self._dedupe_candidates_by_id(matched)
            return matched or resources[:5]
        if context_matches:
            return context_matches[:1]
        return resources[:1]

    def _dedupe_candidates_by_id(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        by_key: Dict[str, Dict[str, Any]] = {}
        ordered_keys: List[str] = []
        for item in candidates:
            item_id = item.get("id")
            key = str(item_id) if item_id else self._normalize_candidate_match_text(item.get("name"))
            if not key:
                continue
            if key not in by_key:
                ordered_keys.append(key)
                by_key[key] = item
                continue
            by_key[key] = self._merge_candidate_details(by_key[key], item)
        return [by_key[key] for key in ordered_keys]

    def _merge_candidate_details(self, primary: Dict[str, Any], secondary: Dict[str, Any]) -> Dict[str, Any]:
        merged = {**primary}
        for key, value in secondary.items():
            if value and not merged.get(key):
                merged[key] = value
        if self._candidate_detail_score(secondary) > self._candidate_detail_score(primary):
            merged = {**secondary, **merged}
        return merged

    def _candidate_detail_score(self, item: Dict[str, Any]) -> int:
        return sum(1 for key in ["location", "salary_range", "salary", "experience_level", "updated_at"] if item.get(key))

    async def _recent_pending_jd_edit_candidates(
        self,
        user_id: UUID,
        conversation_id: Optional[str],
        limit: int = 8,
    ) -> List[Dict[str, Any]]:
        conditions = [
            Conversation.user_id == user_id,
            Message.role == MessageRole.ASSISTANT,
        ]
        if conversation_id:
            try:
                conditions.append(Message.conversation_id == UUID(str(conversation_id)))
            except Exception:
                logger.warning("JD 候选上下文收到非法 conversation_id: %s", conversation_id)
                return []
        query = (
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(*conditions)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(query)
        for message in result.scalars().all():
            response = (message.context or {}).get("agent_response") if isinstance(message.context, dict) else None
            if not isinstance(response, dict):
                continue
            for artifact in response.get("artifacts") or []:
                if artifact.get("type") != "jd_edit_candidates":
                    continue
                content = artifact.get("content") if isinstance(artifact.get("content"), dict) else {}
                candidates = content.get("candidates") if isinstance(content.get("candidates"), list) else []
                return [item for item in candidates if isinstance(item, dict) and item.get("id")]
        return []

    def _match_jd_selection_candidates(
        self,
        candidates: List[Dict[str, Any]],
        keyword: str,
    ) -> List[Dict[str, Any]]:
        ordinal = self._parse_selection_ordinal(keyword)
        if ordinal is not None and 0 <= ordinal < len(candidates):
            return [candidates[ordinal]]
        if re.search(r"最新|最近|上一个|最后", keyword):
            return candidates[:1]
        return self._match_delete_resources(candidates, keyword)

    def _parse_selection_ordinal(self, text: str) -> Optional[int]:
        match = re.search(r"第\s*([一二三四五六七八九十1-9])\s*(?:个|条)?", text)
        if not match:
            return None
        value = match.group(1)
        chinese_numbers = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        number = int(value) if value.isdigit() else chinese_numbers.get(value)
        return number - 1 if number else None

    def _format_jd_candidate_label(self, item: Dict[str, Any]) -> str:
        name = item.get("name") or "未命名 JD"
        details = [
            item.get("location"),
            item.get("salary_range") or item.get("salary"),
            item.get("experience_level"),
        ]
        if item.get("updated_at"):
            details.append(f"更新于 {str(item['updated_at'])[:10]}")
        if item.get("id"):
            details.append(f"ID {str(item['id'])[:8]}")
        detail_text = " / ".join(str(value) for value in details if value)
        return f"{name}（{detail_text}）" if detail_text else name

    async def _recent_pending_criteria_edit_target(
        self,
        user_id: UUID,
        conversation_id: Optional[str],
        limit: int = 8,
    ) -> Optional[Dict[str, Any]]:
        conditions = [
            Conversation.user_id == user_id,
            Message.role == MessageRole.ASSISTANT,
        ]
        if conversation_id:
            try:
                conditions.append(Message.conversation_id == UUID(str(conversation_id)))
            except Exception:
                logger.warning("评分标准修改上下文收到非法 conversation_id: %s", conversation_id)
                return None
        query = (
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(*conditions)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(query)
        for message in result.scalars().all():
            response = (message.context or {}).get("agent_response") if isinstance(message.context, dict) else None
            if not isinstance(response, dict):
                continue
            for artifact in response.get("artifacts") or []:
                if artifact.get("type") != "criteria_edit_request":
                    continue
                content = artifact.get("content") if isinstance(artifact.get("content"), dict) else {}
                criteria_id = content.get("criteria_id")
                if not criteria_id:
                    continue
                title = content.get("title") or artifact.get("title") or "待修改的评分标准"
                return {
                    "id": str(criteria_id),
                    "name": title,
                    "search_text": title,
                    "source": "pending_criteria_edit_context",
                }
        return None

    async def _recent_pending_jd_edit_target(
        self,
        user_id: UUID,
        conversation_id: Optional[str],
        limit: int = 8,
    ) -> Optional[Dict[str, Any]]:
        conditions = [
            Conversation.user_id == user_id,
            Message.role == MessageRole.ASSISTANT,
        ]
        if conversation_id:
            try:
                conditions.append(Message.conversation_id == UUID(str(conversation_id)))
            except Exception:
                logger.warning("JD 修改上下文收到非法 conversation_id: %s", conversation_id)
                return None
        query = (
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(*conditions)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(query)
        for message in result.scalars().all():
            response = (message.context or {}).get("agent_response") if isinstance(message.context, dict) else None
            if not isinstance(response, dict):
                continue
            for artifact in response.get("artifacts") or []:
                if artifact.get("type") != "jd_edit_request":
                    continue
                content = artifact.get("content") if isinstance(artifact.get("content"), dict) else {}
                jd_id = content.get("job_description_id")
                if not jd_id:
                    continue
                title = content.get("title") or artifact.get("title") or "待修改的 JD"
                return {
                    "id": str(jd_id),
                    "name": title,
                    "search_text": title,
                    "source": "pending_jd_edit_context",
                }
        return None

    async def _resolve_recent_criteria_candidates(
        self,
        user_id: UUID,
        conversation_id: Optional[str],
        keyword: str = "",
    ) -> List[Dict[str, Any]]:
        pending_target = await self._recent_pending_criteria_edit_target(user_id, conversation_id)
        if pending_target and not keyword:
            return [pending_target]

        context_matches = await self._recent_generated_resources("criteria", user_id, conversation_id, limit=20)
        if context_matches:
            if keyword:
                matched = self._match_delete_resources(context_matches, keyword)
                if matched:
                    return matched
            else:
                return context_matches[:1]

        try:
            result = await ScoringCriteriaService(self.db).get_scoring_criteria_list(user_id=user_id, page=1, size=100)
            resources = [
                {
                    "id": str(item.id),
                    "name": item.title or item.job_title or "未命名评分标准",
                    "search_text": self._resource_search_text({
                        "title": item.title,
                        "job_title": item.job_title,
                        "content": item.content,
                    }),
                    "job_description_id": str(item.job_description_id) if item.job_description_id else None,
                    "created_at": str(item.created_at) if item.created_at else "",
                    "updated_at": str(item.updated_at) if item.updated_at else "",
                }
                for item in result.items
            ]
        except Exception as exc:
            logger.warning("读取评分标准列表失败: %s", exc)
            resources = []
        if not resources:
            return []
        if keyword:
            matched = self._match_delete_resources(resources, keyword)
            return self._collapse_criteria_candidates(matched) if matched else resources[:1]
        return resources[:1]

    def _collapse_criteria_candidates(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if len(candidates) <= 1:
            return candidates
        sorted_candidates = sorted(
            candidates,
            key=lambda item: item.get("updated_at") or item.get("created_at") or "",
            reverse=True,
        )
        by_jd: Dict[str, Dict[str, Any]] = {}
        without_jd: List[Dict[str, Any]] = []
        for item in sorted_candidates:
            jd_id = item.get("job_description_id")
            if jd_id:
                by_jd.setdefault(str(jd_id), item)
            else:
                without_jd.append(item)
        collapsed = [*by_jd.values(), *without_jd]
        if len(collapsed) > 1:
            normalized_names = {self._normalize_candidate_match_text(item.get("name")) for item in collapsed}
            if len(normalized_names) == 1:
                return [collapsed[0]]
        return collapsed

    async def _rewrite_jd_content(self, original_content: str, edit_instructions: str) -> str:
        prompt = (
            "你是专业招聘 JD 编辑器。请在保留原 JD 结构和没有被要求修改的信息的前提下，"
            "只根据用户修改要求输出修改后的完整 JD 正文。\n"
            "不要输出解释、差异说明或 Markdown 代码块。\n\n"
            f"原 JD：\n{original_content}\n\n"
            f"用户修改要求：\n{edit_instructions}"
        )
        if self.llm_service is None:
            self.llm_service = LLMService()
        response = await self.llm_service.generate_response(prompt)
        updated = str(response or "").strip()
        if not updated:
            raise ValueError("大模型没有返回修改后的 JD")
        return updated

    async def _rewrite_criteria_content(self, original_content: str, edit_instructions: str) -> str:
        prompt = (
            "你是专业简历评分标准编辑器。请在保留原评分标准结构和未被要求修改的信息的前提下，"
            "只根据用户修改要求输出修改后的完整评分标准正文。\n"
            "如果涉及分值调整，请保持总分口径清晰，并同步调整相关维度描述，避免分值前后矛盾。\n"
            "不要输出解释、差异说明或 Markdown 代码块。\n\n"
            f"原评分标准：\n{original_content}\n\n"
            f"用户修改要求：\n{edit_instructions}"
        )
        if self.llm_service is None:
            self.llm_service = LLMService()
        response = await self.llm_service.generate_response(prompt)
        updated = str(response or "").strip()
        if not updated:
            raise ValueError("大模型没有返回修改后的评分标准")
        return updated

    def _build_jd_update_payload(
        self,
        original_data: Dict[str, Any],
        updated_content: str,
        edit_instructions: str,
        edit_request: Optional[Dict[str, Any]] = None,
    ) -> JobDescriptionUpdate:
        edit_request = edit_request or {}
        meta_data = original_data.get("meta_data") if isinstance(original_data.get("meta_data"), dict) else {}
        edit_history = list(meta_data.get("edit_history") or [])
        edit_history.append({"source": "hr_agent", "instruction": edit_instructions})
        meta_data = {**meta_data, "last_edit_source": "hr_agent", "edit_history": edit_history[-10:]}
        if edit_request.get("salary_range"):
            meta_data["salary"] = edit_request["salary_range"]
        if edit_request.get("location"):
            meta_data["location"] = edit_request["location"]
        return JobDescriptionUpdate(
            title=edit_request.get("title") or original_data.get("title"),
            department=edit_request.get("department") or original_data.get("department"),
            location=edit_request.get("location") or original_data.get("location"),
            salary_range=edit_request.get("salary_range") or original_data.get("salary_range"),
            experience_level=edit_request.get("experience_level") or original_data.get("experience_level"),
            education=edit_request.get("education") or original_data.get("education"),
            job_type=edit_request.get("job_type") or original_data.get("job_type"),
            skills=edit_request.get("skills") or original_data.get("skills"),
            content=updated_content,
            requirements=original_data.get("requirements"),
            status=original_data.get("status"),
            meta_data=meta_data,
        )

    def _build_criteria_update_payload(
        self,
        original_data: Dict[str, Any],
        updated_content: str,
        edit_instructions: str,
    ) -> ScoringCriteriaUpdate:
        meta_data = original_data.get("meta_data") if isinstance(original_data.get("meta_data"), dict) else {}
        edit_history = list(meta_data.get("edit_history") or [])
        edit_history.append({"source": "hr_agent", "instruction": edit_instructions})
        meta_data = {**meta_data, "last_edit_source": "hr_agent", "edit_history": edit_history[-10:]}
        return ScoringCriteriaUpdate(
            title=original_data.get("title"),
            job_title=original_data.get("job_title"),
            content=updated_content,
            criteria_data=original_data.get("criteria_data"),
            total_score=original_data.get("total_score"),
            scoring_dimensions=original_data.get("scoring_dimensions"),
            status=original_data.get("status"),
            meta_data=meta_data,
            job_description_id=original_data.get("job_description_id"),
        )

    async def _resolve_interview_exam_followup(
        self,
        message: str,
        user_id: UUID,
        conversation_id: Optional[str],
    ) -> Dict[str, Any]:
        if not self._is_followup_interview_exam_request(message):
            return {"matched": False}
        matches = await self._recent_generated_resources("interview", user_id, conversation_id, limit=20)
        if not matches:
            return {"matched": False}
        latest = matches[0]
        content = latest.get("content") or ""
        return {
            "matched": True,
            "plan_id": latest.get("id"),
            "title": latest.get("name") or "最近生成的面试方案",
            "content": content,
            "message": f"{message}\n\n当前面试方案上下文：\n{content[:2500]}",
        }

    async def _handle_resource_delete(
        self,
        message: str,
        user_id: UUID,
        selected_tool: Optional[AgentToolSpec],
        conversation_id: Optional[str] = None,
    ) -> AgentChatResponse:
        delete_request = await self._parse_delete_request(message)
        resource_type = delete_request.get("type")
        keyword = delete_request.get("keyword") or ""
        if (
            resource_type == "resume"
            and not delete_request.get("confirm_context_low_scores")
            and re.search(r"低分|不合格|小于\s*60|低于\s*60|没通过|未通过", message)
        ):
            followup_action = await self._resolve_resume_screening_followup_action(message, user_id, conversation_id)
            if followup_action.get("action") == "confirm_delete_low_scores":
                return self._build_resume_screening_followup_response(
                    followup_action,
                    "/recruitment/resume-screening",
                    selected_tool,
                )
        if resource_type == "resume" and delete_request.get("confirm_context_low_scores"):
            low_score_matches = await self._recent_low_score_resume_resources(user_id, conversation_id)
            if not low_score_matches:
                return AgentChatResponse(
                    message="我没有在当前对话最近的评分结果里找到低分候选人，因此没有执行删除。你可以先完成简历评分，或明确说出候选人姓名。",
                    intent="resource_delete",
                    route="/recruitment/resume-screening",
                    steps=[
                        self._planning_step(selected_tool, "识别到确认删除低分候选人的请求。"),
                        AgentStep(id="locate_resource", title="定位低分候选人", status="failed", detail="没有找到可删除的低分候选人。"),
                    ],
                    suggestions=["先评分简历", "说出候选人姓名", "查看简历筛选列表"],
                )
            steps = [
                self._planning_step(selected_tool, "用户确认删除最近评分中的低分候选人。"),
                AgentStep(id="locate_resource", title="定位低分候选人", status="completed", detail=f"匹配到 {len(low_score_matches)} 位低分候选人。"),
                AgentStep(id="delete_resource", title="执行删除操作", status="running", detail="正在删除低分简历记录。"),
            ]
            return await self._delete_matched_resources(
                resource_type="resume",
                matches=low_score_matches,
                user_id=user_id,
                steps=steps,
                keyword="低分候选人",
                matched_from_context=True,
            )
        context_first = bool(delete_request.get("context_reference")) and not keyword
        steps = [
            self._planning_step(selected_tool, "识别到删除类请求，准备定位目标资源。"),
            AgentStep(
                id="locate_resource",
                title="定位删除目标",
                status="running",
                detail=self._delete_target_detail(resource_type, keyword),
                tool="delete_resource",
            ),
            AgentStep(id="delete_resource", title="执行删除操作", status="pending", detail="匹配到唯一目标后执行删除。"),
        ]
        if not resource_type:
            steps[1] = steps[1].model_copy(update={"status": "failed", "detail": "缺少要删除的资源类型。"})
            return AgentChatResponse(
                message="我还不确定你要删除哪类内容。请明确说明要删除 JD、简历记录、面试方案还是试卷，例如：帮我删除产品经理的 JD。",
                intent="resource_delete",
                route=None,
                steps=steps,
                artifacts=[],
                suggestions=["删除产品经理的 JD", "删除张三的简历记录", "删除 Java 试卷"],
            )

        if context_first:
            context_matches = await self._recent_generated_resources(resource_type, user_id, conversation_id)
            if context_matches:
                matches = context_matches if delete_request.get("delete_all") else context_matches[:1]
                return await self._delete_matched_resources(
                    resource_type=resource_type,
                    matches=matches,
                    user_id=user_id,
                    steps=steps,
                    keyword=keyword,
                    matched_from_context=True,
                )

        resources = await self._list_deletable_resources(resource_type, user_id, keyword)
        matches = self._match_delete_resources(resources, keyword, delete_request.get("latest"))
        if not matches:
            steps[1] = steps[1].model_copy(update={"status": "failed", "detail": "没有找到匹配资源。"})
            return AgentChatResponse(
                message=f"没有找到匹配的{self._resource_type_label(resource_type)}。你可以换个名称再试，或先到对应列表确认名称。",
                intent="resource_delete",
                route=self._resource_type_route(resource_type),
                steps=steps,
                artifacts=[
                    AgentArtifact(
                        type="delete_resource_result",
                        title="删除结果",
                        content={"resource_type": resource_type, "keyword": keyword, "deleted": [], "matches": []},
                    )
                ],
                suggestions=self._delete_suggestions(resource_type),
            )

        if len(matches) > 1 and not delete_request.get("delete_all"):
            steps[1] = steps[1].model_copy(update={"status": "running", "detail": f"找到 {len(matches)} 个匹配项，需要用户确认。"})
            return AgentChatResponse(
                message=(
                    f"我找到了 {len(matches)} 个匹配的{self._resource_type_label(resource_type)}，为避免误删，请说得更具体一点：\n"
                    + "\n".join(f"- {item['name']}" for item in matches[:8])
                ),
                intent="resource_delete",
                route=self._resource_type_route(resource_type),
                steps=steps,
                artifacts=[
                    AgentArtifact(
                        type="delete_resource_candidates",
                        title="待确认删除目标",
                        content={"resource_type": resource_type, "keyword": keyword, "candidates": matches[:8]},
                    )
                ],
                suggestions=["补充更完整的名称", "说“删除最新的那个”", "到列表中确认后再删除"],
            )

        return await self._delete_matched_resources(
            resource_type=resource_type,
            matches=matches,
            user_id=user_id,
            steps=steps,
            keyword=keyword,
            matched_from_context=False,
        )

    async def _parse_delete_request(self, message: str) -> Dict[str, Any]:
        prompt = (
            "你是 HR Agent 的删除请求解析器。请从用户消息中识别要删除的招聘资源，并严格返回 JSON，不要解释。\n"
            "resource_type 只能是 jd、resume、interview、exam、unknown。\n"
            "含义：\n"
            "- jd：JD、岗位、职位、职位描述、招聘需求、岗位说明书。\n"
            "- resume：简历、候选人、简历评分/筛选记录。\n"
            "- interview：面试方案、面试计划、面试安排。\n"
            "- exam：试卷、考试、笔试题、测评题。\n"
            "keyword 填用户用于定位目标的名称/关键词，例如“AI训练师”“产品经理”“张三”“Java基础”。不要包含“删除、帮我、JD、岗位、试卷”等动作词或类型词。\n"
            "context_reference 表示用户是否用“这个/这份/这条/该/刚才/刚生成的/上一个/它”等指代最近生成的产物。\n"
            "latest 为用户是否表达最新/刚才/上一个/最近。\n"
            "delete_all 为用户是否明确表达全部/所有/都删。\n"
            "confirm_context_low_scores 表示用户是否在上文低分候选人确认后，明确说“确认删除这些低分候选人/确认删除不合格简历”。\n"
            "如果资源类型不明确，resource_type 返回 unknown。\n"
            "返回格式：{\"resource_type\":\"jd|resume|interview|exam|unknown\",\"keyword\":\"...\",\"context_reference\":false,\"latest\":false,\"delete_all\":false,\"confirm_context_low_scores\":false}\n\n"
            f"用户消息：{message}"
        )
        try:
            if self.llm_service is None:
                self.llm_service = LLMService()
            response = await self.llm_service.generate_response(prompt)
            parsed = self._safe_json_loads(response)
            resource_type = str(parsed.get("resource_type") or parsed.get("type") or "").strip().lower()
            if resource_type in {"jd", "resume", "interview", "exam"}:
                keyword = self._clean_optional_value(parsed.get("keyword")) or ""
                return {
                    "type": resource_type,
                    "keyword": keyword,
                    "context_reference": bool(parsed.get("context_reference")),
                    "latest": bool(parsed.get("latest")),
                    "delete_all": bool(parsed.get("delete_all")),
                    "confirm_context_low_scores": bool(parsed.get("confirm_context_low_scores")),
                    "source": "llm",
                }
        except Exception as exc:
            logger.warning("删除请求大模型解析失败，使用规则兜底: %s", exc)

        return self._fallback_parse_delete_request(message)

    def _fallback_parse_delete_request(self, message: str) -> Dict[str, Any]:
        lowered = message.lower()
        resource_type = None
        if re.search(r"jd|职位描述|岗位说明书|招聘需求|职位|岗位", lowered):
            resource_type = "jd"
        elif re.search(r"面试方案|面试计划|面试", lowered):
            resource_type = "interview"
        elif re.search(r"试卷|考试|笔试", lowered):
            resource_type = "exam"
        elif re.search(r"简历|候选人", lowered):
            resource_type = "resume"

        keyword = re.sub(r"请|帮我|麻烦|一下|这个|这份|这条|记录|生成的|已生成的", "", message, flags=re.I)
        keyword = re.sub(r"删除|删掉|移除|清理|取消", "", keyword, flags=re.I)
        keyword = re.sub(r"jd|职位描述|岗位说明书|招聘需求|职位|岗位|简历评分|简历记录|简历|候选人|面试方案|面试计划|面试|试卷|考试|笔试", "", keyword, flags=re.I)
        keyword = keyword.strip(" 的：:，,。.?？!！「」『』【】[]()（）")
        return {
            "type": resource_type,
            "keyword": keyword,
            "context_reference": bool(re.search(r"这个|这份|这条|该|刚才|刚刚|上一个|上一条|最近|最新|它|其|刚生成", message)),
            "latest": bool(re.search(r"最近|最新|刚才|上一个|最后", message)),
            "delete_all": bool(re.search(r"全部|所有|都删|全删", message)),
            "confirm_context_low_scores": bool(re.search(r"确认|确定", message) and re.search(r"低分|不合格|没通过|未通过|这些", message)),
            "source": "fallback",
        }

    async def _recent_low_score_resume_resources(
        self,
        user_id: UUID,
        conversation_id: Optional[str],
        threshold: int = 60,
    ) -> List[Dict[str, Any]]:
        groups = await self._recent_resume_screening_groups(conversation_id, user_id, threshold)
        return [
            {
                "id": item["id"],
                "name": item.get("name") or item.get("filename") or "低分候选人",
                "search_text": self._resource_search_text(item, ["name", "filename", "position"]),
                "source": "conversation_context",
            }
            for item in groups.get("low_score_candidates") or []
            if item.get("id")
        ]

    def _delete_target_detail(self, resource_type: Optional[str], keyword: str) -> str:
        label = self._resource_type_label(resource_type) if resource_type else "资源"
        return f"目标类型：{label}；关键词：{keyword or '未提供'}。"

    async def _recent_generated_resources(
        self,
        resource_type: str,
        user_id: UUID,
        conversation_id: Optional[str] = None,
        limit: int = 12,
    ) -> List[Dict[str, Any]]:
        conditions = [
            Conversation.user_id == user_id,
            Message.role == MessageRole.ASSISTANT,
        ]
        if conversation_id:
            try:
                conditions.append(Message.conversation_id == UUID(str(conversation_id)))
            except Exception:
                logger.warning("删除指代匹配收到非法 conversation_id: %s", conversation_id)
        query = (
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(*conditions)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(query)
        resources: List[Dict[str, Any]] = []
        for message in result.scalars().all():
            response = (message.context or {}).get("agent_response") if isinstance(message.context, dict) else None
            if not isinstance(response, dict):
                continue
            resources.extend(self._resources_from_agent_response(response, resource_type))
            if resources:
                return resources
        return resources

    def _resources_from_agent_response(self, response: Dict[str, Any], resource_type: str) -> List[Dict[str, Any]]:
        resources: List[Dict[str, Any]] = []
        for artifact in response.get("artifacts") or []:
            metadata = artifact.get("metadata") or {}
            content = artifact.get("content")
            if resource_type == "jd" and metadata.get("saved_jd_id"):
                resources.append({
                    "id": str(metadata["saved_jd_id"]),
                    "name": artifact.get("title") or metadata.get("job_title") or "最近生成的 JD",
                    "search_text": self._resource_search_text({
                        "title": artifact.get("title"),
                        "job_title": metadata.get("job_title"),
                        "content": content if isinstance(content, str) else "",
                    }),
                    "source": "conversation_context",
                })
            elif resource_type == "criteria" and metadata.get("saved_criteria_id"):
                resources.append({
                    "id": str(metadata["saved_criteria_id"]),
                    "name": artifact.get("title") or metadata.get("job_title") or "最近生成的评分标准",
                    "search_text": self._resource_search_text({
                        "title": artifact.get("title"),
                        "job_title": metadata.get("job_title"),
                        "content": content if isinstance(content, str) else "",
                    }),
                    "source": "conversation_context",
                })
            elif resource_type == "resume" and artifact.get("type") == "resume_screening_results" and isinstance(content, list):
                for item in content:
                    if item.get("id") and item.get("status") != "failed":
                        resources.append({
                            "id": str(item["id"]),
                            "name": item.get("name") or item.get("filename") or "最近评分的简历",
                            "search_text": self._resource_search_text(item, ["name", "filename", "position", "summary"]),
                            "source": "conversation_context",
                        })
            elif resource_type == "interview" and metadata.get("saved_plan_id"):
                resources.append({
                    "id": str(metadata["saved_plan_id"]),
                    "name": artifact.get("title") or metadata.get("candidate_name") or "最近生成的面试方案",
                    "content": content if isinstance(content, str) else "",
                    "search_text": self._resource_search_text({
                        "title": artifact.get("title"),
                        "candidate_name": metadata.get("candidate_name"),
                        "content": content if isinstance(content, str) else "",
                    }),
                    "source": "conversation_context",
                })
            elif resource_type == "exam" and metadata.get("saved_exam_id"):
                resources.append({
                    "id": str(metadata["saved_exam_id"]),
                    "name": artifact.get("title") or metadata.get("subject") or "最近生成的试卷",
                    "search_text": self._resource_search_text({
                        "title": artifact.get("title"),
                        "subject": metadata.get("subject"),
                        "content": content if isinstance(content, str) else "",
                    }),
                    "source": "conversation_context",
                })
        return resources

    async def _delete_matched_resources(
        self,
        resource_type: str,
        matches: List[Dict[str, Any]],
        user_id: UUID,
        steps: List[AgentStep],
        keyword: str,
        matched_from_context: bool = False,
    ) -> AgentChatResponse:
        deleted = []
        failures = []
        for item in matches:
            try:
                delete_result = await self._delete_resource_by_type(resource_type, item["id"], user_id)
                deleted.append({**item, "delete_result": delete_result or {}})
            except Exception as exc:
                logger.warning("Agent 删除资源失败 type=%s id=%s: %s", resource_type, item.get("id"), exc)
                failures.append({"id": item.get("id"), "name": item.get("name"), "error": str(exc)})

        match_detail = "从最近对话产物中匹配到目标。" if matched_from_context else f"匹配到 {len(matches)} 个目标。"
        steps[1] = steps[1].model_copy(update={"status": "completed", "detail": match_detail})
        steps[2] = steps[2].model_copy(update={
            "status": "completed" if deleted and not failures else ("failed" if not deleted else "completed"),
            "detail": f"已删除 {len(deleted)} 个，失败 {len(failures)} 个。",
        })
        deleted_names = "、".join(item["name"] for item in deleted[:5])
        failure_text = f"；{len(failures)} 个删除失败" if failures else ""
        context_text = "（已按最近对话中的“这个/刚才”指代匹配）" if matched_from_context else ""
        cascade_count = sum(len((item.get("delete_result") or {}).get("deleted_scoring_criteria") or []) for item in deleted)
        cascade_text = f"，并删除关联评分标准 {cascade_count} 条" if resource_type == "jd" and cascade_count else ""
        return AgentChatResponse(
            message=f"已删除{self._resource_type_label(resource_type)}{context_text}：{deleted_names or '无'}{cascade_text}{failure_text}。",
            intent="resource_delete",
            route=self._resource_type_route(resource_type),
            steps=steps,
            artifacts=[
                AgentArtifact(
                    type="delete_resource_result",
                    title="删除结果",
                    content={
                        "resource_type": resource_type,
                        "keyword": keyword,
                        "deleted": deleted,
                        "failures": failures,
                        "matched_from_context": matched_from_context,
                    },
                )
            ],
            suggestions=self._delete_suggestions(resource_type),
        )

    async def _list_deletable_resources(self, resource_type: str, user_id: UUID, keyword: str = "") -> List[Dict[str, Any]]:
        if resource_type == "jd":
            result = await JobDescriptionService(self.db).list_job_descriptions(user_id=user_id, page=1, size=100)
            return [
                {
                    "id": str(item.get("id")),
                    "name": item.get("title") or item.get("job_title") or item.get("name") or "未命名 JD",
                    "location": item.get("location"),
                    "salary_range": item.get("salary_range"),
                    "experience_level": item.get("experience_level"),
                    "updated_at": str(item.get("updated_at")) if item.get("updated_at") else "",
                    "search_text": self._resource_search_text(item, ["id", "title", "job_title", "department", "location", "salary_range", "experience_level", "content", "requirements"]),
                }
                for item in result.get("items", [])
                if item.get("id")
            ]
        if resource_type == "resume":
            evaluations, _ = await ResumeEvaluationService(self.db).get_evaluation_history(user_id=user_id, skip=0, limit=100)
            return [
                {
                    "id": str(item.id),
                    "name": item.candidate_name or item.original_filename or "未命名简历",
                    "search_text": self._resource_search_text({
                        "candidate_name": item.candidate_name,
                        "original_filename": item.original_filename,
                        "candidate_position": item.candidate_position,
                        "education_level": item.education_level,
                    }),
                }
                for item in evaluations
            ]
        if resource_type == "interview":
            result = await InterviewPlanService(self.db).list_interview_plans(user_id=user_id, page=1, size=100)
            items = result.get("items") or result.get("data") or []
            return [
                {
                    "id": str(item.get("id")),
                    "name": item.get("title") or item.get("candidate_name") or item.get("name") or "未命名面试方案",
                    "search_text": self._resource_search_text(item, ["title", "candidate_name", "candidate_position", "job_title", "content"]),
                }
                for item in items
                if item.get("id")
            ]
        if resource_type == "exam":
            result = await ExamService(self.db).get_exam_list(skip=0, limit=100, search=keyword or None)
            return [
                {
                    "id": str(item.get("id")),
                    "name": item.get("title") or item.get("subject") or "未命名试卷",
                    "search_text": self._resource_search_text(item, ["title", "subject", "description", "special_requirements"]),
                }
                for item in result.get("items", [])
                if item.get("id")
            ]
        return []

    def _match_delete_resources(self, resources: List[Dict[str, Any]], keyword: str, latest: bool = False) -> List[Dict[str, Any]]:
        if latest and resources:
            return [resources[0]]
        normalized_keyword = self._normalize_candidate_match_text(keyword)
        if not normalized_keyword:
            return resources[:1] if latest else []
        exact_matches = [
            item for item in resources
            if normalized_keyword == self._normalize_candidate_match_text(item.get("name"))
        ]
        if exact_matches:
            return exact_matches
        return [
            item for item in resources
            if normalized_keyword in self._normalize_candidate_match_text(item.get("search_text"))
            or self._normalize_candidate_match_text(item.get("name")) in normalized_keyword
        ]

    async def _delete_resource_by_type(self, resource_type: str, resource_id: str, user_id: UUID) -> Optional[Dict[str, Any]]:
        if resource_type == "jd":
            return await JobDescriptionService(self.db).delete_job_description(resource_id, user_id)
        if resource_type == "resume":
            success = await ResumeEvaluationService(self.db).delete_evaluation(UUID(resource_id), user_id)
            if not success:
                raise ValueError("简历记录不存在")
            return {}
        if resource_type == "interview":
            await InterviewPlanService(self.db).delete_interview_plan(UUID(resource_id), user_id)
            return {}
        if resource_type == "exam":
            await ExamService(self.db).delete_exam(resource_id)
            return {}
        raise ValueError("不支持的删除类型")

    def _resource_search_text(self, item: Any, keys: Optional[List[str]] = None) -> str:
        if not isinstance(item, dict):
            return ""
        values = []
        source_keys = keys or list(item.keys())
        for key in source_keys:
            value = item.get(key)
            if value is None:
                continue
            values.append(str(value))
        return " ".join(values)

    def _resource_type_label(self, resource_type: Optional[str]) -> str:
        return {
            "jd": "JD",
            "resume": "简历记录",
            "interview": "面试方案",
            "exam": "试卷",
        }.get(resource_type or "", "资源")

    def _resource_type_route(self, resource_type: Optional[str]) -> Optional[str]:
        return {
            "jd": "/recruitment/jd-generator",
            "resume": "/recruitment/resume-screening",
            "interview": "/recruitment/smart-interview",
            "exam": "/training/exam-generator",
        }.get(resource_type or "")

    def _delete_suggestions(self, resource_type: str) -> List[str]:
        return {
            "jd": ["查看 JD 列表", "重新生成 JD"],
            "resume": ["查看简历筛选列表", "重新评分简历"],
            "interview": ["查看面试方案列表", "重新生成面试方案"],
            "exam": ["查看试卷列表", "重新生成试卷"],
        }.get(resource_type, ["查看列表", "换个名称再试"])

    async def _parse_requirements(
        self,
        text: str,
        conversation_id: Optional[str],
        memory_context: str = "",
    ) -> Dict[str, Any]:
        prompt = (
            "你是招聘助手。请从用户需求中提取结构化字段，并严格返回 JSON，不要解释。\n"
            "字段：job_title, department, location, salary, experience, education, job_type, skills, benefits, additional_requirements。\n"
            "优先结合历史对话记忆理解“继续、这个岗位、刚才”等指代；仍未明确的信息返回 null。\n"
            "只提取用户或历史对话中明确提到的信息；未提到的字段必须返回 null，skills/benefits 未提到时返回 []，不要自行推断默认值。\n"
            "additional_requirements 只填写用户明确提出的额外岗位要求、加分项、福利或特殊约束；不要填入历史对话原文、当前用户原句或已结构化到其他字段的信息。\n"
            f"{self._format_memory_for_prompt(memory_context)}"
            f"用户需求：{text}"
        )
        try:
            if self.llm_service is None:
                self.llm_service = LLMService()
            response = await self.llm_service.generate_response(prompt)
            parsed = self._safe_json_loads(response)
            if parsed:
                return self._normalize_requirements(parsed, text)
        except Exception as exc:
            logger.warning("Agent 需求解析失败，使用本地兜底解析: %s", exc)
        return self._fallback_parse(text)

    async def _parse_exam_requirements(
        self,
        text: str,
        conversation_id: Optional[str],
        memory_context: str = "",
    ) -> Dict[str, Any]:
        parsed = self._fallback_exam_parse("\n".join([memory_context, text]) if memory_context else text)
        return self._normalize_exam_requirements(parsed, original_text=text)

    async def _select_exam_knowledge_files(
        self,
        exam: Dict[str, Any],
        message: str,
        user_id: UUID,
    ) -> List[Dict[str, Any]]:
        query = " ".join(
            item for item in [
                self._clean_optional_value(exam.get("subject")),
                self._clean_optional_value(exam.get("title")),
                self._clean_optional_value(exam.get("special_requirements")),
                message,
            ] if item
        ).strip()
        if not query:
            return []
        try:
            selector = KBSelectionService(self.db)
            selection = await selector.select_kb_for_question(
                question=query,
                user_id=user_id,
                max_candidates=200,
            )
        except Exception as exc:
            logger.warning("Agent 试卷知识库文档匹配失败: %s", exc)
            return []
        if not selection or not selection.get("document_id"):
            return []
        confidence = float(selection.get("confidence") or 0)
        if confidence and confidence < 0.35:
            return []
        return [{
            "id": str(selection.get("document_id")),
            "fileName": selection.get("filename") or "知识库文档",
        }]

    async def _generate_jd(self, original_text: str, parsed: Dict[str, Any], conversation_id: Optional[str]) -> str:
        query = self._build_jd_query(original_text, parsed)
        response = await self.dify_service.call_workflow_sync(
            workflow_type=1,
            query=query,
            conversation_id=None,
            additional_inputs={
                "position_title": parsed.get("job_title"),
                "department": parsed.get("department"),
                "experience_level": parsed.get("experience"),
                "task": "agent_generate_jd",
            },
        )
        return self._extract_answer(response).strip()

    async def _generate_jd_stream(
        self,
        original_text: str,
        parsed: Dict[str, Any],
        conversation_id: Optional[str],
    ) -> AsyncGenerator[str, None]:
        query = self._build_jd_query(original_text, parsed)
        full_text = ""
        try:
            async for chunk in self.dify_service.call_workflow_stream(
                workflow_type=1,
                query=query,
                conversation_id=None,
                additional_inputs={
                    "position_title": parsed.get("job_title"),
                    "department": parsed.get("department"),
                    "experience_level": parsed.get("experience"),
                    "task": "agent_generate_jd",
                },
            ):
                raw_delta = self._extract_stream_delta(chunk)
                delta = self._dedupe_stream_delta(raw_delta, full_text)
                if not delta:
                    continue
                full_text += delta
                yield delta
        except Exception as exc:
            logger.warning("Agent JD 流式生成失败，回退同步生成: %s", exc)

        if not full_text.strip():
            fallback = await self._generate_jd(original_text, parsed, conversation_id)
            async for delta in self._stream_text(fallback):
                yield delta

    def _build_jd_query(self, original_text: str, parsed: Dict[str, Any]) -> str:
        query_parts = [f"请基于以下招聘需求生成一份专业、完整、适合发布的中文岗位JD：{original_text}"]
        field_map = {
            "job_title": "岗位名称",
            "department": "部门",
            "location": "工作地点",
            "salary": "薪资范围",
            "experience": "经验要求",
            "education": "学历要求",
        }
        for key, label in field_map.items():
            if parsed.get(key):
                query_parts.append(f"{label}：{parsed[key]}")
        if parsed.get("skills"):
            query_parts.append(f"技能要求：{'、'.join(parsed['skills'])}")
        return "\n".join(query_parts)

    async def _generate_interview_plan_content(
        self,
        resume: ResumeEvaluation,
        jd_content: str,
        conversation_id: Optional[str],
    ) -> str:
        query = self._build_interview_plan_query()
        response = await self.dify_service.call_workflow_sync(
            workflow_type=4,
            query=query,
            conversation_id=None,
            additional_inputs={
                "jianli": resume.resume_content,
                "jd": jd_content,
                "evaluation_metrics": resume.evaluation_metrics or [],
                "total_score": resume.total_score,
                "candidate_name": resume.candidate_name,
                "task": "agent_generate_interview_plan",
            },
        )
        return self._extract_answer(response).strip()

    async def _generate_interview_plan_stream(
        self,
        resume: ResumeEvaluation,
        jd_content: str,
        conversation_id: Optional[str],
    ) -> AsyncGenerator[str, None]:
        query = self._build_interview_plan_query()
        full_text = ""
        try:
            async for chunk in self.dify_service.call_workflow_stream(
                workflow_type=4,
                query=query,
                conversation_id=None,
                additional_inputs={
                    "jianli": resume.resume_content,
                    "jd": jd_content,
                    "evaluation_metrics": resume.evaluation_metrics or [],
                    "total_score": resume.total_score,
                    "candidate_name": resume.candidate_name,
                    "task": "agent_generate_interview_plan",
                },
            ):
                raw_delta = self._extract_stream_delta(chunk)
                delta = self._dedupe_stream_delta(raw_delta, full_text)
                if not delta:
                    continue
                full_text += delta
                yield delta
        except Exception as exc:
            logger.warning("Agent 面试计划流式生成失败，回退同步生成: %s", exc)

        if not full_text.strip():
            fallback = await self._generate_interview_plan_content(resume, jd_content, conversation_id)
            async for delta in self._stream_text(fallback):
                yield delta

    def _build_interview_plan_query(self) -> str:
        return "请根据候选人简历、JD要求和简历评分结果生成一份结构化面试计划。"

    async def _get_resume_evaluation(self, user_id: UUID, resume_evaluation_id: UUID) -> ResumeEvaluation:
        result = await self.db.execute(
            select(ResumeEvaluation).where(
                ResumeEvaluation.id == resume_evaluation_id,
                ResumeEvaluation.user_id == user_id,
            )
        )
        resume = result.scalar_one_or_none()
        if not resume:
            raise ValueError("简历评价记录未找到或无权限访问")
        return resume

    async def _save_exam_source_document(self, filename: str, content: bytes, user_id: UUID) -> Document:
        file_hash = hashlib.sha256(content).hexdigest()
        existing_result = await self.db.execute(
            select(Document).where(
                Document.file_hash == file_hash,
                Document.user_id == user_id,
            )
        )
        existing_document = existing_result.scalar_one_or_none()
        if existing_document:
            return existing_document

        upload_dir = os.path.join(settings.UPLOAD_DIR, str(user_id))
        os.makedirs(upload_dir, exist_ok=True)
        file_path = os.path.join(upload_dir, filename)
        base_name, extension = os.path.splitext(filename)
        counter = 1
        while os.path.exists(file_path):
            file_path = os.path.join(upload_dir, f"{base_name}_{counter}{extension}")
            counter += 1

        with open(file_path, "wb") as file:
            file.write(content)

        document = Document(
            filename=os.path.basename(file_path),
            original_filename=filename,
            file_path=file_path,
            file_size=len(content),
            file_hash=file_hash,
            mime_type=self._get_document_mime_type(filename),
            extracted_content=None,
            embedding=None,
            category="exam_source",
            tags=["hr_agent", "exam_generation"],
            user_id=user_id,
            knowledge_base_id=None,
        )
        self.db.add(document)
        await self.db.commit()
        await self.db.refresh(document)
        return document

    def _get_document_mime_type(self, filename: str) -> str:
        extension = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        mime_types = {
            "pdf": "application/pdf",
            "doc": "application/msword",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "txt": "text/plain",
            "md": "text/markdown",
        }
        return mime_types.get(extension, "application/octet-stream")

    async def _generate_scoring_criteria(
        self,
        jd_content: str,
        parsed: Dict[str, Any],
        conversation_id: Optional[str],
    ) -> str:
        query = self._build_scoring_criteria_query(jd_content)
        response = await self.dify_service.call_workflow_sync(
            workflow_type=2,
            query=query,
            conversation_id=None,
            additional_inputs={
                "job_title": parsed.get("job_title"),
                "task": "agent_generate_scoring_criteria",
            },
        )
        return self._extract_answer(response).strip()

    async def _generate_scoring_criteria_stream(
        self,
        jd_content: str,
        parsed: Dict[str, Any],
        conversation_id: Optional[str],
    ) -> AsyncGenerator[str, None]:
        query = self._build_scoring_criteria_query(jd_content)
        full_text = ""
        try:
            async for chunk in self.dify_service.call_workflow_stream(
                workflow_type=2,
                query=query,
                conversation_id=None,
                additional_inputs={
                    "job_title": parsed.get("job_title"),
                    "task": "agent_generate_scoring_criteria",
                },
            ):
                raw_delta = self._extract_stream_delta(chunk)
                delta = self._dedupe_stream_delta(raw_delta, full_text)
                if not delta:
                    continue
                full_text += delta
                yield delta
        except Exception as exc:
            logger.warning("Agent 评分标准流式生成失败，回退同步生成: %s", exc)

        if not full_text.strip():
            fallback = await self._generate_scoring_criteria(jd_content, parsed, conversation_id)
            async for delta in self._stream_text(fallback):
                yield delta

    def _build_scoring_criteria_query(self, jd_content: str) -> str:
        return (
            f"请基于以下JD内容，生成简历初筛评分标准，总分100分。\n\n{jd_content}\n\n"
            "请包含技能匹配、工作经验、教育背景、项目经验、加分项和淘汰项，并给出可操作的分值区间。"
        )

    async def _save_job_description(
        self,
        jd_content: str,
        parsed: Dict[str, Any],
        original_text: str,
        user_id: UUID,
        conversation_id: Optional[str],
    ):
        jd_data = JobDescriptionCreate(
            title=parsed.get("job_title") or "未命名岗位",
            department=parsed.get("department"),
            location=parsed.get("location"),
            salary_range=parsed.get("salary"),
            experience_level=parsed.get("experience"),
            education=parsed.get("education"),
            job_type=parsed.get("job_type") or "全职",
            skills=parsed.get("skills") or [],
            content=jd_content,
            requirements=parsed.get("additional_requirements") or original_text,
            status="draft",
            meta_data={
                "source": "hr_agent",
                "salary": parsed.get("salary"),
                "location": parsed.get("location"),
                "benefits": parsed.get("benefits") or [],
            },
            conversation_id=conversation_id,
            workflow_type="agent_jd_generation",
        )
        service = JobDescriptionService(self.db)
        return await service.create_job_description(jd_data, user_id)

    async def _save_scoring_criteria(
        self,
        criteria_content: str,
        parsed: Dict[str, Any],
        user_id: UUID,
        conversation_id: Optional[str],
        saved_jd_id: Optional[UUID] = None,
    ):
        criteria_data = ScoringCriteriaCreate(
            title=f"{parsed.get('job_title') or '岗位'}简历评分标准",
            job_title=parsed.get("job_title"),
            content=criteria_content,
            total_score="100",
            status="draft",
            meta_data={"source": "hr_agent"},
            conversation_id=conversation_id,
            workflow_type="agent_scoring_criteria_generation",
            job_description_id=saved_jd_id,
        )
        service = ScoringCriteriaService(self.db)
        return await service.save_scoring_criteria(criteria_data, user_id)

    def _extract_answer(self, response: Any) -> str:
        if isinstance(response, dict):
            if response.get("answer"):
                return str(response["answer"])
            data = response.get("data")
            if isinstance(data, dict) and data.get("answer"):
                return str(data["answer"])
            if response.get("text"):
                return str(response["text"])
            return json.dumps(response, ensure_ascii=False)
        return str(response)

    def _extract_stream_delta(self, chunk: Any) -> str:
        if isinstance(chunk, str):
            try:
                chunk = json.loads(chunk)
            except json.JSONDecodeError:
                return chunk
        if not isinstance(chunk, dict):
            return str(chunk)

        event = chunk.get("event")
        if event in {
            "message_end",
            "agent_message_end",
            "workflow_finished",
            "node_finished",
            "tts_message",
            "tts_message_end",
            "message_file",
        }:
            return ""
        if event and event not in {"message", "agent_message", "text_chunk"}:
            return ""

        for key in ("answer", "delta", "text"):
            value = chunk.get(key)
            if isinstance(value, str):
                return value

        data = chunk.get("data")
        if isinstance(data, dict):
            for key in ("answer", "delta", "text"):
                value = data.get(key)
                if isinstance(value, str):
                    return value
        return ""

    def _normalize_sse_chunk(self, chunk: Any) -> Any:
        if not isinstance(chunk, str):
            return chunk
        text = chunk.strip()
        if not text:
            return ""
        lines = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line and line != "[DONE]":
                lines.append(line)
        return "\n".join(lines)

    def _dedupe_stream_delta(self, delta: str, full_text: str) -> str:
        """兼容 Dify 流式返回增量/累计全文/最终全文，避免前端重复拼接。"""
        if not delta:
            return ""
        if not full_text:
            return delta
        if delta == full_text or delta.strip() == full_text.strip():
            return ""
        if delta.startswith(full_text):
            return delta[len(full_text):]

        # 较大的 chunk 往往是累计全文或最终全文，尝试按前后缀重叠裁剪。
        # 小 token 不做重叠裁剪，避免把正常重复字符误删。
        if len(delta) < 20:
            return delta

        max_overlap = min(len(full_text), len(delta))
        for size in range(max_overlap, 0, -1):
            if full_text.endswith(delta[:size]):
                return delta[size:]
        return delta

    def _safe_json_loads(self, text: str) -> Dict[str, Any]:
        json_text = text.strip()
        if "```" in json_text:
            json_text = re.sub(r"^```(?:json)?", "", json_text, flags=re.IGNORECASE).strip()
            json_text = re.sub(r"```$", "", json_text).strip()
        match = re.search(r"\{.*\}", json_text, re.S)
        if match:
            json_text = match.group(0)
        try:
            value = json.loads(json_text)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _normalize_requirements(self, parsed: Dict[str, Any], original_text: str) -> Dict[str, Any]:
        normalized = {
            "job_title": self._clean_optional_value(parsed.get("job_title")),
            "department": self._clean_optional_value(parsed.get("department")),
            "location": self._clean_optional_value(parsed.get("location")),
            "salary": self._clean_optional_value(parsed.get("salary")),
            "experience": self._clean_optional_value(parsed.get("experience")),
            "education": self._clean_optional_value(parsed.get("education")),
            "job_type": self._clean_optional_value(parsed.get("job_type")),
            "skills": parsed.get("skills") or [],
            "benefits": parsed.get("benefits") or [],
            "additional_requirements": self._clean_optional_value(parsed.get("additional_requirements")),
        }
        if isinstance(normalized["skills"], str):
            normalized["skills"] = [item.strip() for item in re.split(r"[,，、/]", normalized["skills"]) if item.strip()]
        if isinstance(normalized["benefits"], str):
            normalized["benefits"] = [item.strip() for item in re.split(r"[,，、/]", normalized["benefits"]) if item.strip()]
        return normalized

    def _normalize_exam_requirements(
        self,
        parsed: Dict[str, Any],
        original_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        question_counts = parsed.get("question_counts") or {}
        if not isinstance(question_counts, dict):
            question_counts = {}
        normalized = {
            "title": self._clean_optional_value(parsed.get("title")) or (f"{self._clean_optional_value(parsed.get('subject')) or '候选人'}能力测试"),
            "subject": self._clean_optional_value(parsed.get("subject")) or self._infer_exam_subject(original_text or ""),
            "description": self._clean_optional_value(parsed.get("description")),
            "difficulty": self._clean_optional_value(parsed.get("difficulty")) or "medium",
            "duration": self._safe_int(parsed.get("duration"), 60),
            "total_score": self._safe_int(parsed.get("total_score"), 100),
            "question_types": parsed.get("question_types") or ["single_choice", "multiple_choice", "short_answer"],
            "question_counts": {
                "single_choice": self._safe_int(question_counts.get("single_choice"), 5),
                "multiple_choice": self._safe_int(question_counts.get("multiple_choice"), 3),
                "short_answer": self._safe_int(question_counts.get("short_answer"), 2),
            },
            "knowledge_files": self._normalize_knowledge_files(parsed.get("knowledge_files")),
            "special_requirements": self._clean_optional_value(parsed.get("special_requirements")) or "",
            "interview_plan_context": parsed.get("interview_plan_context") if isinstance(parsed.get("interview_plan_context"), dict) else None,
        }
        if isinstance(normalized["question_types"], str):
            normalized["question_types"] = [item.strip() for item in re.split(r"[,，、/]", normalized["question_types"]) if item.strip()]
        return normalized

    def _safe_int(self, value: Any, default: int) -> int:
        if value is None or value == "":
            return default
        match = re.search(r"\d+", str(value))
        return int(match.group(0)) if match else default

    def _normalize_knowledge_files(self, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        files = []
        for item in value:
            if isinstance(item, dict) and item.get("id"):
                files.append({"id": item.get("id"), "fileName": item.get("fileName") or item.get("filename") or "知识库文档"})
        return files

    def _infer_exam_subject(self, text: str) -> str:
        patterns = ["Java", "Python", "Go", "前端", "后端", "产品经理", "AI产品经理", "测试", "算法", "运营"]
        for pattern in patterns:
            if pattern.lower() in text.lower():
                return pattern
        return "通用岗位"

    def _brief_exam_requirements(self, exam: Dict[str, Any]) -> str:
        counts = exam.get("question_counts") or {}
        count_text = "、".join([f"{key}:{value}" for key, value in counts.items() if value])
        return f"{exam.get('title')} / {exam.get('subject')} / {exam.get('total_score')}分 / {exam.get('duration')}分钟 / {count_text}"

    def _clean_optional_value(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() in {"none", "null", "n/a", "na"}:
            return None
        if text in {"无", "暂无", "未提及", "未说明", "未知"}:
            return None
        return text

    def _normalize_optional_list(self, value: Any) -> Optional[List[str]]:
        if value is None:
            return None
        if isinstance(value, list):
            items = [self._clean_optional_value(item) for item in value]
            return [item for item in items if item] or None
        if isinstance(value, str):
            items = [item.strip() for item in re.split(r"[,，、/]", value) if item.strip()]
            return items or None
        return None

    def _is_jd_edit_request(self, message: str) -> bool:
        lowered = message.lower()
        has_jd_target = bool(re.search(r"jd|职位描述|岗位说明书|招聘需求|职位|岗位", lowered))
        if (
            has_jd_target
            and re.search(r"删除|删掉|移除|清理", message)
            and not re.search(r"把|将|内容|职责|要求|福利|技能|薪资|地点|经验|学历", message)
        ):
            return False
        has_edit_action = bool(re.search(r"改改|修改|调整|优化|更新|编辑|润色|改成|改为|换成|加上|增加|删掉|删除|去掉", message))
        has_context = bool(re.search(r"上次|刚才|刚刚|这个|这份|这条|那个|上一版|最近|最新|原来", message))
        return has_jd_target and has_edit_action and (has_context or "jd" in lowered or "职位描述" in message or "岗位说明书" in message)

    def _is_criteria_edit_request(self, message: str) -> bool:
        has_target = bool(re.search(r"评分标准|评分规则|打分标准|筛选标准|简历评分标准|简历评分规则", message))
        has_edit_action = bool(re.search(r"改改|修改|调整|优化|更新|编辑|润色|改成|改为|换成|加上|增加|删掉|删除|去掉|降低|提高", message))
        return has_target and has_edit_action

    def _is_jd_edit_followup(self, message: str, memory_context: str = "") -> bool:
        if not memory_context:
            return False
        waiting_for_changes = bool(re.search(r"你想具体改哪些内容|等待用户说明要修改哪些内容|等待修改要求|想具体改哪些内容", memory_context))
        if not waiting_for_changes:
            return False
        return bool(re.search(r"改成|改为|换成|加上|增加|删掉|删除|去掉|薪资|地点|职责|技能|要求|福利|经验|学历|React|Vue|Python|Java", message, re.I))

    def _is_criteria_edit_followup(self, message: str, memory_context: str = "") -> bool:
        if not memory_context:
            return False
        waiting_for_changes = bool(re.search(r"想具体改哪些评分规则|等待用户说明要修改哪些评分规则|等待修改要求", memory_context))
        if not waiting_for_changes:
            return False
        return bool(re.search(r"改成|改为|换成|加上|增加|删掉|删除|去掉|降低|提高|分|权重|维度|技能|经验|学历|项目|加分|淘汰", message, re.I))

    def _is_generic_jd_edit_request(self, text: str) -> bool:
        normalized = re.sub(r"\s+", "", text)
        normalized = re.sub(r"请|帮我|麻烦|一下|这个|那个|上次|刚才|刚刚|JD|jd|职位描述|岗位说明书", "", normalized)
        return normalized in {"", "改改", "修改", "调整", "优化", "更新", "编辑", "润色", "帮改", "再改"}

    def _is_generic_criteria_edit_request(self, text: str) -> bool:
        normalized = re.sub(r"\s+", "", text)
        normalized = re.sub(r"请|帮我|麻烦|一下|这个|那个|上次|刚才|刚刚|评分标准|评分规则|打分标准|筛选标准|简历评分", "", normalized)
        return normalized in {"", "改改", "修改", "调整", "优化", "更新", "编辑", "润色", "帮改", "再改"}

    def _fallback_parse(self, text: str) -> Dict[str, Any]:
        parsed = {
            "job_title": None,
            "department": None,
            "location": None,
            "salary": None,
            "experience": None,
            "education": None,
            "job_type": None,
            "skills": [],
            "benefits": [],
            "additional_requirements": None,
        }
        patterns = {
            "experience": r"(\d+\s*-\s*\d+年|\d+年以上|\d+年\+?)",
            "salary": r"(\d+\s*-\s*\d+\s*[kK]|\d+\s*[kK]\s*-\s*\d+\s*[kK]|\d+\s*-\s*\d+)",
            "education": r"本科|专科|硕士|博士|大专",
            "job_type": r"全职|兼职|实习",
            "location": r"北京|上海|深圳|广州|杭州|南京|成都|重庆|苏州|武汉|西安|长沙",
        }
        title_match = re.search(r"([\u4e00-\u9fa5A-Za-z0-9+#]+(?:开发|测试|算法|产品|运营|销售|人事|财务|行政)?(?:工程师|经理|专员|主管|顾问|设计师|架构师))", text)
        if title_match:
            parsed["job_title"] = title_match.group(1)
        for key, pattern in patterns.items():
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                parsed[key] = match.group(0).replace(" ", "")
        skill_candidates = ["Java", "Python", "Go", "Vue", "React", "Spring", "MySQL", "Redis", "Docker", "Kubernetes", "AI", "大模型"]
        parsed["skills"] = [skill for skill in skill_candidates if skill.lower() in text.lower()]
        return parsed

    def _fallback_exam_parse(self, text: str) -> Dict[str, Any]:
        title = None
        subject = None
        clean_text = text.strip()
        title_match = re.search(r"(?:生成|出|创建|制作)(?:一份|一个)?(.{1,30}?)(?:试卷|考试|笔试题|题目)", clean_text)
        if title_match:
            subject = title_match.group(1).strip(" ，,。的")
            title = f"{subject}试卷" if subject else None

        named_title_match = re.search(
            r"(?:试卷名称|试卷名|标题|名称|名字)(?:是|为|叫|设为|定为)?[:：]?\s*([^\n，,。；;]+)|(?:就叫|叫做|叫|命名为|取名为|起名为)\s*([^\n，,。；;]+?)(?:吧|。|$)",
            clean_text,
        )
        if named_title_match:
            title_value = (named_title_match.group(1) or named_title_match.group(2) or "").strip()
            title = re.sub(r"(?:的)?试卷$", "", title_value).strip() or title

        subject_candidates = ["Java", "Python", "Go", "前端", "后端", "产品经理", "AI产品经理", "测试", "算法", "运营", "大模型"]
        for candidate in subject_candidates:
            if subject:
                break
            if candidate.lower() in clean_text.lower():
                subject = candidate
                title = title or f"{candidate}试卷"
                break

        total_score_match = re.search(r"(\d+)\s*分", clean_text)
        duration_match = re.search(r"(\d+)\s*(?:分钟|分(?:钟)?|min)", clean_text, re.IGNORECASE)
        counts = {
            "single_choice": self._extract_question_count(clean_text, ["单选题", "单选", "选择题"], 5),
            "multiple_choice": self._extract_question_count(clean_text, ["多选题", "多选"], 3),
            "short_answer": self._extract_question_count(clean_text, ["简答题", "简答", "问答题", "问答"], 2),
        }

        difficulty = None
        if re.search(r"简单|基础|入门", clean_text):
            difficulty = "easy"
        elif re.search(r"困难|高阶|高级", clean_text):
            difficulty = "hard"
        elif re.search(r"中等|中级", clean_text):
            difficulty = "medium"

        special_requirements_match = re.search(r"(?:要求|注意事项|其他)[:：]\s*(.+)", clean_text)

        return {
            "title": title,
            "subject": subject,
            "difficulty": difficulty,
            "duration": int(duration_match.group(1)) if duration_match else None,
            "total_score": int(total_score_match.group(1)) if total_score_match else None,
            "question_types": ["single_choice", "multiple_choice", "short_answer"],
            "question_counts": counts,
            "special_requirements": special_requirements_match.group(1).strip() if special_requirements_match else "",
        }

    def _extract_question_count(self, text: str, labels: List[str], default: int) -> int:
        for label in labels:
            match = re.search(rf"(\d+)\s*(?:道|个)?\s*{label}|{label}\s*(\d+)\s*(?:道|个)?", text)
            if match:
                return int(match.group(1) or match.group(2))
        return default

    def _brief_requirements(self, parsed: Dict[str, Any]) -> str:
        parts = [
            parsed.get("job_title"),
            parsed.get("location"),
            parsed.get("salary"),
            parsed.get("experience"),
        ]
        return " / ".join([str(part) for part in parts if part]) or "已提取基础字段。"

    def _missing_required_fields(self, parsed: Dict[str, Any]) -> List[str]:
        required_fields = ["job_title", "location", "salary", "experience", "education"]
        missing = []
        for field in required_fields:
            value = parsed.get(field)
            if value is None or value == "" or value == []:
                missing.append(field)
        return missing

    def _field_label(self, field: str) -> str:
        labels = {
            "job_title": "岗位名称",
            "location": "工作地点",
            "salary": "薪资范围",
            "experience": "经验要求",
            "education": "学历要求",
            "skills": "核心技能",
        }
        return labels.get(field, field)

    def _fallback_message(self, intent: str, message: str = "") -> str:
        mapping = {
            "interview_plan": "我识别到你想做面试方案。这个工具可以跳转到智能面试页继续处理。",
            "exam_generate": "我识别到你想生成考试/试卷。这个工具可以跳转到试卷生成页继续处理。",
        }
        if intent == "general":
            return (
                "我是招聘场景的 HR Agent，当前主要能帮你做这些事：生成 JD、生成简历评分标准、筛选简历、生成面试计划、基于文档和面试方案生成试卷、生成邮件草稿，以及删除已生成内容。"
                "如果你想让我执行任务，可以直接说其中一个目标。"
            )
        return mapping.get(intent, "我已经识别到需求，但当前还没有可安全自动执行的招聘工具。")

    def _suggestions_for_intent(self, intent: str) -> List[str]:
        mapping = {
            "interview_plan": ["生成面试计划", "先筛选简历", "查看已评分候选人"],
            "exam_generate": ["生成试卷", "上传参考文档", "补充考试配置"],
        }
        return mapping.get(intent, ["生成 JD", "筛选简历", "生成试卷"])
