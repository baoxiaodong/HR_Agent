"""简历评分标准在草稿、启用和归档流程中的状态枚举。"""
import enum

class ScoringStatus(str, enum.Enum):
    """评分标准状态枚举"""
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"