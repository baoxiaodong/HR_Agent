"""
对话与消息的持久化服务。

``BaseConversationService`` 集中处理所有权校验和错误包装，``ConversationService`` 负责
会话、消息的增删改查以及消息计数。写操作在本服务内提交事务，失败时回滚；上层聊天或
Agent 服务只需要按业务顺序调用这些方法，不直接操作会话表。
"""
import logging
from typing import List, Optional, Dict, Any
from uuid import UUID
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, desc, func, case
from sqlalchemy.orm import selectinload

from app.models.conversation import Conversation, Message, MessageRole, ConversationStatus
from app.schemas.conversation import ConversationCreate, ConversationUpdate, MessageCreate, MessageUpdate
from app.schemas.user import User as UserSchema

logger = logging.getLogger(__name__)


class BaseConversationService:
    """集中提供会话所有权检查和端点错误包装。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_conversation_with_permission_check(
        self,
        conversation_id: str,
        current_user: UserSchema
    ) -> Any:
        """读取会话后校验当前用户是否为所有者。

        该方法把“资源不存在”和“资源属于他人”分别映射为 404 与 403。调用方必须传入由认证
        依赖得到的 ``current_user``，不能使用请求体中的用户标识代替。
        """
        conversation = await self.get_conversation(conversation_id)
        
        if not conversation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="对话未找到"
            )
        
        # 所有权以数据库中的 user_id 为准，而不是会话 ID 能否被猜中或客户端声明。
        if conversation.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足"
            )
        
        return conversation

    def handle_conversation_error(self, error: Exception, operation: str) -> HTTPException:
        """
        统一处理对话相关错误
        
        Args:
            error: 异常对象
            operation: 操作描述
            
        Returns:
            HTTPException: 格式化后的HTTP异常
        """
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{operation}时出错: {str(error)}"
        )


class ConversationService(BaseConversationService):
    """管理会话与消息事务；需要调用方在消息操作前建立父会话权限边界。"""

    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create_conversation(
        self,
        user_id: UUID,
        conversation_data: ConversationCreate
    ) -> Conversation:
        """用认证用户 ID 和请求 Schema 创建一条活动会话。

        Pydantic 请求字段被复制到 ORM 实体；``user_id`` 由上层认证上下文单独传入，客户端
        不能通过 Schema 指定所有者。``commit`` 使记录持久化，``refresh`` 再读取数据库生成的
        ID、时间戳和默认值；任一步失败都会回滚当前事务。
        """
        try:
            conversation = Conversation(
                user_id=user_id,
                title=conversation_data.title,
                description=conversation_data.description,
                status=ConversationStatus.ACTIVE,
                meta_data=conversation_data.meta_data or {}
            )

            self.db.add(conversation)
            await self.db.commit()
            await self.db.refresh(conversation)

            logger.info(f"为用户{user_id}创建了对话{conversation.id}")
            return conversation

        except Exception as e:
            await self.db.rollback()
            logger.error(f"创建对话时出错: {e}")
            raise

    async def get_conversation(
        self,
        conversation_id: UUID,
        user_id: Optional[UUID] = None
    ) -> Optional[Conversation]:
        """按 ID 读取会话，并可把用户归属加入同一 SQL 条件。

        面向用户的调用应始终传 ``user_id``；省略时只做内部资源查询，不具备权限隔离。该方法
        只读数据库，不提交事务，未找到返回 ``None``。
        """
        try:
            query = select(Conversation).where(Conversation.id == conversation_id)

            # 传入用户后，存在性与所有权由数据库一次判断，避免先查后验。
            if user_id:
                query = query.where(Conversation.user_id == user_id)

            result = await self.db.execute(query)
            return result.scalar_one_or_none()

        except Exception as e:
            logger.error(f"获取对话{conversation_id}时出错: {e}")
            raise

    async def get_user_conversations(
        self,
        user_id: UUID,
        skip: int = 0,
        limit: int = 20,
        status: Optional[ConversationStatus] = None
    ) -> List[Conversation]:
        """分页读取当前用户的会话，可选按状态过滤。

        用户隔离是查询的固定前提，状态只是进一步收窄；结果以最后更新时间倒序返回，整个
        方法不加载消息详情也不写数据库。
        """
        try:
            # 先固定所有者条件，客户端分页和状态参数无法扩大到其他用户。
            query = select(Conversation).where(Conversation.user_id == user_id)

            if status:
                query = query.where(Conversation.status == status)

            query = query.order_by(desc(Conversation.updated_at)).offset(skip).limit(limit)

            result = await self.db.execute(query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"获取用户{user_id}的对话时出错: {e}")
            raise

    async def update_conversation(
        self,
        conversation_id: UUID,
        user_id: UUID,
        conversation_data: ConversationUpdate
    ) -> Optional[Conversation]:
        """在所有权校验通过后，仅更新请求中显式提供的会话字段。

        ``exclude_unset`` 区分“未提交字段”和“明确提交空值”；空更新不会产生 SQL 或提交。
        更新与提交失败时回滚，成功后 ``refresh`` 让返回 ORM 对象反映数据库最新值。
        """
        try:
            # 读取条件同时包含用户 ID；找不到时不暴露资源是否属于其他用户。
            conversation = await self.get_conversation(conversation_id, user_id)
            if not conversation:
                return None

            # Schema 转普通字典后交给 SQLAlchemy update，未传字段保持原值。
            update_data = conversation_data.dict(exclude_unset=True)
            if update_data:
                query = (
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(**update_data)
                )
                await self.db.execute(query)
                await self.db.commit()
                await self.db.refresh(conversation)

            logger.info(f"更新了对话{conversation_id}")
            return conversation

        except Exception as e:
            await self.db.rollback()
            logger.error(f"更新对话{conversation_id}时出错: {e}")
            raise

    async def delete_conversation(
        self,
        conversation_id: UUID,
        user_id: UUID
    ) -> bool:
        """校验所有者后，在一个数据库事务中删除消息和会话。

        当前实现显式先删子表消息再删父表会话；两条 SQL 共用一次 ``commit``，任一失败会
        ``rollback``，因此不会只提交其中一半。未找到当前用户会话时返回 ``False``。
        """
        try:
            # 只有当前用户拥有的会话才会进入删除阶段。
            conversation = await self.get_conversation(conversation_id, user_id)
            if not conversation:
                return False

            # 先清理外键依赖的消息，避免数据库未配置级联删除时父记录删除失败。
            await self.db.execute(
                delete(Message).where(Message.conversation_id == conversation_id)
            )

            # 删除对话
            await self.db.execute(
                delete(Conversation).where(Conversation.id == conversation_id)
            )

            await self.db.commit()
            logger.info(f"删除了对话{conversation_id}")
            return True

        except Exception as e:
            await self.db.rollback()
            logger.error(f"删除对话{conversation_id}时出错: {e}")
            raise

    async def add_message(
        self,
        conversation_id: UUID,
        content: str,
        role: MessageRole,
        model_name: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        parent_id: Optional[UUID] = None
    ) -> Message:
        """创建消息并同步增加所属会话的消息计数和活动时间。

        消息插入与会话计数更新在同一次 ``commit`` 中提交，``refresh`` 获取消息的数据库默认
        字段。此方法本身不接收 ``user_id``、也不验证会话归属，调用方必须先通过用户范围的
        会话查询或权限检查确认该 ``conversation_id`` 可写。
        """
        try:
            message = Message(
                conversation_id=conversation_id,
                content=content,
                role=role,
                model_name=model_name,
                context=context or {},
                parent_message_id=parent_id
            )

            self.db.add(message)
            print('消息保存成功')
            # 更新对话消息计数和最后活动时间
            await self.db.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(
                    total_messages=Conversation.total_messages + 1,
                    updated_at=func.now()
                )
            )

            await self.db.commit()
            await self.db.refresh(message)
            print('update conversation message number success')
            logger.info(f"向对话{conversation_id}添加了消息")
            return message

        except Exception as e:
            # 当前实现没有主动 rollback；异常继续抛出后，调用方在复用该会话前必须处理失败事务。
            # 这与本类其他写方法不同，是理解后续 ``PendingRollbackError`` 风险的关键边界。
            # await self.db.rollback()
            logger.error(f"向对话{conversation_id}添加消息时出错: {e}")
            raise

    async def get_conversation_messages(
        self,
        conversation_id: UUID,
        skip: int = 0,
        limit: int = 50
    ) -> List[Message]:
        """按时间正序分页读取一个会话的消息。

        这里只按 ``conversation_id`` 查询，不验证会话所有者；面向用户的入口应先调用会话
        权限检查。返回 ORM 列表，不提交事务。
        """
        try:
            query = (
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at)
                .offset(skip)
                .limit(limit)
            )

            result = await self.db.execute(query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"获取对话{conversation_id}的消息时出错: {e}")
            raise

    async def get_message(self, message_id: UUID) -> Optional[Message]:
        """按消息 ID 读取单条记录，未命中返回 ``None``。

        本方法不带会话 ID 或用户 ID，因此只适合作为已经完成父会话权限校验后的内部查询；
        更新、删除方法会继续核对消息所属会话，但用户所有权仍由上层负责。
        """
        try:
            query = select(Message).where(Message.id == message_id)
            result = await self.db.execute(query)
            return result.scalar_one_or_none()

        except Exception as e:
            logger.error(f"获取消息{message_id}时出错: {e}")
            raise

    async def update_message(
        self,
        conversation_id: UUID,
        message_id: UUID,
        message_update: MessageUpdate
    ) -> Optional[Message]:
        """更新指定会话中的消息，并刷新会话活动时间。

        先确认消息记录确实属于传入会话，再把请求 Schema 的 ``user_feedback`` 适配为 ORM
        的 ``feedback`` 字段；两次 update 共用一次提交。这里不接收用户 ID，用户归属必须由
        调用方对父会话提前校验。
        """
        try:
            message = await self.get_message(message_id)
            if not message or str(message.conversation_id) != str(conversation_id):
                return None

            update_data = message_update.model_dump(exclude_unset=True)
            if "user_feedback" in update_data:
                update_data["feedback"] = update_data.pop("user_feedback")
            if update_data:
                await self.db.execute(
                    update(Message)
                    .where(
                        Message.id == message_id,
                        Message.conversation_id == conversation_id
                    )
                    .values(**update_data, updated_at=func.now())
                )
                await self.db.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(updated_at=func.now())
                )
                await self.db.commit()
                await self.db.refresh(message)

            logger.info(f"更新了对话{conversation_id}中的消息{message_id}")
            return message

        except Exception as e:
            await self.db.rollback()
            logger.error(f"更新消息{message_id}时出错: {e}")
            raise

    async def delete_message(self, conversation_id: UUID, message_id: UUID) -> bool:
        """删除会话内一条消息，并以不低于零的规则递减消息计数。

        消息删除和父会话计数更新时间共用一个事务；失败时全部回滚。方法只校验消息与会话
        的关联，不校验用户所有权，上层必须先验证父会话属于当前用户。
        """
        try:
            message = await self.get_message(message_id)
            if not message or str(message.conversation_id) != str(conversation_id):
                return False

            await self.db.execute(
                delete(Message).where(
                    Message.id == message_id,
                    Message.conversation_id == conversation_id
                )
            )
            await self.db.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(
                    total_messages=case(
                        (Conversation.total_messages > 0, Conversation.total_messages - 1),
                        else_=0
                    ),
                    updated_at=func.now()
                )
            )
            await self.db.commit()
            logger.info(f"删除了对话{conversation_id}中的消息{message_id}")
            return True

        except Exception as e:
            await self.db.rollback()
            logger.error(f"删除消息{message_id}时出错: {e}")
            raise

    async def update_message_feedback(
        self,
        message_id: str,
        rating: int,
        feedback: str = ""
    ) -> bool:
        """把评分和文字反馈写入消息记录，返回是否实际命中行。

        字符串 ID 先转 UUID，再以 JSON 字典写入反馈字段。该底层方法没有 ``user_id`` 或父会话
        条件；调用方如果直接暴露给用户，必须先验证消息所属会话的所有权。
        """
        try:
            message_uuid = UUID(message_id)
            query = (
                update(Message)
                .where(Message.id == message_uuid)
                .values(
                    user_feedback={
                        "rating": rating,
                        "feedback": feedback
                    }
                )
            )

            result = await self.db.execute(query)
            await self.db.commit()

            return result.rowcount > 0

        except Exception as e:
            await self.db.rollback()
            logger.error(f"更新消息反馈{message_id}时出错: {e}")
            raise

    async def search_conversations(
        self,
        user_id: UUID,
        query: str,
        limit: int = 10
    ) -> List[Conversation]:
        """在当前用户会话标题中执行大小写不敏感的包含搜索。

        搜索条件固定包含 ``user_id``，不会返回其他用户记录；当前实现只查标题，模块旧注释中
        的“内容搜索”并未查询消息正文，也不是数据库全文检索。
        """
        try:
            # ILIKE 参数由 SQLAlchemy 绑定，不直接拼接为可执行 SQL；百分号表示包含匹配。
            search_query = (
                select(Conversation)
                .where(
                    Conversation.user_id == user_id,
                    Conversation.title.ilike(f"%{query}%")
                )
                .order_by(desc(Conversation.updated_at))
                .limit(limit)
            )

            result = await self.db.execute(search_query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"搜索用户{user_id}的对话时出错: {e}")
            raise
