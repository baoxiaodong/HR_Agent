"""
Pydantic 请求与响应模型包。

Schema 位于 HTTP/业务边界，负责字段类型、必填项和 JSON 序列化；它们不替代服务层的
权限、资源归属和跨服务可信候选校验。
"""

from .user import *
from .conversation import *
from .document import *
from .knowledge_base import *
from .chat import *