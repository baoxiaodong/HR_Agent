import enum

class JDStatus(str, enum.Enum):
    """职位描述状态枚举"""
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"