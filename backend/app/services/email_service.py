"""
招聘邮箱配置、抓取与发送服务。

``EmailConfigService`` 管理 IMAP/SMTP 配置，``EmailFetchService`` 扫描邮件附件并触发简历
评价，``EmailSendService`` 根据 Skill 配置生成或发送通知。数据库、文件下载和外部邮件
服务器不共享事务，因此各流程会分别记录成功或失败状态。
"""
from __future__ import annotations

import smtplib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional
from email.message import EmailMessage as SMTPMessage
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, UUID

from app.utils.email_utils import EmailConfig as ReaderConfig, EmailReader
from pathlib import Path
import os

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.models.email_config import EmailConfig, EmailFetchLog
    from app.schemas.email_config import EmailConfigCreate, EmailConfigUpdate


def _email_config_model():
    from app.models.email_config import EmailConfig
    return EmailConfig


def _email_fetch_log_model():
    from app.models.email_config import EmailFetchLog
    return EmailFetchLog


def _resume_evaluation_model():
    from app.models.resume_evaluation import ResumeEvaluation
    return ResumeEvaluation


def _resume_evaluation_service():
    from app.services.resume_evaluation_service import ResumeEvaluationService
    return ResumeEvaluationService


@dataclass(frozen=True)
class SkillMailConfig:
    """Skill bundle 中的发送邮箱配置。"""

    email: str
    password: str
    smtp_server: str
    smtp_port: int
    use_ssl: bool


class EmailConfigService:
    """管理邮箱配置记录和 IMAP 连通性状态。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def list(self, skip: int = 0, limit: int = 100) -> List[EmailConfig]:
        EmailConfig = _email_config_model()
        stmt = select(EmailConfig).offset(skip).limit(limit).order_by(EmailConfig.created_at.desc())
        result = await self.db.execute(stmt)
        return [row[0] for row in result.all()]

    async def get(self, config_id: str) -> Optional[EmailConfig]:
        EmailConfig = _email_config_model()
        stmt = select(EmailConfig).where(EmailConfig.id == config_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, data: EmailConfigCreate,create_by: UUID) -> EmailConfig:
        EmailConfig = _email_config_model()
        # 请求 Schema 映射到 ORM；created_by/updated_by 由当前登录用户注入，不能由请求正文伪造。
        # password 当前按模型字段直接保存，后续连接时会取出使用，并非密码哈希登录场景。
        config = EmailConfig(
            name=data.name,
            email=data.email,
            imap_server=data.imap_server,
            imap_port=data.imap_port,
            imap_ssl=data.imap_ssl,
            smtp_server=data.smtp_server,
            smtp_port=data.smtp_port,
            smtp_ssl=data.smtp_ssl,
            password=data.password,
            fetch_interval=data.fetch_interval,
            auto_fetch=data.auto_fetch,
            status=data.status,
            subject_keywords = data.subject_keywords,
            connection_status="unknown",
            created_by = create_by,
            updated_by = create_by
        )
        self.db.add(config)
        # flush 先把 INSERT 发送给数据库，commit 才正式提交；refresh 再读取数据库默认字段。
        await self.db.flush()
        await self.db.commit()
        await self.db.refresh(config)
        return config

    async def update(self, config: EmailConfig, data: EmailConfigUpdate) -> EmailConfig:
        # 只修改请求中实际出现的字段；空密码表示“保留原密码”，而不是清空凭据。
        update_data = data.model_dump(exclude_unset=True)
        if "password" in update_data and not update_data.get("password"):
            update_data.pop("password")

        # 在已加载 ORM 对象上逐字段赋值，由 SQLAlchemy 脏检查生成 UPDATE。
        for k, v in update_data.items():
            setattr(config, k, v)
        await self.db.commit()
        # commit 后再 flush 通常已无待发送变更；refresh 用数据库最终值覆盖当前对象状态。
        await self.db.flush()
        await self.db.refresh(config)
        return config

    async def delete(self, config: EmailConfig) -> None:
        await self.db.delete(config)
        await self.db.commit()
        await self.db.flush()

    async def test_connection(self, config: EmailConfig, password: Optional[str] = None) -> bool:
        # 编辑页面可传尚未保存的新密码进行测试；未传时才回退数据库现有密码。
        reader_cfg = ReaderConfig(
            host=config.imap_server,
            port=config.imap_port,
            username=config.email,
            password=password or config.password or "",
            use_ssl=config.imap_ssl,
            protocol="IMAP",
        )
        reader = EmailReader(reader_cfg)
        # EmailReader.connect/disconnect 是同步调用，当前会短暂阻塞异步事件循环。
        ok = reader.connect()
        reader.disconnect()
        config.connection_status = "connected" if ok else "error"
        # 这里只 flush 连接状态，不 commit；调用端负责决定是否与当前请求的其他修改一起提交。
        await self.db.flush()
        return ok


class EmailFetchService:
    """扫描招聘邮箱，并在定时抓取路径中下载附件和触发简历评价。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def manual_fetch(self, config: EmailConfig) -> EmailFetchLog:
        """连接邮箱并统计最近邮件中的简历附件，不下载附件或执行自动评价。

        抓取日志先加入会话并 ``flush``，随后根据连接、扫描结果更新日志和配置状态；本方法
        不 ``commit``，当前 API 调用方会在返回后统一提交。连接失败和扫描异常都以 failed
        日志返回，邮箱连接始终在 finally 中关闭。
        """
        EmailFetchLog = _email_fetch_log_model()
        # 先建立 running 日志并 flush 取得 id；尚未 commit，连接结果会继续更新同一事务中的记录。
        log = EmailFetchLog(email_config_id=config.id, status="running")
        self.db.add(log)
        await self.db.flush()

        reader_cfg = ReaderConfig(
            host=config.imap_server,
            port=config.imap_port,
            username=config.email,
            password=config.password or "",
            use_ssl=config.imap_ssl,
            protocol="IMAP",
        )
        reader = EmailReader(reader_cfg)
        try:
            if not reader.connect():
                log.status = "failed"
                log.error_message = "无法连接到邮箱服务器"
                return log

            # search 返回整个收件箱的邮件 id，因此 emails_found 记录总数；实际只扫描最新 50 封。
            reader.select_folder("INBOX")
            ids = reader.search_emails(["ALL"]) or []
            log.emails_found = len(ids)

            resumes = 0
            take = ids[-50:] if len(ids) > 50 else ids
            for msg_id in take:
                msg = reader.get_email(msg_id)
                if not msg:
                    continue
                # 手动抓取只按扩展名计数，不下载附件，也不验证附件内容能否被简历解析器读取。
                for att in msg.attachments:
                    filename = (att.get("filename") or "").lower()
                    if filename.endswith((".pdf", ".doc", ".docx")):
                        resumes += 1

            log.resumes_extracted = resumes
            log.status = "success"
            config.last_fetch_at = datetime.utcnow()
            config.connection_status = "connected"
            return log
        except Exception as e:
            # 把扫描异常写入业务日志对象而不是向上抛出；外层仍需 commit 才能持久化失败状态。
            log.status = "failed"
            log.error_message = str(e)
            config.connection_status = "error"
            return log
        finally:
            # 成功、失败和提前 return 都会经过 finally，避免 IMAP 连接泄漏。
            reader.disconnect()

    async def list_logs(self, config_id: str, skip: int = 0, limit: int = 100) -> List[EmailFetchLog]:
        EmailFetchLog = _email_fetch_log_model()
        stmt = (
            select(EmailFetchLog)
            .where(EmailFetchLog.email_config_id == config_id)
            .offset(skip)
            .limit(limit)
            .order_by(EmailFetchLog.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return [row[0] for row in result.all()]

    async def fetch_recent_attachments(
        self,
        config: EmailConfig,
        create_by: UUID,
        limit: int = 10,
        subject_keyword: list = None,
        output_dir: Optional[Path] = None,
    ) -> EmailFetchLog:
        """筛选最近邮件，下载附件，并尽力触发自动简历评价。

        日志仅先 ``flush``，最终由调度调用方提交；主题过滤后，以邮件 ID 是否已有评价记录
        作为去重条件。当前不按附件扩展名过滤，任意非空附件都会先写入文件系统并计数，再
        调用评价服务；评价或单个附件失败会被记录并继续，因此成功日志可能包含已保存但未
        评价的附件。评价服务可在循环中独立提交记录，后续日志提交失败也不会回滚已落盘
        文件或已提交评价。
        """
        EmailFetchLog = _email_fetch_log_model()
        log = EmailFetchLog(email_config_id=config.id, status="running")
        self.db.add(log)
        await self.db.flush()

        # 默认按触发用户分目录保存附件。目录创建失败被吸收，真正写文件时会按单附件失败处理。
        base_dir = output_dir or (Path(__file__).resolve().parent.parent.parent / "uploads" / "email_attachments" / str(create_by))
        try:
            os.makedirs(base_dir, exist_ok=True)
        except Exception:
            pass

        reader_cfg = ReaderConfig(
            host=config.imap_server,
            port=config.imap_port,
            username=config.email,
            password=config.password or "",
            use_ssl=config.imap_ssl,
            protocol="IMAP",
        )
        reader = EmailReader(reader_cfg)
        try:
            if not reader.connect():
                log.status = "failed"
                log.error_message = "无法连接到邮箱服务器"
                return log
            logger.debug("邮箱连接状态: %s", getattr(reader.connection, "state", "unknown"))
            select_result = reader.select_folder("INBOX")
            if select_result:
                logger.debug("成功选择邮箱: INBOX")

            ids = reader.search_emails(["ALL"]) or []
            if not ids:
                log.status = "success"
                log.emails_found = 0
                log.resumes_extracted = 0
                return log
            logger.debug("找到邮件 ID: %s", ids)
            take = ids[-limit:] if len(ids) > limit else ids
            log.emails_found = len(take)
            resumes = 0

            for msg_id in reversed(take):
                msg = reader.get_email(msg_id)
                if not msg:
                    continue
                subject = (msg.subject or "").lower()

                if subject_keyword and not any(keyword.lower() in subject for keyword in subject_keyword):
                    continue

                logger.info("发现符合条件的邮件: %s (ID: %s)", subject, msg_id)

                for att in (msg.attachments or []):
                    # filename 直接来自邮件附件；当前路径拼接没有先取 Path(fname).name，
                    # 因而这里尚未像上传接口那样完成目录信息净化。
                    fname = att.get("filename") or "attachment"
                    content = att.get("content")
                    if not content:
                        continue
                    logger.debug("处理附件: %s", fname)
                    # 去重粒度是邮件 id 而不是单个附件：只要该邮件已有一条评价，当前邮件其余附件也会跳过。
                    # 查询失败时仍继续处理，不能视为已经确认附件未处理。
                    try:
                        ResumeEvaluation = _resume_evaluation_model()
                        existing = await self.db.execute(select(ResumeEvaluation).where(ResumeEvaluation.email_id == str(msg_id)))
                        records = existing.scalars().first()
                        if records:
                            logger.info("邮件 ID %s 已存在评价记录，跳过", msg_id)
                            continue
                    except Exception as e:
                        logger.warning("检查邮件 ID %s 是否已存在评价记录时出错: %s", msg_id, e)
                        pass
                    target_path = base_dir / fname
                    idx = 1
                    while target_path.exists():
                        stem = target_path.stem
                        suffix = target_path.suffix
                        target_path = base_dir / f"{stem}_{idx}{suffix}"
                        idx += 1
                    # 文件保存成功即计数；后续自动评价失败不会删除该附件。
                    try:
                        with open(target_path, "wb") as f:
                            f.write(content)
                        resumes += 1
                        logger.info("保存附件成功: %s", target_path)
                        try:
                            ResumeEvaluationService = _resume_evaluation_service()
                            ev_svc = ResumeEvaluationService(self.db)
                            user_id = create_by
                            if user_id:
                                logger.info("开始评价简历: %s", fname)
                                await ev_svc.evaluate_resume_auto(user_id=user_id, subject=subject, file_content=content, filename=fname, email_id=str(msg_id))
                                logger.info("成功评价简历: %s", fname)
                        except Exception as e:
                            logger.warning("评价简历 %s 时出错: %s", fname, e)
                            pass
                    except Exception as e:
                        logger.warning("保存附件 %s 时出错: %s", fname, e)
                        pass

            log.resumes_extracted = resumes
            log.error_message = None
            log.status = "success"
            config.last_fetch_at = datetime.utcnow()
            config.connection_status = "connected"
            return log
        except Exception as e:
            log.status = "failed"
            log.error_message = str(e)
            config.connection_status = "error"
            return log
        finally:
            reader.disconnect()


class EmailSendService:
    """从 Skill 文件读取发送配置，并通过 SMTP 提交单封邮件。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.config_path = Path(__file__).resolve().parents[2] / "skills" / "hr-agent-email" / "config.txt"

    async def send_agent_email(
        self,
        user_id: UUID,
        recipient_email: str,
        subject: str,
        body: str,
    ) -> dict:
        """加载 Skill 邮箱配置并同步调用 SMTP 发送。

        当前实现直接在异步方法中执行阻塞 SMTP，没有切换到工作线程；配置读取、参数校验、
        登录、网络或服务器拒收异常都会传播给调用方。返回 submitted 只表示 SMTP 服务器
        接受了提交，不代表最终投递成功。
        """
        # 当前发送配置来自全局 Skill 文件，user_id 未参与账号选择或权限判断；
        # 调用端必须在进入本服务前确认当前用户允许发送邮件。
        config = self._load_skill_mail_config()

        # 在建立 SMTP 连接前完成最低限度必填校验，避免发送空主题/正文。
        if not recipient_email:
            raise ValueError("缺少收件人邮箱地址。")
        if not subject:
            raise ValueError("缺少邮件主题。")
        if not body:
            raise ValueError("缺少邮件正文。")

        rejected_recipients = self._send_via_smtp(
            config=config,
            recipient_email=recipient_email,
            subject=subject,
            body=body,
        )
        if rejected_recipients:
            reason = rejected_recipients.get(recipient_email) or next(iter(rejected_recipients.values()))
            raise ValueError(f"SMTP 服务器拒收该邮件：{reason}")

        return {
            "recipient_email": recipient_email,
            "subject": subject,
            "sender_email": config.email,
            "config_path": str(self.config_path),
            "status": "submitted",
            "delivery_note": "SMTP 服务器已接受邮件，但最终投递结果以收件方服务器返回为准。",
        }

    def _load_skill_mail_config(self) -> SkillMailConfig:
        # 该配置独立于数据库 EmailConfig，读取的是 skills/hr-agent-email/config.txt。
        if not self.config_path.exists():
            raise ValueError(f"未找到邮箱配置文件：{self.config_path}")

        raw: dict[str, str] = {}
        for line in self.config_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            # 忽略空行、注释和不符合 KEY=VALUE 的行；split(..., 1) 允许值本身包含等号。
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            raw[key.strip()] = value.strip()

        def require(key: str) -> str:
            value = raw.get(key, "").strip()
            if not value or value.startswith("your_") or value.endswith("example.com"):
                raise ValueError(f"邮箱配置缺少有效字段：{key}，请先填写 {self.config_path}")
            return value

        def optional_int(key: str, default: int) -> int:
            value = raw.get(key, "").strip()
            if not value:
                return default
            try:
                return int(value)
            except ValueError as exc:
                raise ValueError(f"邮箱配置字段 {key} 必须是整数") from exc

        use_ssl = raw.get("MAIL_ACCOUNT_1_USE_SSL", "true").strip().lower() in {"1", "true", "yes", "on"}

        return SkillMailConfig(
            email=require("MAIL_ACCOUNT_1_EMAIL"),
            password=require("MAIL_ACCOUNT_1_PASSWORD"),
            smtp_server=require("MAIL_ACCOUNT_1_SMTP_SERVER"),
            smtp_port=optional_int("MAIL_ACCOUNT_1_SMTP_PORT", 465 if use_ssl else 587),
            use_ssl=use_ssl,
        )

    def _send_via_smtp(
        self,
        config: SkillMailConfig,
        recipient_email: str,
        subject: str,
        body: str,
    ) -> dict:
        message = SMTPMessage()
        message["From"] = config.email
        message["To"] = recipient_email
        message["Subject"] = subject
        message.set_content(body)

        if config.use_ssl:
            # SSL 模式从建连开始加密，通常使用 465 端口。
            with smtplib.SMTP_SSL(config.smtp_server, config.smtp_port or 465, timeout=20) as server:
                server.login(config.email, config.password or "")
                return server.send_message(message) or {}

        # 非 SSL 模式先普通连接，再尽力升级 STARTTLS。当前升级失败会被忽略，
        # 随后仍继续登录和发送，因此是否允许明文降级取决于 SMTP 服务器环境。
        with smtplib.SMTP(config.smtp_server, config.smtp_port or 587, timeout=20) as server:
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except Exception:
                pass
            server.login(config.email, config.password or "")
            return server.send_message(message) or {}
