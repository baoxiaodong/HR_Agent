"""
所有 SQLAlchemy 业务模型的公共基类。

具体模型继承 ``BaseModel`` 后会自动获得 UUID 主键、创建/更新时间、软启用标记和审计人
字段；``__abstract__`` 表示基类自身不创建数据库表。模型只描述持久化结构，输入校验和
对外响应结构由 ``app.schemas`` 中的 Pydantic 模型负责。
"""
import uuid
from datetime import datetime
from typing import Any, Dict
from sqlalchemy import Column, DateTime, String, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.declarative import declared_attr

from app.core.database import Base


class BaseModel(Base):
    """为所有业务表提供 UUID、审计时间和软启用字段。"""
    
    __abstract__ = True
    
    # Generate __tablename__ automatically
    @declared_attr
    def __tablename__(cls) -> str:
        return cls.__name__.lower()
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(UUID(as_uuid=True), nullable=True)
    updated_by = Column(UUID(as_uuid=True), nullable=True)
    
    def to_dict(self) -> Dict[str, Any]:
        """把当前表的列值复制为字典，不递归加载 ORM 关系。"""
        return {
            column.name: getattr(self, column.name)
            for column in self.__table__.columns
        }
    
    def update_from_dict(self, data: Dict[str, Any]) -> None:
        """把字典中模型已声明的属性写回对象，并刷新内存中的更新时间。

        方法只改变 ORM 对象状态，不执行 ``flush`` 或 ``commit``；未知键会被忽略，事务仍
        由调用它的服务层负责。
        """
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)
        self.updated_at = datetime.utcnow()