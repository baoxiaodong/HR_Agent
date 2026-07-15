"""
HR Agent 的 Skill 加载与调度组件。

每个 Skill 目录使用 ``SKILL.md`` 提供说明和基础元数据，使用 ``skill.json``
manifest 声明意图、阶段及阶段脚本映射。阶段脚本由 ``ScriptedSkillPhase``
动态加载，``AgentSkillBundle`` 组织同一 Skill 的阶段并解析当前阶段，dispatcher
按意图定位 bundle，也可用确认动作反查对应 bundle。
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str
    intent: str
    route: Optional[str]
    prerequisites: tuple[str, ...]
    phases: tuple[str, ...]
    default_phase: str
    confirmation_action: Optional[str] = None


@dataclass(frozen=True)
class SkillPhaseDefinition:
    phase_name: str
    script_path: Path
    function_name: str


class ScriptedSkillPhase:
    """技能阶段由技能包中的Python脚本执行."""

    def __init__(self, skill_dir: Path, definition: SkillPhaseDefinition):
        self.skill_dir = skill_dir
        self.definition = definition

    @property
    def skill_markdown_path(self) -> Path:
        return self.skill_dir / "SKILL.md"

    def load_skill_instructions(self) -> str:
        try:
            return self.skill_markdown_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            logger.warning("Skill 文件不存在: %s", self.skill_markdown_path)
            return ""

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        # 模块名只用于本次动态加载标识；真正执行文件由 manifest 中解析出的 script_path 决定。
        module_name = f"skill_{self.skill_dir.name}_{self.definition.phase_name}".replace("-", "_")
        spec = importlib.util.spec_from_file_location(module_name, self.definition.script_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载 skill 脚本: {self.definition.script_path}")

        # 加载阶段会执行脚本模块顶层代码，因此 Skill 目录属于受信任的本地扩展边界，而非普通数据文件。
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn = getattr(module, self.definition.function_name, None)
        if fn is None:
            raise RuntimeError(f"skill 脚本缺少函数 {self.definition.function_name}: {self.definition.script_path}")

        # 运行上下文在原调用参数基础上附加 Skill 路径、说明文本和当前阶段，供脚本统一读取。
        enriched_context = {
            **context,
            "skill_dir": self.skill_dir,
            "skill_markdown": self.load_skill_instructions(),
            "phase_name": self.definition.phase_name,
        }

        # 阶段函数可以同步返回字典，也可以返回 awaitable；调度器在这里统一成异步结果。
        result = fn(enriched_context)
        if inspect.isawaitable(result):
            result = await result
        # dict 是 Agent 编排层约定的唯一阶段输出形状，防止任意对象继续向响应层传播。
        if not isinstance(result, dict):
            raise RuntimeError(f"skill phase 必须返回 dict: {self.definition.script_path}#{self.definition.function_name}")
        return result


@dataclass
class AgentSkillBundle:
    intent: str
    bundle_name: str
    skill_dir: Path
    metadata: SkillMetadata
    phases: dict[str, ScriptedSkillPhase] = field(default_factory=dict)

    def get_phase(self, phase: str) -> ScriptedSkillPhase:
        if phase not in self.phases:
            raise KeyError(f"Skill bundle {self.bundle_name} 未注册 phase: {phase}")
        return self.phases[phase]

    def resolve_phase(self, confirmed_requirements: Optional[dict[str, Any]] = None) -> str:
        # 只有 manifest 声明的确认动作精确匹配且存在 send 阶段时才切换；
        # 其余请求统一回到默认阶段，避免把普通确认误当成发送动作。
        action = str((confirmed_requirements or {}).get("action") or "").strip()
        if self.metadata.confirmation_action and action == self.metadata.confirmation_action and "send" in self.phases:
            return "send"
        return self.metadata.default_phase


class AgentSkillDispatcher:
    """根据 intent 和 phase 分发到 skill bundle。"""

    def __init__(self, bundles: dict[str, AgentSkillBundle]):
        self.bundles = bundles

    def get_bundle(self, intent: str) -> AgentSkillBundle:
        if intent not in self.bundles:
            raise KeyError(f"未找到 intent 对应的 skill bundle: {intent}")
        return self.bundles[intent]

    def dispatch(self, intent: str, phase: str) -> ScriptedSkillPhase:
        """按意图和阶段返回已注册的脚本阶段；任一键不存在时抛出 ``KeyError``。"""
        return self.get_bundle(intent).get_phase(phase)

    def match_confirmation_action(self, action: str) -> Optional[AgentSkillBundle]:
        # 确认动作按 manifest 中的字符串精确匹配，命中后由 bundle 决定实际阶段。
        for bundle in self.bundles.values():
            if bundle.metadata.confirmation_action == action:
                return bundle
        return None


def parse_skill_metadata(skill_markdown_path: Path) -> Optional[dict[str, str]]:
    """读取 ``SKILL.md`` 的简易 frontmatter；文件缺失或格式无效时返回 ``None``。"""
    try:
        content = skill_markdown_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None

    if not content.startswith("---\n"):
        return None
    parts = content.split("\n---\n", 1)
    if len(parts) != 2:
        return None
    frontmatter = parts[0].removeprefix("---\n")

    data: dict[str, str] = {}
    for line in frontmatter.splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        data[key.strip()] = value.strip()

    name = data.get("name", "").strip()
    description = data.get("description", "").strip()
    if not name or not description:
        return None
    return {"name": name, "description": description}


def parse_skill_manifest(skill_dir: Path) -> Optional[SkillMetadata]:
    """解析 ``skill.json`` 基础字段并补充说明。

    不验证 ``default_phase`` 是否属于 ``phases``，也不验证阶段脚本声明是否完整。
    """
    manifest_path = skill_dir / "skill.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        logger.warning("Skill manifest 解析失败: %s", exc)
        return None

    name = str(data.get("name") or skill_dir.name).strip()
    intent = str(data.get("intent") or "").strip()
    route = str(data.get("route") or "").strip() or None
    phases = tuple(str(item).strip() for item in data.get("phases") or [] if str(item).strip())
    prerequisites = tuple(str(item).strip() for item in data.get("prerequisites") or [] if str(item).strip())
    default_phase = str(data.get("default_phase") or "").strip() or (phases[0] if phases else "")
    confirmation_action = str(data.get("confirmation_action") or "").strip() or None
    phase_scripts_raw = data.get("phase_scripts") or {}
    if not name or not intent or not phases or not default_phase or not isinstance(phase_scripts_raw, dict):
        return None

    description = ""
    skill_md = parse_skill_metadata(skill_dir / "SKILL.md")
    if skill_md:
        description = skill_md["description"]
    return SkillMetadata(
        name=name,
        description=description,
        intent=intent,
        route=route,
        prerequisites=prerequisites,
        phases=phases,
        default_phase=default_phase,
        confirmation_action=confirmation_action,
    )


def build_skill_bundle_from_directory(skill_dir: Path) -> Optional[AgentSkillBundle]:
    """从 Skill 目录构建阶段集合；跳过无脚本映射的阶段，无可用阶段时返回 ``None``。"""
    metadata = parse_skill_manifest(skill_dir)
    if not metadata:
        return None

    phases: dict[str, ScriptedSkillPhase] = {}
    # manifest 先给出阶段顺序；缺少或无法解析映射的阶段会被跳过，不阻断其他阶段。
    for phase in metadata.phases:
        phase_spec = parse_phase_script_spec(skill_dir, phase)
        if not phase_spec:
            logger.warning("Skill %s 缺少 phase 脚本定义: %s", metadata.name, phase)
            continue
        phases[phase] = ScriptedSkillPhase(skill_dir=skill_dir, definition=phase_spec)

    if not phases:
        return None
    return AgentSkillBundle(
        intent=metadata.intent,
        bundle_name=metadata.name,
        skill_dir=skill_dir,
        metadata=metadata,
        phases=phases,
    )


def parse_phase_script_spec(skill_dir: Path, phase: str) -> Optional[SkillPhaseDefinition]:
    """解析指定阶段的 ``相对脚本路径:函数名`` 映射。

    只检查映射非空且包含冒号，不完整验证脚本路径、函数名或阶段一致性。
    """
    manifest_path = skill_dir / "skill.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None

    phase_scripts = data.get("phase_scripts") or {}
    raw = str(phase_scripts.get(phase) or "").strip()
    if not raw or ":" not in raw:
        return None
    rel_path, function_name = raw.split(":", 1)
    # 当前只做路径拼接，没有 resolve 后验证脚本仍位于 skill_dir 内；manifest 被视为受信任本地配置。
    return SkillPhaseDefinition(
        phase_name=phase,
        script_path=skill_dir / rel_path.strip(),
        function_name=function_name.strip(),
    )


def build_default_skill_dispatcher(skills_root: Optional[Path] = None) -> AgentSkillDispatcher:
    """扫描根目录的直接子目录构建 dispatcher；目录不存在时返回空调度器。"""
    root = Path(skills_root) if skills_root else Path(__file__).resolve().parents[2] / "skills"
    bundles: dict[str, AgentSkillBundle] = {}
    if not root.exists():
        return AgentSkillDispatcher(bundles)

    for skill_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        bundle = build_skill_bundle_from_directory(skill_dir)
        if not bundle:
            continue
        # dispatcher 以 intent 为唯一键；多个目录声明同一 intent 时，按目录排序靠后的 bundle 覆盖前者。
        bundles[bundle.intent] = bundle
    return AgentSkillDispatcher(bundles)
