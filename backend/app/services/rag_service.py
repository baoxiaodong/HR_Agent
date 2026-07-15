"""
基于 LangChain 的检索增强问答服务。

负责查询增强、向量与全文混合检索、结果融合和可选重排，并构造普通对话或 RAG 流式回答。
数据库访问仅用于读取向量/文本上下文，本服务不保存对话消息或提交业务事务。
"""
import logging
import re
from typing import List, Dict, Any, Optional
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

# LangChain导入
from langchain_core.documents import Document as LangChainDocument
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
from langchain_postgres import PGVector
from langchain_openai import ChatOpenAI

from app.services.embedding_service import get_embedding_service
from app.services.rerank_service import get_rerank_service
from app.core.config import settings

logger = logging.getLogger(__name__)


class RAGService:
    """编排检索路由、上下文选择和流式回答。"""

    def __init__(self, db: AsyncSession):
        self.db = db

        # 初始化嵌入服务
        self.embedding_service = get_embedding_service()
        self.embeddings = self.embedding_service.get_embeddings()

        # 初始化重排服务
        self.rerank_service = get_rerank_service()

        # 初始化LLM
        self.llm = ChatOpenAI(
            model=settings.LLM_MODEL,
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
            temperature=0.7,
            max_tokens=2000
        )

        # PGVector的数据库连接字符串
        self.connection_string = settings.DATABASE_URL

        logger.info("RAG服务已使用LangChain组件初始化")

    # ---------- 查询增强 ----------

    def _enhance_query_for_kb(self, question: str, conversation_history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        """尝试用 LLM 重写检索查询并生成有限数量的扩展词。

        功能关闭或调用失败时原样返回问题；非 JSON 响应会按字符令牌启发式提取扩展词。
        返回内容用于提高召回，不代表已经确定用户的真实语义或检索意图。
        """
        try:
            # 关闭增强时完全跳过额外模型调用，检索继续使用用户原问题。
            if not getattr(settings, "KB_QUERY_ENHANCE_ENABLED", False):
                return {"rewritten_query": question, "expanded_keywords": []}

            system_prompt = (
                "你是一个检索查询增强器。\n"
                "通过对上下文理解，理解用户真实意图，输出更清晰的检索查询和若干关键术语扩展。\n"
                "返回严格的 JSON 对象：{{\"rewritten_query\": \"...\", \"expanded_keywords\": [\"...\"]}}。\n"
                "注意：扩展术语需短而准，避免过长句子。"
            )

            conversation_history = conversation_history or []
            prompt = ChatPromptTemplate.from_messages([
                ("system", system_prompt),
                MessagesPlaceholder(variable_name="chat_history"),
                ("human", "原始查询：{question}\n请返回 JSON 格式结果")
            ])

            enhancer_llm = ChatOpenAI(
                model=settings.LLM_MODEL,
                api_key=settings.LLM_API_KEY,
                base_url=settings.LLM_BASE_URL,
                temperature=0.2,
                max_tokens=512
            )

            chain = (
                {
                    "question": RunnablePassthrough(),
                    "chat_history": lambda x: conversation_history
                }
                | prompt
                | enhancer_llm
                | StrOutputParser()
            )

            raw = chain.invoke(question)
            # 模型输出只影响召回查询，不替代最终用户问题；解析失败仍保留原问题。
            rewritten_query = question
            expanded_keywords: List[str] = []

            try:
                import json as pyjson
                data = pyjson.loads(raw)
                rewritten_query = data.get("rewritten_query") or question
                ek = data.get("expanded_keywords") or []
                if isinstance(ek, list):
                    expanded_keywords = [str(t).strip() for t in ek if str(t).strip()]
                elif isinstance(ek, str):
                    # 兼容模型把数组错误输出为逗号分隔字符串。
                    expanded_keywords = [t.strip() for t in ek.split(',') if t.strip()]
            except Exception:
                # 非 JSON 输出仅提取中英文/数字令牌作为扩展词，不把整段模型回复直接拼入 SQL 查询。
                terms = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", raw)
                expanded_keywords = [t.lower() for t in terms if len(t) >= 2]

            max_terms = getattr(settings, "KB_QUERY_EXPANSION_MAX_TERMS", 6)
            # 截断扩展词数量，防止模型输出过多词导致全文查询膨胀。
            if len(expanded_keywords) > max_terms:
                expanded_keywords = expanded_keywords[:max_terms]

            return {"rewritten_query": rewritten_query, "expanded_keywords": expanded_keywords}
        except Exception as e:
            logger.warning(f"查询增强失败: {e}")
            return {"rewritten_query": question, "expanded_keywords": []}

    # ---------- 混合检索与融合 ----------

    async def _tsvector_search(
        self,
        collection_name: str,
        query: str,
        k: int = 5,
        knowledge_base_id: Optional[UUID] = None,
        extra_terms: Optional[List[str]] = None
    ) -> List[tuple]:
        """只读查询 langchain_pg_embedding，返回全文检索文档和排名分数。

        查询词通过简单正则、停用词和扩展词构造 OR 前缀 tsquery，同时保留原问题的 ILIKE
        包含匹配；这是一种召回启发式。集合名始终过滤，知识库过滤可选；SQL 或解析失败时
        记录警告并返回空列表，不影响向量检索分支。
        """
        try:
            # 1) 查询重写：提取英文/数字/中文词元，移除常见问句停用词，构造 OR 前缀 tsquery
            stop_words = {"有哪些", "什么", "如何", "怎么", "请问", "的", "和", "与"}
            terms = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", query)
            terms = [t.lower() for t in terms if t not in stop_words and len(t) >= 2]
            # 追加额外的扩展术语
            if extra_terms:
                for t in extra_terms:
                    t = str(t).strip().lower()
                    if t and t not in terms and t not in stop_words:
                        terms.append(t)
            # 构造 tsquery
            tsquery_or = " | ".join(f"{t}:*" for t in terms) if terms else None

            # SQL 结构固定，查询词、集合名、知识库 id 和 limit 都通过绑定参数传入。
            # :tsq 为空时只保留 ILIKE 原问题兜底。
            base_sql = (
                "SELECT id, document, cmetadata, "
                "ts_rank_cd(to_tsvector('simple', document), to_tsquery('simple', :tsq)) AS rank "
                "FROM langchain_pg_embedding "
                "WHERE cmetadata->>'collection_name' = :collection_name "
                "AND ("
                "     ( :tsq IS NOT NULL AND to_tsvector('simple', document) @@ to_tsquery('simple', :tsq) ) "
                "     OR document ILIKE '%' || :q || '%'"
                ") "
            )
            params = {"q": query, "tsq": tsquery_or, "collection_name": collection_name, "limit": k}

            if knowledge_base_id:
                base_sql += "AND cmetadata->>'knowledge_base_id' = :kb_id "
                params["kb_id"] = str(knowledge_base_id)

            base_sql += "ORDER BY rank DESC LIMIT :limit"

            res2 = await self.db.execute(text(base_sql), params)
            rows = res2.fetchall()
            results: List[tuple] = []
            for r in rows:
                doc_text = r[1]
                metadata = r[2] or {}
                # 将数据库行恢复为与向量检索相同的 LangChainDocument + score 形状，便于后续统一融合。
                page_content = doc_text
                lc_doc = LangChainDocument(page_content=page_content, metadata=metadata)
                results.append((lc_doc, float(r[3]) if r[3] is not None else 0.0))
            return results
        except Exception as e:
            logger.warning(f"tsvector搜索错误: {e}")
            return []

    async def _merge_docs_with_scores(
        self,
        content_results: List[tuple],
        text_results: List[tuple],
        query: str,
        top_k: int = 5,
        min_similarity_score: float = 0.2
    ) -> (List[LangChainDocument], List[Dict[str, Any]]):
        """按文档和块索引去重，使用配置权重融合向量分数与全文分数。

        融合后按阈值过滤并截取 top_k，配置启用时再调用重排服务。融合或重排任一环节异常
        会退回原始向量结果的前 top_k，此回退不再应用融合阈值或全文结果。
        """
        try:
            merged_map: Dict[tuple, Dict[str, Any]] = {}

            # 以 document_id + chunk_index 识别同一文档块，使向量和全文两路命中合并为一项。
            # 若元数据同时缺少这两个字段，多个缺失项会共享 (None, None) 键并互相覆盖。
            for doc, score in content_results:
                key = (doc.metadata.get("document_id"), doc.metadata.get("chunk_index"))
                merged_map[key] = merged_map.get(key, {"doc": doc, "content_score": 0.0})
                merged_map[key]["doc"] = doc
                merged_map[key]["content_score"] = float(score)

            # 全文命中补充 text_score；只被全文命中的块会以 content_score=0 加入。
            for doc, score in text_results:
                key = (doc.metadata.get("document_id"), doc.metadata.get("chunk_index"))
                entry = merged_map.get(key, {"doc": doc, "content_score": 0.0})
                entry["doc"] = entry.get("doc") or doc
                entry["text_score"] = float(score)
                merged_map[key] = entry

            # 两路分数按配置权重直接线性组合；这里假设向量相关度和 ts_rank 分数可按当前权重比较。
            combined_list: List[tuple] = []
            for key, entry in merged_map.items():
                content_score = float(entry.get("content_score", 0.0))
                text_score = float(entry.get("text_score", 0.0))
                combined_score = (settings.RAG_CONTENT_WEIGHT * content_score + 
                                settings.RAG_TEXT_WEIGHT * text_score)
                combined_list.append((entry["doc"], combined_score, entry))

            # 先排序再按最低相关度过滤，最后截取 top_k，低分块不会进入提示词上下文。
            combined_list.sort(key=lambda x: x[1], reverse=True)
            combined_list = [item for item in combined_list if float(item[1]) >= min_similarity_score]

            top = combined_list[:top_k]
            docs: List[LangChainDocument] = []
            sources: List[Dict[str, Any]] = []
            for doc, combined_score, entry in top:
                final_page_content = doc.page_content
                final_doc = LangChainDocument(page_content=final_page_content, metadata=doc.metadata)
                docs.append(final_doc)
                sources.append({
                    "document_id": doc.metadata.get("document_id"),
                    "document_title": doc.metadata.get("filename", "Unknown"),
                    "chunk_id": doc.metadata.get("chunk_id"),
                    "chunk_index": doc.metadata.get("chunk_index", 0),
                    "content": final_page_content,
                    "combined_score": float(combined_score),
                    "content_score": float(entry.get("content_score", 0.0)),
                    "text_score": float(entry.get("text_score", 0.0)),
                    "metadata": doc.metadata
                })

            # 重排是融合后的可选第二阶段；服务可根据 query 重新调整顺序和截断结果。
            if settings.RERANK_ENABLED and self.rerank_service.is_enabled():
                docs, sources = await self.rerank_service.rerank_documents(
                    query=query,
                    documents=docs,
                    sources=sources,
                    top_k=top_k
                )
            else:
                # 如果没有重排则只取top_k
                docs = docs[:top_k]
                sources = sources[:top_k]

            return docs, sources

        except Exception as e:
            logger.warning(f"合并多路径结果时出错: {e}")
            # 备用方案：仅返回content_results
            docs = [doc for doc, _ in content_results[:top_k]]
            sources = []
            for doc, score in content_results[:top_k]:
                sources.append({
                    "document_id": doc.metadata.get("document_id"),
                    "document_title": doc.metadata.get("filename", "Unknown"),
                    "chunk_id": doc.metadata.get("chunk_id"),
                    "chunk_index": doc.metadata.get("chunk_index", 0),
                    "content": doc.page_content,
                    "combined_score": float(score),
                    "content_score": float(score),
                    "text_score": 0.0,
                    "metadata": doc.metadata
                })
            return docs, sources

    # ---------- 链构造 ----------

    def _create_rag_chain_with_docs(self, docs: List[LangChainDocument], conversation_history: List[Dict[str, str]]):
        """把已经检索出的文档和对话历史绑定到回答链。

        本方法不再检索或访问数据库；上下文在链构造时捕获，真正的远程 LLM 调用发生在
        后续 invoke/astream 阶段。
        """
        from langchain_core.messages import HumanMessage, AIMessage
        
        # 转换对话历史格式
        formatted_history = []
        for msg in conversation_history:
            if msg.get("role") == "user":
                formatted_history.append(HumanMessage(content=msg.get("content", "")))
            elif msg.get("role") == "assistant":
                formatted_history.append(AIMessage(content=msg.get("content", "")))

        # 创建提示模板
        system_prompt = """你是一个智能助手，基于提供的上下文信息回答用户问题。

上下文信息：
{context}

请根据上下文信息回答用户的问题。如果上下文信息不足以回答问题，请诚实地说明。
保持回答准确、有用且简洁。"""

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            MessagesPlaceholder(variable_name="chat_history"),
            ("human", "{question}")
        ])

        # 格式化文档
        def format_docs(docs_list):
            return "\n\n".join(doc.page_content for doc in docs_list)

        # 使用预检索文档创建链
        rag_chain = (
            {
                "context": lambda x: format_docs(docs),
                "question": RunnablePassthrough(),
                "chat_history": lambda x: formatted_history
            }
            | prompt
            | self.llm
            | StrOutputParser()
        )

        return rag_chain

    # ---------- 流式问答与上下文 ----------

    def _should_use_knowledge_base(self, question: str) -> bool:
        """用关键词和 LLM 输出启发式选择知识库或普通对话路径。

        精确命中少量闲聊词时直接返回 False；其他问题依赖模型输出，无法识别或调用异常时
        默认返回 True。该结果只是路由选择，不是确定的语义分类。
        """
        try:
            # 先做关键词预筛：纯闲聊关键词直接走 GENERAL，避免浪费 LLM 调用
            chitchat_keywords = ["你好", "谢谢", "再见", "你是谁", "讲个笑话", "作首诗", "写首诗", "聊天"]
            question_stripped = question.strip()
            if any(question_stripped == kw for kw in chitchat_keywords):
                logger.info(f"关键词预筛命中闲聊，跳过KB检索: {question}")
                return False

            # 带明确指令和示例的基于LLM的分类
            classification_prompt = (
                "你是一个分类器。判断该问题是否需要基于知识库内容回答还是由大模型自主回答。\n"
                "只有用户在明显是闲聊的内容才输出GENERAL。\n"
                "其他情况都输出KB\n"
                "示例：\n"
                "问：讲个笑话\n答：GENERAL\n"
                "问：你好\n答：GENERAL\n"
                "问：你是谁\n答：GENERAL\n"
                "问：作首诗\n答：GENERAL\n"
                "而其他情况或者判断不准的时候，都输出KB\n"
                "重要规则：你只需要输出一个词。只能回答KB或者GENERAL。不要解释，不要说其他任何话。\n"
                f"问：{question}\n答："
            )
            # 使用确定性分类器LLM
            from langchain_openai import ChatOpenAI
            classifier_llm = ChatOpenAI(
                model=settings.LLM_MODEL,
                api_key=settings.LLM_API_KEY,
                base_url=settings.LLM_BASE_URL,
                temperature=0,
                max_tokens=50
            )
            resp = classifier_llm.invoke(classification_prompt)
            content = getattr(resp, "content", str(resp))
            logger.info(f"KB意图分类结果: question='{question[:50]}...', response='{content}'")
            # 如果明确包含 GENERAL 且不包含 KB，不走知识库
            if "GENERAL" in (content or "").upper() and "KB" not in (content or "").upper():
                return False
            # 其余情况一律走知识库（默认走 KB 更安全）
            return True
        except Exception as e:
            logger.warning(f"KB意图检测失败，默认使用KB: {e}")
            return True

    def _create_general_chat_chain(self, conversation_history: List[Dict[str, str]]):
        """构造不读取知识库上下文的普通对话链。"""
        from langchain_core.messages import HumanMessage, AIMessage
        
        # 转换对话历史格式
        formatted_history = []
        for msg in conversation_history:
            if msg.get("role") == "user":
                formatted_history.append(HumanMessage(content=msg.get("content", "")))
            elif msg.get("role") == "assistant":
                formatted_history.append(AIMessage(content=msg.get("content", "")))
        
        system_prompt = (
            "你是一个智能助手。直接根据用户问题进行回答，不使用任何知识库上下文。"
            "保持回答准确、简洁、有帮助。"
        )
        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            MessagesPlaceholder(variable_name="chat_history"),
            ("human", "{question}")
        ])
        chain = (
            {
                "question": RunnablePassthrough(),
                "chat_history": lambda x: formatted_history
            }
            | prompt
            | self.llm
            | StrOutputParser()
        )
        return chain

    async def ask_question_stream(
        self,
        question: str,
        user_id: UUID,
        knowledge_base_id: Optional[UUID] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        context_limit: int = settings.CONTEXT_LIMIT
    ):
        """按启发式路由执行普通对话或 RAG 流式问答。

        普通路径不检索；知识库路径可先增强查询，再读取 PGVector 向量结果和 PostgreSQL
        全文结果，融合/重排后构造 RAG 链。即使传入 knowledge_base_id，当前代码也不会
        强制覆盖路由判断；没有相关文档时回退普通对话。数据库操作均为只读且没有 commit。

        普通对话或无文档回退的生成异常会发送 error 后结束；RAG 生成异常会发送 error，
        随后仍按现有流程发送 end。更外层的检索或构链异常只发送 error。

        Args:
            question: 用户问题
            user_id: 用于文档过滤的用户ID
            knowledge_base_id: 可选的知识库ID用于过滤
            conversation_history: 之前的对话消息
            context_limit: 要检索的最大上下文文档数

        Yields:
            包含流式响应数据的字典
        """
        try:
            conversation_history = conversation_history or []

            # 1. 启发式选择普通对话或知识库路径。
            use_kb = self._should_use_knowledge_base(question)
            print('use_kb=======', use_kb)
            # 如果提供了特定的知识库，总是使用KB检索
            # if knowledge_base_id:
            #     use_kb = True

            logger.info(f"ask_question_stream: use_kb={use_kb}, knowledge_base_id={knowledge_base_id}, user_id={user_id}")

            if not use_kb:
                # 流式传输不使用KB检索的通用LLM答案
                yield {
                    "type": "start",
                    "question": question,
                    "sources": [],
                    "context_used": False,
                    "num_sources": 0
                }
                try:
                    general_chain = self._create_general_chat_chain(conversation_history)
                    # 使用真正的流式输出
                    async for chunk in general_chain.astream({"question": question}):
                        # 处理不同类型的chunk
                        if isinstance(chunk, str):
                            if chunk.strip():  # 只发送非空字符串
                                yield {"type": "chunk", "content": chunk}
                        elif isinstance(chunk, dict):
                            # 处理字典类型的chunk
                            if "content" in chunk and chunk["content"].strip():
                                yield {"type": "chunk", "content": chunk["content"]}
                            elif "output" in chunk and chunk["output"].strip():
                                yield {"type": "chunk", "content": chunk["output"]}
                    
                    yield {"type": "end", "complete": True, "sources": [], "num_sources": 0}
                except GeneratorExit:
                    logger.info("客户端断开连接，停止流式响应")
                    return
                except Exception as e:
                    logger.error(f"流式响应生成错误: {str(e)}")
                    yield {"type": "error", "error": str(e)}
                return
            print('conversation_history =======', conversation_history)
            # 2. 知识库路径可选增强查询，再执行向量和全文两路只读检索。
            enhance = self._enhance_query_for_kb(question, conversation_history)
            print('enhance=======', enhance)
            rewritten_query = enhance.get("rewritten_query", question)
            print('rewritten_query=======', rewritten_query)
            expanded_keywords = enhance.get("expanded_keywords", [])
            print('expanded_keywords=======', expanded_keywords)

            # 用户 id 编入集合名，先把向量检索限制到该用户的分块集合。
            collection_name = f"document_chunks_{user_id}".replace("-", "_")

            # PGVector 使用同一嵌入模型把重写查询转向量；这里只建立集合访问对象。
            vector_store = PGVector(
                connection=self.connection_string,
                embeddings=self.embeddings,
                collection_name=collection_name,
                use_jsonb=True
            )
            # 关键词存储已移除；仅保留块向量存储

            # 可选知识库 id 在用户集合范围内继续过滤，不能扩大到其他用户集合。
            filter_conditions = {}
            if knowledge_base_id:
                filter_conditions["knowledge_base_id"] = str(knowledge_base_id)

            # 第一条路径执行语义向量检索。该 LangChain API 是同步方法，当前直接在异步生成器中调用。
            content_results = vector_store.similarity_search_with_relevance_scores(
                rewritten_query, k=context_limit, filter=filter_conditions if filter_conditions else None
            )

            # 第二条路径使用原始问题做 PostgreSQL 全文检索，并加入模型生成的扩展词提高召回。
            text_results = await self._tsvector_search(
                collection_name,
                question,
                k=context_limit,
                knowledge_base_id=knowledge_base_id,
                extra_terms=expanded_keywords
            )

            # # 调试：输出两个路径的结果对比
            # logger.info(f"=== 向量搜索结果 (content_results) ===")
            # for i, (doc, score) in enumerate(content_results):
            #     logger.info(f"向量结果 {i+1}: 分数={score:.4f}, 文档ID={doc.metadata.get('document_id')}, 块索引={doc.metadata.get('chunk_index')}")
            #     logger.info(f"内容预览: {doc.page_content[:200]}...")
            
            # logger.info(f"=== 全文检索结果 (text_results) ===")
            # for i, (doc, score) in enumerate(text_results):
            #     logger.info(f"文本结果 {i+1}: 分数={score:.4f}, 文档ID={doc.metadata.get('document_id')}, 块索引={doc.metadata.get('chunk_index')}")
            #     logger.info(f"内容预览: {doc.page_content[:200]}...")

            # 3. 加权融合并可选重排；无结果时退回普通对话链。
            relevant_docs, sources = await self._merge_docs_with_scores(
                content_results,
                text_results,
                question,
                top_k=context_limit,
                min_similarity_score=settings.RAG_MIN_SIMILARITY_SCORE
            )

            # 没有足够相关的文档时，直接走纯 LLM 生成
            if not relevant_docs:
                yield {
                    "type": "start",
                    "question": question,
                    "query_rewrite": {
                        "rewritten_query": rewritten_query,
                        "expanded_keywords": expanded_keywords
                    },
                    "sources": [],
                    "context_used": False,
                    "num_sources": 0
                }

                try:
                    general_chain = self._create_general_chat_chain(conversation_history)
                    async for chunk in general_chain.astream({"question": question}):
                        if isinstance(chunk, str):
                            if chunk.strip():
                                yield {"type": "chunk", "content": chunk}
                        elif isinstance(chunk, dict):
                            if "content" in chunk and chunk["content"].strip():
                                yield {"type": "chunk", "content": chunk["content"]}
                            elif "output" in chunk and chunk["output"].strip():
                                yield {"type": "chunk", "content": chunk["output"]}
                    yield {"type": "end", "complete": True, "sources": [], "num_sources": 0}
                except GeneratorExit:
                    logger.info("客户端断开连接，停止纯LLM流式响应")
                    return
                except Exception as e:
                    logger.error(f"纯LLM流式响应生成错误: {str(e)}")
                    yield {"type": "error", "error": str(e)}
                return

            # 使用来源产生初始数据
            yield {
                "type": "start",
                "question": question,
                "query_rewrite": {
                    "rewritten_query": rewritten_query,
                    "expanded_keywords": expanded_keywords
                },
                "sources": sources,
                "context_used": True,
                "num_sources": len(sources)
            }

            # 4. 使用已选上下文构链并流式输出，不再执行额外检索。
            rag_chain = self._create_rag_chain_with_docs(relevant_docs, conversation_history)

            # 流式传输LLM的响应
            try:
                # 使用真正的流式输出
                async for chunk in rag_chain.astream({"question": question}):
                    # 处理不同类型的chunk
                    if isinstance(chunk, str):
                        if chunk.strip():  # 只发送非空字符串
                            yield {"type": "chunk", "content": chunk}
                    elif isinstance(chunk, dict):
                        # 处理字典类型的chunk
                        if "content" in chunk and chunk["content"].strip():
                            yield {"type": "chunk", "content": chunk["content"]}
                        elif "output" in chunk and chunk["output"].strip():
                            yield {"type": "chunk", "content": chunk["output"]}
                    
            except GeneratorExit:
                logger.info("客户端断开连接，停止RAG流式响应")
                return
            except Exception as e:
                logger.error(f"RAG流式响应生成错误: {str(e)}")
                yield {"type": "error", "error": str(e)}

            # 使用来源产生完成信号以供前端显示
            yield {
                "type": "end",
                "complete": True,
                "sources": sources,
                "num_sources": len(sources)
            }

        except Exception as e:
            logger.error(f"流式RAG问答中出错: {e}")
            yield {
                "type": "error",
                "error": str(e)
            }


    async def get_conversation_context(
        self,
        conversation_history: List[Dict[str, str]],
        max_messages: int = 10
    ) -> List[Dict[str, str]]:
        """截取最近消息并转换角色名称，不访问数据库或远程服务。

        只保留 user/assistant 两类消息，其他角色会被忽略；返回列表是内存中的新结构，
        不会保存或修改原始对话记录。

        Args:
            conversation_history: 对话消息列表
            max_messages: 要保留的最大消息数

        Returns:
            处理后的对话历史
        """
        if not conversation_history:
            return []

        # 仅保留最后max_messages条消息
        recent_history = conversation_history[-max_messages:]

        # 为LangChain格式化
        formatted_history = []
        for msg in recent_history:
            if msg.get("role") == "user":
                formatted_history.append({"role": "human", "content": msg["content"]})
            elif msg.get("role") == "assistant":
                formatted_history.append({"role": "ai", "content": msg["content"]})

        return formatted_history
