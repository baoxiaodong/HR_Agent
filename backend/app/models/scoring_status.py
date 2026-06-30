import enum

class ScoringStatus(str, enum.Enum):
    """评分标准状态枚举"""
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"