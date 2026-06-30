"""
用于密码哈希和JWT令牌的安全工具
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Union, Optional
from jose import jwt
from passlib.context import CryptContext

from app.core.config import settings

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = "HS256"


def create_access_token(
    data: dict, expires_delta: Optional[timedelta] = None
) -> str:
    """
    创建JWT访问令牌
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    验证密码与其哈希值
    """
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """
    哈希密码

    Args:
        password: 要哈希的密码

    Returns:
        哈希后的密码

    Raises:
        ValueError: 如果密码对于bcrypt来说太长
    """
    try:
        # bcrypt的最大密码长度为72字节
        password_bytes = password.encode('utf-8')
        if len(password_bytes) > 72:
            # 截断密码到72字节，但对此进行警告
            logger.warning(f"密码长度为{len(password_bytes)}字节，截断为72字节以兼容bcrypt")
            password = password_bytes[:72].decode('utf-8', errors='ignore')

        return pwd_context.hash(password)
    except Exception as e:
        logger.error(f"密码哈希错误: {e}")
        raise


def verify_token(token: str) -> Optional[dict]:
    """
    验证并解码JWT令牌
    """
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[ALGORITHM]
        )
        return payload
    except jwt.JWTError:
        return None