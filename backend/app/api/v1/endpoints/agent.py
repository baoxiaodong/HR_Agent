"""
统一 HR Agent API 入口。

端点层负责认证、请求格式校验、上传文件读取以及 SSE 事件编码；任务识别、需求确认、工具
规划和各招聘工作流编排全部由 ``AgentService`` 完成。专用流式接口分别覆盖招聘流程、
批量简历筛选、面试计划和试卷生成，并以 ``[DONE]`` 标记事件流结束。
"""
import json
from typing import Any, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.schemas.agent import AgentChatRequest
from app.schemas.user import User as UserSchema
from app.services.agent_service import AgentService

router = APIRouter()


@router.post("/chat")
async def chat_with_agent(
    request: AgentChatRequest,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """把已校验请求转换为 Agent 服务输入并返回普通 JSON 响应。

    ``current_user.id`` 来自 JWT 依赖，是所有候选查询和资源写入的用户边界；附件 Schema 在
    端点转为普通字典，服务层再规范化。Agent 返回 Pydantic 响应后转为 JSON 友好字典。
    当前兜底会把服务层未处理异常统一包装为 500。
    """
    try:
        agent_service = AgentService(db)
        # 请求体不能覆盖 user_id；确认数据、附件和会话 ID 只作为编排输入继续下传。
        response = await agent_service.chat(
            message=request.message.strip(),
            user_id=current_user.id,
            conversation_id=request.conversation_id,
            auto_execute=request.auto_execute,
            confirmed_requirements=request.confirmed_requirements,
            attachments=[item.model_dump() for item in request.attachments],
        )
        return response.model_dump()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"HR Agent 执行失败: {str(exc)}",
        ) from exc


@router.post("/chat/stream")
async def stream_chat_with_agent(
    request: AgentChatRequest,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """把 Agent 的内部事件字典编码为 SSE 数据帧。

    认证和请求 Schema 校验在创建响应前完成；生成器运行期间复用本请求数据库会话。正常结束
    发送 ``[DONE]``，流中异常因响应头已发出而转换为 error 事件，不能再改 HTTP 状态码。
    """

    async def generate():
        try:
            agent_service = AgentService(db)
            # 每个服务事件先序列化为 JSON，再按 SSE 的 data + 空行协议输出。
            async for event in agent_service.stream_chat_agent(
                message=request.message.strip(),
                user_id=current_user.id,
                conversation_id=request.conversation_id,
                auto_execute=request.auto_execute,
                confirmed_requirements=request.confirmed_requirements,
                attachments=[item.model_dump() for item in request.attachments],
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            error_event = {"type": "error", "error": f"HR Agent 执行失败: {str(exc)}"}
            yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post("/stream")
async def stream_agent_progress(
    request: AgentChatRequest,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """执行用户已确认的 JD 与评分标准生成链，并流式汇报进度。

    该专用端点不再负责需求收集；缺少 ``confirmed_requirements`` 会在流建立前返回 400。
    认证用户 ID 会随确认字段进入服务层，生成的 JD 与评分标准由各领域服务分别持久化。
    """
    if not request.confirmed_requirements:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="stream 接口需要 confirmed_requirements",
        )

    async def generate():
        try:
            agent_service = AgentService(db)
            async for event in agent_service.stream_recruitment_agent(
                message=request.message.strip(),
                user_id=current_user.id,
                conversation_id=request.conversation_id,
                confirmed_requirements=request.confirmed_requirements,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            error_event = {"type": "error", "error": f"HR Agent 执行失败: {str(exc)}"}
            yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post("/resume-screen/stream")
async def stream_resume_screening(
    job_description_id: UUID = Form(...),
    conversation_id: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """把 multipart 简历批次读入内存，再交给 Agent 逐份评价。

    端点限制文件数量并拒绝空内容；``job_description_id`` 已由 FastAPI 转为 UUID，但目标 JD 的
    存在性和用户归属由评价服务继续校验。每个 UploadFile 转成文件名/字节字典，单份评价失败
    由服务隔离，因此最终事件可能表示部分成功。
    """
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请至少上传一份简历")
    if len(files) > 20:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="单次最多上传 20 份简历")

    # 在建立流之前完成上传文件读取，后续生成器不再依赖 UploadFile 的生命周期。
    file_payloads = []
    for upload_file in files:
        content = await upload_file.read()
        if not content:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{upload_file.filename} 文件内容为空")
        file_payloads.append({"filename": upload_file.filename, "content": content})

    async def generate():
        try:
            agent_service = AgentService(db)
            async for event in agent_service.stream_resume_screening(
                user_id=current_user.id,
                job_description_id=job_description_id,
                files=file_payloads,
                conversation_id=conversation_id,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            error_event = {"type": "error", "error": f"简历批量筛选失败: {str(exc)}"}
            yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.post("/interview-plan/stream")
async def stream_interview_plan(
    resume_evaluation_id: UUID = Form(...),
    conversation_id: Optional[str] = Form(None),
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """根据当前用户拥有的一条简历评价生成面试计划事件流。

    端点只接收已由 FastAPI 解析的评价 UUID；Agent 服务会在同一 SQL 中校验评价记录和用户
    归属，再读取关联 JD、生成远程计划并推进候选状态。流中错误按 error 事件返回。
    """

    async def generate():
        try:
            agent_service = AgentService(db)
            async for event in agent_service.stream_interview_plan(
                user_id=current_user.id,
                resume_evaluation_id=resume_evaluation_id,
                conversation_id=conversation_id,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            error_event = {"type": "error", "error": f"面试计划生成失败: {str(exc)}"}
            yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.post("/exam/stream")
async def stream_exam_generation(
    request: AgentChatRequest,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """用确认后的结构化配置生成并保存试卷。

    缺少确认数据时在流建立前返回 400；服务层负责规范化题量、调用生成工作流、补齐缺题并
    保存试卷。认证用户 ID 会传入生成流程，但试卷领域模型的具体归属能力由 ``ExamService``
    实现决定。
    """
    if not request.confirmed_requirements:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="exam stream 接口需要 confirmed_requirements",
        )

    async def generate():
        try:
            agent_service = AgentService(db)
            async for event in agent_service.stream_exam_generation(
                user_id=current_user.id,
                exam_requirements=request.confirmed_requirements,
                conversation_id=request.conversation_id,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            error_event = {"type": "error", "error": f"考试生成失败: {str(exc)}"}
            yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.post("/exam/document-stream")
async def stream_exam_generation_with_documents(
    exam_requirements: str = Form(...),
    conversation_id: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """解析 multipart 中的考试 JSON 和参考文件，再启动文档出题流。

    配置 JSON 在响应建立前解析，非法输入直接返回 400；文件数量和空内容同样先校验。服务层
    会按当前用户保存/复用附件、提取文本并提交文档记录，再复用试卷生成链，因此文件、文档
    记录和试卷可能形成分阶段成功状态。
    """
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请至少上传一个参考文档")
    if len(files) > 5:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="单次最多上传 5 个参考文档")

    try:
        parsed_requirements = json.loads(exam_requirements)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="考试配置 JSON 格式不正确") from exc

    # 在建立流之前完成上传文件读取，后续生成器不再依赖 UploadFile 的生命周期。
    file_payloads = []
    for upload_file in files:
        content = await upload_file.read()
        if not content:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{upload_file.filename} 文件内容为空")
        file_payloads.append({"filename": upload_file.filename, "content": content})

    async def generate():
        try:
            agent_service = AgentService(db)
            async for event in agent_service.stream_exam_generation_with_documents(
                user_id=current_user.id,
                exam_requirements=parsed_requirements,
                files=file_payloads,
                conversation_id=conversation_id,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            error_event = {"type": "error", "error": f"基于文档生成考试失败: {str(exc)}"}
            yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
