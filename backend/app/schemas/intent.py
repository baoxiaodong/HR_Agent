"""
意图路由及自然语言需求解析的传输模型。

路由模型返回前端目标页面；招聘需求和试卷需求响应把模型/规则提取的自由文本转换为表单
可直接使用的结构化字段。``KnowledgeFileInfo`` 表示试卷生成时自动匹配到的参考文档。
"""
from pydantic import BaseModel
from typing import Any, Optional, Dict, List

class IntentRouteRequest(BaseModel):
    """前端提交的原始自然语言，只在服务层做意图分类。"""

    query: str


class IntentRouteResponse(BaseModel):
    """分类完成后的导航结果；前端根据 ``route`` 跳转到对应功能页。"""

    intent: str
    route: str
    query: str = ""


class RequirementParseRequest(BaseModel):
    """招聘需求解析输入；conversation_id 用于把连续解析关联到同一上下文。"""

    text: str
    conversation_id: Optional[str] = None


class RequirementParseResponse(BaseModel):
    """
    大模型或本地规则提取出的招聘表单字段。

    字段允许为空，因为用户的自然语言通常只包含部分要求；前端可让用户补全缺失项。
    """

    # 岗位基本信息
    job_title: Optional[str] = None
    department: Optional[str] = None
    location: Optional[str] = None
    salary: Optional[str] = None

    # 任职条件
    experience: Optional[str] = None
    education: Optional[str] = None
    job_type: Optional[str] = None
    skills: Optional[List[str]] = None

    # 补充待遇与自由文本要求
    benefits: Optional[List[str]] = None
    additional_requirements: Optional[str] = None


class ExamIntentParseRequest(BaseModel):
    """试卷需求解析输入，文本可包含题型、题量、总分、难度和时长。"""

    text: str
    conversation_id: Optional[str] = None


class KnowledgeFileInfo(BaseModel):
    """自动选中的参考文档；id 供后端检索，fileName 供前端展示。"""

    id: str
    fileName: Optional[str] = None


class ExamIntentParseResponse(BaseModel):
    """解析后可直接回填到试卷生成表单的数据。"""

    # 试卷基本配置；未识别时使用产品默认值。
    title: Optional[str] = None
    subject: Optional[str] = None
    total_score: Optional[int] = 100
    difficulty: Optional[str] = "medium"  # 可选值：easy、medium、hard
    duration: Optional[int] = 90

    # question_counts 的键是题型，值是该题型数量。
    question_counts: Dict[str, int] = {}
    special_requirements: Optional[str] = None

    # 知识库选择服务可在解析阶段自动补充参考文档。
    knowledge_files: List[KnowledgeFileInfo] = []