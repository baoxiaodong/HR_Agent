"""
IMAP/POP3 邮件读取与 MIME 消息解析工具。

本模块只负责建立协议连接、检索原始邮件并转换为内存中的 ``EmailMessage``；简历筛选、
附件落盘、去重和数据库事务属于上层邮件服务。协议异常大多会被记录并转换为 ``False``、
空列表或 ``None``，调用方需要把这些降级值与“邮箱确实没有邮件”区分处理。
"""
import imaplib
import platform
import poplib
import smtplib
import email
import socket
from email.header import decode_header
from email.message import Message
from typing import List, Optional, Tuple
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class EmailConfig:
    """邮件协议连接参数的内存容器。

    密码保持为明文属性以便协议库登录，因此对象不应被序列化进 API 响应或普通日志。
    ``protocol`` 会统一为大写，实际支持范围在 ``EmailReader.connect`` 中校验。
    """
    def __init__(self, host: str, port: int, username: str, password: str, 
                 use_ssl: bool = True, protocol: str = "IMAP"):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.protocol = protocol.upper()  # IMAP or POP3


class EmailMessage:
    """解析后的邮件值对象。

    ``body`` 与 ``html_body`` 分开保存；附件内容仍是内存中的原始字节及 MIME 元数据，
    尚未经过文件类型校验、病毒扫描或落盘处理。
    """
    def __init__(self, subject: str, sender: str, recipients: List[str], 
                 date: datetime, body: str, html_body: Optional[str] = None,
                 attachments: Optional[List[dict]] = None):
        self.subject = subject
        self.sender = sender
        self.recipients = recipients
        self.date = date
        self.body = body
        self.html_body = html_body
        self.attachments = attachments or []


class EmailReader:
    """管理 IMAP/POP3 连接，并把协议响应转换成统一邮件对象。

    推荐通过上下文管理器使用以确保退出时断开连接。连接、搜索和单封读取失败通常不会
    向上抛出，而是记录日志并返回降级值；附件和正文会完整载入内存。
    """

    def __init__(self, config: EmailConfig):
        self.config = config
        self.connection = None


    def connect(self) -> bool:
        """
        连接到邮件服务器
        
        Returns:
            bool: 连接成功返回True，否则返回False
        """
        try:
            if self.config.protocol == "IMAP":
                if self.config.use_ssl:
                    self.connection = imaplib.IMAP4_SSL(self.config.host, self.config.port)
                else:
                    self.connection = imaplib.IMAP4(self.config.host, self.config.port)
                self.connection.login(self.config.username, self.config.password)


            elif self.config.protocol == "POP3":
                if self.config.use_ssl:
                    self.connection = poplib.POP3_SSL(self.config.host, self.config.port)
                else:
                    self.connection = poplib.POP3(self.config.host, self.config.port)
                self.connection.user(self.config.username)
                self.connection.pass_(self.config.password)
            else:
                raise ValueError(f"Unsupported protocol: {self.config.protocol}")

            logger.info(f"Successfully connected to {self.config.protocol} server {self.config.host}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to email server: {str(e)}")
            return False

    def disconnect(self):
        """
        断开与邮件服务器的连接
        """
        if self.connection:
            try:
                if self.config.protocol == "IMAP":
                    # 只有在SELECTED状态下才调用close()
                    # 否则直接logout以避免"command CLOSE illegal in state AUTH"错误
                    try:
                        self.connection.close()
                    except Exception as close_error:
                        # 如果close失败，很可能是因为我们不在SELECTED状态
                        # 这是可以接受的，只需记录为调试信息并继续logout
                        logger.debug(f"Close command failed (likely not in SELECTED state): {str(close_error)}")
                    self.connection.logout()
                elif self.config.protocol == "POP3":
                    self.connection.quit()
                logger.info("Disconnected from email server")
            except Exception as e:
                logger.error(f"Error while disconnecting: {str(e)}")
            finally:
                self.connection = None

    def list_folders(self) -> List[str]:
        """
        列出所有文件夹/邮箱（仅限IMAP）
        
        Returns:
            List[str]: 文件夹名称列表
        """
        if not self.connection or self.config.protocol != "IMAP":
            return []
        
        try:
            imaplib.Commands["ID"] = "NONAUTH"
            args = ("name", "111",  "version", "1.0.0", "vendor", "myclient")
            typ, dat = self.connection._simple_command('ID', '("' + '" "'.join(args) + '")')
            print(self.connection._untagged_response(typ, dat, 'ID'))
            status, folders = self.connection.list()
            if status == "OK":
                folder_names = []
                for folder in folders:
                    # 从响应中解析文件夹名称
                    folder_str = folder.decode('utf-8') if isinstance(folder, bytes) else folder
                    # 提取文件夹名称（通常在引号中）
                    if '"' in folder_str:
                        folder_name = folder_str.split('"')[-2]
                    else:
                        folder_name = folder_str.split()[-1]
                    folder_names.append(folder_name)
                return folder_names
            return []
        except Exception as e:
            logger.error(f"Error listing folders: {str(e)}")
            return []

    def select_folder(self, folder: str = "INBOX") -> int:
        """
        选择要读取邮件的文件夹（仅限IMAP）
        
        Args:
            folder (str): 文件夹名称，默认为INBOX
            
        Returns:
            int: 文件夹中的邮件数量
        """
        if not self.connection or self.config.protocol != "IMAP":
            return 0
        
        try:
            imaplib.Commands["ID"] = "NONAUTH"
            args = ("name", "111",  "version", "1.0.0", "vendor", "myclient")
            typ, dat = self.connection._simple_command('ID', '("' + '" "'.join(args) + '")')

            print(self.connection._untagged_response(typ, dat, 'ID'))
            status, msg_count = self.connection.select(folder)
            print(f'status:{status}, msg_count:{msg_count}')
            if status == "OK":
                count = int(msg_count[0]) if isinstance(msg_count[0], bytes) else int(msg_count[0])
                logger.info(f"Selected folder '{folder}' with {count} messages")
                return count
            return 0
        except Exception as e:
            logger.error(f"Error selecting folder '{folder}': {str(e)}")
            return 0

    def search_emails(self, criteria: list, charset: Optional[str] = "UTF-8") -> List[int]:
        """根据邮件协议搜索条件返回消息编号。

    首次使用 UID SEARCH；异常时回退到普通 SEARCH，因此两个分支返回编号的稳定性语义
    可能不同。空列表既可能表示无匹配，也可能表示协议不支持或查询失败。
    """
        if not self.connection or self.config.protocol != "IMAP":
            return []
        
        try:
            # 优先使用 UTF-8 字符集，失败则回退到默认
            status, messages = self.connection.uid('SEARCH', *criteria)
            # status, messages = self.connection.search(charset, *criteria)
            if status == "OK":
                # Convert byte strings to integers
                msg_ids = messages[0].split()
                print(f'msg_ids:{msg_ids}')
                return [int(msg_id) for msg_id in msg_ids]
            return []
        except Exception as e:
            logger.warning(f"Search with charset '{charset}' failed: {str(e)}; retrying without charset")
            try:
                status, messages = self.connection.search(None, *criteria)
                if status == "OK":
                    msg_ids = messages[0].split()
                    return [int(msg_id) for msg_id in msg_ids]
                return []
            except Exception as e2:
                logger.error(f"Error searching emails with criteria '{criteria}': {str(e2)}")
                return []

    def get_email(self, msg_id: int) -> Optional[EmailMessage]:
        """
        根据ID检索特定邮件
        
        Args:
            msg_id (int): 邮件ID
            
        Returns:
            EmailMessage: 邮件消息对象，失败时返回None
        """
        if not self.connection:
            return None
            
        try:
            if self.config.protocol == "IMAP":
                return self._get_imap_email(msg_id)
            elif self.config.protocol == "POP3":
                return self._get_pop3_email(msg_id)
            return None
        except Exception as e:
            logger.error(f"Error retrieving email {msg_id}: {str(e)}")
            return None

    def _get_imap_email(self, msg_id: int) -> Optional[EmailMessage]:
        """
        使用IMAP协议检索邮件
        
        Args:
            msg_id (int): 邮件ID
            
        Returns:
            EmailMessage: 邮件消息对象
        """
        status, msg_data = self.connection.uid('FETCH', str(msg_id), "(RFC822)")
        if status != "OK":
            return None
            
        raw_email = msg_data[0][1]
        email_message = email.message_from_bytes(raw_email)
        return self._parse_email_message(email_message)

    def _get_pop3_email(self, msg_id: int) -> Optional[EmailMessage]:
        """按 POP3 的从 1 开始序号读取原始消息并解析。

        POP3 序号只在当前邮箱会话中有意义，不具备 IMAP UID 的稳定标识语义。
        """
        # POP3 uses 1-based indexing
        msg_lines, octets = self.connection.retr(msg_id)
        raw_email = b'\n'.join(msg_lines)
        email_message = email.message_from_bytes(raw_email)
        return self._parse_email_message(email_message)

    def _parse_email_message(self, email_message: Message) -> EmailMessage:
        """把标准库 MIME 消息解析为内部 ``EmailMessage``。

        多段邮件分别收集纯文本、HTML 和附件；未知编码用 UTF-8 忽略错误回退。日期解析只
        覆盖当前固定格式，解析失败时使用本机当前时间，因此该时间不能视为可靠邮件证据。
        """
        # Decode subject
        subject = self._decode_header_value(email_message.get("Subject", ""))
        
        # Get sender
        sender = self._decode_header_value(email_message.get("From", ""))
        
        # Get recipients
        to_header = email_message.get("To", "")
        recipients = [self._decode_header_value(addr) for addr in to_header.split(",")] if to_header else []
        
        # Get date
        date_str = email_message.get("Date", "")
        try:
            date = datetime.strptime(date_str[:-6], "%a, %d %b %Y %H:%M:%S") if date_str else datetime.now()
        except:
            date = datetime.now()
        
        # Get body
        body = ""
        html_body = ""
        attachments = []
        
        if email_message.is_multipart():
            for part in email_message.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))
                
                # Skip attachments
                if "attachment" in content_disposition:
                    filename = part.get_filename()
                    if filename:
                        filename = self._decode_header_value(filename)
                        payload = part.get_payload(decode=True)
                        attachments.append({
                            "filename": filename,
                            "content": payload,
                            "content_type": content_type
                        })
                    continue
                
                # Process text parts
                if content_type == "text/plain":
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        body = part.get_payload(decode=True).decode(charset, errors="ignore")
                    except Exception:
                        body = part.get_payload()
                elif content_type == "text/html":
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        html_body = part.get_payload(decode=True).decode(charset, errors="ignore")
                    except Exception:
                        html_body = part.get_payload()
        else:
            # Single part message
            content_type = email_message.get_content_type()
            charset = email_message.get_content_charset() or "utf-8"
            if content_type == "text/plain":
                try:
                    body = email_message.get_payload(decode=True).decode(charset, errors="ignore")
                except Exception:
                    body = email_message.get_payload()
            elif content_type == "text/html":
                try:
                    html_body = email_message.get_payload(decode=True).decode(charset, errors="ignore")
                except Exception:
                    html_body = email_message.get_payload()
        
        return EmailMessage(
            subject=subject,
            sender=sender,
            recipients=recipients,
            date=date,
            body=body,
            html_body=html_body,
            attachments=attachments
        )

    def _decode_header_value(self, header_value: str) -> str:
        """解码可能由多段文本和不同字符集组成的 MIME 邮件头。

        声明字符集解码失败时回退到 UTF-8 并忽略非法字节，优先保证主题、发件人等字段
        可展示；因此返回文本可能不是原始字节的无损表示。
        """
        if not header_value:
            return ""
            
        decoded_parts = decode_header(header_value)
        decoded_string = ""
        for part, encoding in decoded_parts:
            if isinstance(part, bytes):
                if encoding:
                    try:
                        decoded_string += part.decode(encoding)
                    except Exception:
                        decoded_string += part.decode('utf-8', errors='ignore')
                else:
                    decoded_string += part.decode('utf-8', errors='ignore')
            else:
                decoded_string += part
        return decoded_string

    def __enter__(self):
        """进入上下文时尝试连接；调用方仍可检查连接结果。"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出上下文时断开连接，不吞掉业务代码抛出的异常。"""
        self.disconnect()


# 仅用于手工联调协议连接；示例中的账号配置不参与应用运行时依赖注入。
if __name__ == "__main__":
    # 手工联调示例。运行前必须替换为专用测试邮箱，不能把真实凭据提交到源码。
    config = EmailConfig(
        host="imap.163.com",
        port=993,
        username="user@example.com",
        # password="change-me",
        password="change-me",
        protocol="IMAP"
    )

    with EmailReader(config) as reader:
        # For IMAP, select folder first
        reader.select_folder("INBOX")
        criteria = ['FROM', '"kunpeng55@qq.com"']
        # Search for emails
        email_ids = reader.search_emails(criteria )

        # Get latest email
        if email_ids:
            latest_email = reader.get_email(email_ids[-1])
            if latest_email:
                print(f"Subject: {latest_email.subject}")
                print(f"From: {latest_email.sender}")
                print(f"Body: {latest_email.body[:100]}...")
