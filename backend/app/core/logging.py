"""
日志配置
"""
import logging
import logging.config
import sys
import os
from pathlib import Path

from app.core.config import settings


class SafeStreamHandler(logging.StreamHandler):
    """
    能够优雅处理编码错误的StreamHandler
    """
    def emit(self, record):
        try:
            super().emit(record)
        except UnicodeEncodeError:
            # 如果编码失败，替换有问题的字符
            try:
                msg = self.format(record)
                # 用安全的替代字符替换有问题的Unicode字符
                safe_msg = msg.encode('utf-8', errors='replace').decode('utf-8')
                self.stream.write(safe_msg + self.terminator)
                self.flush()
            except Exception:
                # 最后的手段：只打印一个简单的错误消息
                self.stream.write(f"[编码错误] 日志消息无法显示\n")
                self.flush()


def setup_logging() -> None:
    """
    设置应用程序日志配置
    """
    # 如果日志目录不存在则创建
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    # 如果可能，将控制台编码设置为UTF-8（Windows）
    if os.name == 'nt':  # Windows
        try:
            # 尝试将控制台设置为UTF-8
            os.system('chcp 65001 >nul 2>&1')
        except:
            pass

    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": settings.LOG_FORMAT,
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "detailed": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(module)s - %(funcName)s - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "()": SafeStreamHandler,
                "level": settings.LOG_LEVEL,
                "formatter": "default",
                "stream": sys.stdout,
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": settings.LOG_LEVEL,
                "formatter": "detailed",
                "filename": "logs/hr_agent.log",
                "maxBytes": 10485760,  # 10MB
                "backupCount": 5,
                "encoding": "utf-8",
            },
            "error_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "ERROR",
                "formatter": "detailed",
                "filename": "logs/hr_agent_error.log",
                "maxBytes": 10485760,  # 10MB
                "backupCount": 5,
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "": {  # 根日志记录器
                "level": settings.LOG_LEVEL,
                "handlers": ["console", "file", "error_file"],
                "propagate": False,
            },
            "uvicorn": {
                "level": "INFO",
                "handlers": ["console"],
                "propagate": False,
            },
            "sqlalchemy": {
                "level": "WARNING",
                "handlers": ["file"],
                "propagate": False,
            },
        },
    }

    logging.config.dictConfig(logging_config)
    logger = logging.getLogger(__name__)
    logger.info("日志配置设置完成")


# 创建全局日志记录器实例
logger = logging.getLogger(__name__)