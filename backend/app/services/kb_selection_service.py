"""
基于问题自动选择知识库的服务。

服务先读取当前用户可见的候选文档，只把文档 ID 和文件名组成紧凑提示交给 LLM；模型
返回最佳文档后，再从原候选集合反查可信的知识库 ID。候选数上限用于控制 token 成本，
模型输出无效时返回空选择而不是猜测资源。
"""
import logging
import json
from typing import List, Dict, Any, Optional
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.llm_service import LLMService
from app.services.lightweight_document_service import LightweightDocumentService

# 配置日志记录
logger = logging.getLogger(__name__)


class KBSelectionService:
    """用 LLM 推荐候选文档，再以用户范围内的本地候选集确定可信知识库。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.llm_service = LLMService()
        self.document_service = LightweightDocumentService(db)

    async def list_candidates(
        self,
        user_id: UUID,
        max_candidates: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        读取用户自己的候选文档，并转换成选择阶段使用的最小数据结构。

        ``max_candidates`` 同时限制数据库返回量和后续提示词长度，避免文档较多时
        一次请求消耗过多 token。
        """
        try:
            # 这里只做数据库查询，不生成向量，也不调用大模型。
            documents = await self.document_service.get_user_documents(
                user_id=user_id,
                skip=0,
                limit=max_candidates,
            )

            # document_id 和 filename 可以发给模型；knowledge_base_id 只保留在本地，
            # 等模型选出文档后再反查，不能让模型自行编造知识库 ID。
            candidates: List[Dict[str, Any]] = []
            for doc in documents:
                candidates.append({
                    "document_id": str(doc.id),
                    "filename": doc.filename,
                    "knowledge_base_id": str(doc.knowledge_base_id) if doc.knowledge_base_id else None,
                })
            return candidates
        except Exception as e:
            logger.error(f"Error listing candidate documents: {e}")
            raise

    async def select_best_document(
        self,
        question: str,
        candidates: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """
        让大模型从候选文档中选出一个最相关项。

        返回值只是模型建议，调用方仍需回到原始候选列表校验 document_id；模型输出
        不是合法 JSON、缺少关键字段或调用失败时返回 ``None``，交由上层执行降级。
        """
        if not candidates:
            # 空列表无需调用模型，既节省成本，也避免模型在没有候选时凭空回答。
            return None

        # 提示词只携带选择所必需的字段，减少 token 用量并隐藏内部 knowledge_base_id。
        compact = [
            {
                "document_id": c.get("document_id"),
                "filename": c.get("filename"),
            }
            for c in candidates
        ]

        system_prompt = (
            "You are an expert selector. Given a user question and a list of documents "
            "(each with document_id and filename), choose the single most relevant document. "
            "Respond ONLY with valid JSON: {\"document_id\": string, \"confidence\": number, \"reason\": string}. "
            "Confidence is in [0,1]. No extra commentary."
        )
        user_message = (
            "Question: " + question + "\n\n" +
            "Documents: " + json.dumps(compact, ensure_ascii=False)
        )

        try:
            # temperature=0 减少随机性；max_tokens 限制模型只能返回短小的选择结果。
            response = await self.llm_service.client.chat.completions.create(
                model=self.llm_service.llm_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=0,
                max_tokens=400,
            )
            content = response.choices[0].message.content.strip()

            # 模型输出属于不可信外部数据：先解析 JSON，再检查业务必需字段。
            selected: Dict[str, Any] = json.loads(content)
            if not isinstance(selected, dict) or "document_id" not in selected:
                logger.warning(f"Invalid LLM selection output: {content}")
                return None
            return selected
        except Exception as e:
            # 自动选择是辅助能力，失败时返回空结果，让主业务决定如何降级。
            logger.error(f"Error selecting best document via LLM: {e}")
            return None

    async def select_kb_for_question(
        self,
        question: str,
        user_id: UUID,
        max_candidates: int = 100,
    ) -> Dict[str, Any]:
        """
        串联“读取候选、模型选择、本地校验”三个阶段，返回知识库选择结果。

        无候选或模型失败时仍返回固定结构，前端无需针对异常形状做额外判断。
        """
        # 第一阶段：候选已经按 user_id 过滤，模型看不到其他用户的文档。
        candidates = await self.list_candidates(user_id=user_id, max_candidates=max_candidates)

        # 第二阶段：模型只负责推荐 document_id，不直接决定最终知识库 ID。
        selection = await self.select_best_document(question=question, candidates=candidates)

        if not selection:
            return {
                "knowledge_base_id": None,
                "document_id": None,
                "filename": None,
                "confidence": 0.0,
                "reason": "No selection or no candidates",
                "candidates_count": len(candidates),
            }

        selected_doc_id = selection.get("document_id")
        confidence = selection.get("confidence", 0.0)
        reason = selection.get("reason", "")

        # 第三阶段：只信任本地候选集合。即使模型返回了不存在的 ID，也不会映射到任意知识库。
        selected_candidate = next((c for c in candidates if c["document_id"] == selected_doc_id), None)
        if not selected_candidate:
            return {
                "knowledge_base_id": None,
                "document_id": selected_doc_id,
                "filename": None,
                "confidence": confidence,
                "reason": reason or "Selected document not in candidate list",
                "candidates_count": len(candidates),
            }

        return {
            "knowledge_base_id": selected_candidate.get("knowledge_base_id"),
            "document_id": selected_doc_id,
            "filename": selected_candidate.get("filename"),
            "confidence": confidence,
            "reason": reason,
            "candidates_count": len(candidates),
        }