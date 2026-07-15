"""
HR Agent 的请求、执行步骤和产物响应模型。

请求可携带自然语言、会话、附件元信息及用户确认后的结构化需求；响应把 Agent 状态拆为
可展示消息、识别意图、执行步骤、业务产物和后续建议。这里只描述数据协议，工具规划和
状态推进由 ``AgentService`` 完成。
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AgentAttachment(BaseModel):
    """附件的描述信息；这里只传元数据，二进制内容由专用上传接口处理。"""

    name: str
    size: Optional[int] = None
    content_type: Optional[str] = None


class AgentChatRequest(BaseModel):
    """
    发送给 Agent 编排器的一次用户请求。

    首轮通常只有 message；需要补充信息时，前端再次提交 confirmed_requirements；
    conversation_id 让服务读取历史，auto_execute 决定低风险工具是否可直接运行。
    """

    # 用户当前输入，是意图识别与工具规划的起点。
    message: str = Field(..., min_length=1, description="用户自然语言需求")

    # 可选上下文：会话历史、执行策略、已确认参数及附件线索。
    conversation_id: Optional[str] = None
    auto_execute: bool = Field(True, description="是否自动执行低风险生成类工具")
    confirmed_requirements: Optional[Dict[str, Any]] = Field(None, description="用户确认后的结构化招聘需求")
    attachments: List[AgentAttachment] = Field(default_factory=list, description="聊天消息中携带的附件元信息")


class AgentStep(BaseModel):
    """前端进度条中的一个步骤；status 表示等待、执行、完成或失败。"""

    id: str
    title: str
    status: str
    detail: Optional[str] = None
    tool: Optional[str] = None


class AgentArtifact(BaseModel):
    """工具执行后产生的业务结果，例如 JD、评分标准、面试计划或试卷。"""

    type: str
    title: str
    content: Any
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AgentChatResponse(BaseModel):
    """
    Agent 完成一轮规划或执行后返回给前端的统一快照。

    requires_confirmation 为真时，前端应展示 missing_fields 并等待用户补充；否则可展示
    steps 和 artifacts。suggestions 是下一步操作提示，不属于已经执行的结果。
    """

    # 对话展示信息与路由判断。
    message: str
    intent: str
    route: Optional[str] = None

    # 执行过程、产物及后续建议。
    steps: List[AgentStep] = Field(default_factory=list)
    artifacts: List[AgentArtifact] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)

    # 需求不完整时使用的交互状态。
    requires_confirmation: bool = False
    missing_fields: List[str] = Field(default_factory=list)
