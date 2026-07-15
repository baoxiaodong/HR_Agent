"""职位描述在生成、发布和归档流程中的状态枚举。"""
import enum

class JDStatus(str, enum.Enum):
    """职位描述状态枚举"""
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"