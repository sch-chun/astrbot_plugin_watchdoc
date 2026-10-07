"""LLM 总结（单次调用 / agent 循环）与原文查阅工具。"""

from __future__ import annotations

from astrbot.api import FunctionTool, ToolSet, logger
from astrbot.api.star import Context
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.platform.message_session import MessageSession

from .constants import (
    AGENT_INSTRUCTION,
    DEFAULT_INSTRUCTION,
    DOC_TOOL_NAMES,
    TOOL_READ_CHARS,
    TOOL_READ_DEFAULT_LINES,
    TOOL_READ_MAX_LINES,
    TOOL_SEARCH_CONTEXT_LINES,
    TOOL_SEARCH_MAX_HITS,
)

ROLE_PROMPT = "你是 API 文档变更分析助手。"


def default_instruction(agent: bool = False) -> str:
    """取内置默认任务指令。

    Args:
        agent: 是否走 agent 循环，决定用哪条默认指令。

    Returns:
        默认指令文本。
    """
    return AGENT_INSTRUCTION if agent else DEFAULT_INSTRUCTION


def task_instruction(target: dict, agent: bool = False) -> str:
    """取监控项的任务指令，未配置时回落到内置默认。

    Args:
        target: 监控项配置，可带 instruction 字段。
        agent: 是否走 agent 循环，决定用哪条默认指令。

    Returns:
        任务指令文本。
    """
    custom = str(target.get("instruction") or "").strip()
    if custom:
        return custom
    return default_instruction(agent)


def _system_prompt(persona_prompt: str) -> str:
    """组装 system prompt。

    有人格时只放人格小节，与主 Agent 的注入结果逐字一致，使前缀能被提示缓存命中；
    无人格时没有可共享的前缀，用角色设定兜住输出风格。
    任务约束一律放 user 轮，不占用可缓存的前缀。
    """
    if persona_prompt:
        return f"\n# Persona Instructions\n\n{persona_prompt}\n"
    return ROLE_PROMPT


def get_provider(context: Context, provider_id: str):
    """获取用于总结的模型 Provider。

    Args:
        context: 插件上下文。
        provider_id: 配置的模型 ID，为空则用默认对话模型。

    Returns:
        Provider 实例，未配置且无可用模型时返回 None。
    """
    if provider_id:
        provider = context.get_provider_by_id(provider_id)
        if provider:
            return provider
        logger.warning(f"[watchdoc] 未找到指定模型 {provider_id}，改用当前会话模型")
    return context.get_using_provider()


async def summarize(provider, target: dict, diff: str, persona_prompt: str = "") -> str:
    """调用 LLM 总结 diff。

    Args:
        provider: 模型 Provider，为 None 时返回原始 diff。
        target: 监控项配置。
        diff: unified diff 文本。
        persona_prompt: 会话人格的 system prompt，非空时追加到系统提示。

    Returns:
        总结文本。调用失败时返回带说明的原文片段。
    """
    if provider is None:
        return f"（未获取到 LLM Provider，以下为原始 diff）\n\n{diff}"

    prompt = (
        f"文档名称：{target.get('name') or target.get('id')}\n"
        f"文档地址：{target.get('url')}\n\n"
        f"{task_instruction(target)}\n\n"
        f"以下是本次抓取与上次快照之间的 unified diff：\n\n{diff}"
    )
    system_prompt = _system_prompt(persona_prompt)
    try:
        resp = await provider.text_chat(
            prompt=prompt,
            session_id=f"astrbot_plugin_watchdoc:{target.get('id')}",
            system_prompt=system_prompt,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[watchdoc] 调用 LLM 失败: {exc}")
        return f"（AI 总结失败：{exc}）\n\n原始 diff:\n{diff}"

    return (resp.completion_text or "").strip() or "（LLM 返回空内容）"


async def summarize_with_agent(
    context: Context,
    target: dict,
    change: dict,
    group: dict,
    provider_id: str,
    tools: ToolSet,
    max_steps: int,
) -> str:
    """用 agent 循环生成总结，模型可自行调用工具查阅原文。

    Args:
        context: 插件上下文。
        target: 监控项配置。
        change: 含 before / after / diff 的变更数据。
        group: 人格分组。
        provider_id: 模型 ID。
        tools: 可用工具集。
        max_steps: 最大工具调用轮次。

    Returns:
        总结文本。

    Raises:
        RuntimeError: agent 返回错误态或没有产出内容。
    """
    umo = group["umos"][0]
    session = MessageSession.from_str(umo)
    event = CronMessageEvent(
        context=context,
        session=session,
        message="请分析本次文档变更。",
        message_type=session.message_type,
    )
    # 不按插件白名单过滤，本插件的查阅工具必须可见
    event.plugins_name = None

    prompt = (
        f"文档名称：{target.get('name') or target.get('id')}\n"
        f"文档地址：{target.get('url')}\n\n"
        f"{task_instruction(target, agent=True)}\n\n"
        "diff 的 @@ 行号对应原文行号，需要更多上下文时可用 "
        f"{DOC_TOOL_NAMES[0]} 按 start_line / line_count 读取指定行区间，"
        f"或用 {DOC_TOOL_NAMES[1]} 搜索关键词定位。\n\n"
        f"以下是本次抓取与上次快照之间的 unified diff：\n\n{change['diff']}\n\n"
        # 行数是动态的，放最后才不会截断可缓存的前缀
        f"变更前原文 {len(change['before'].splitlines())} 行，"
        f"变更后 {len(change['after'].splitlines())} 行。"
    )
    system_prompt = _system_prompt(group["prompt"])

    resp = await context.tool_loop_agent(
        event=event,
        chat_provider_id=provider_id,
        prompt=prompt,
        system_prompt=system_prompt,
        tools=tools,
        max_steps=max_steps,
    )

    # 出错时 tool_loop_agent 不抛异常，只返回 role=err 的响应
    if resp.role == "err":
        raise RuntimeError(resp.completion_text or "agent 返回错误态")
    text = (resp.completion_text or "").strip()
    if not text:
        raise RuntimeError("agent 未产出内容")
    return text


def build_tool_set(
    context: Context,
    persona_tools: list[str] | None,
    extra_tools: list[FunctionTool] | None = None,
) -> ToolSet:
    """按人格配置组装可用工具集。

    Args:
        context: 插件上下文。
        persona_tools: 人格的工具名单。None 表示不限，空列表表示禁用全部。
        extra_tools: 无条件追加的工具。人格的白名单通常不会写本插件的
            查阅工具，不追加的话模型就查不到原文了。

    Returns:
        组装好的工具集。
    """
    manager = context.get_llm_tool_manager()
    if persona_tools is None:
        tools = manager.get_full_tool_set()
        for tool in list(tools):
            if not tool.active:
                tools.remove_tool(tool.name)
    else:
        tools = ToolSet()
        for name in persona_tools:
            tool = manager.get_func(name)
            if tool and tool.active:
                tools.add_tool(tool)
    for tool in extra_tools or []:
        tools.add_tool(tool)
    return tools


def doc_tools(read_handler, search_handler) -> list[FunctionTool]:
    """构造查阅本次变更原文的工具。

    Args:
        read_handler: 按行读取的处理函数。
        search_handler: 搜索关键词的处理函数。

    Returns:
        工具列表。只在 agent 循环里可见，不注册到全局工具集。
    """
    scope = {"type": "string", "description": "before 表示变更前，after 表示变更后"}
    return [
        FunctionTool(
            name=DOC_TOOL_NAMES[0],
            description=(
                "按行号区间读取本次文档变更前/后的原文，返回内容带行号。"
                "diff 里的 @@ 行号可据此定位到原文上下文。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scope": scope,
                    "start_line": {
                        "type": "number",
                        "description": "起始行号，从 1 开始，默认 1",
                    },
                    "line_count": {
                        "type": "number",
                        "description": (
                            f"读取的行数，默认 {TOOL_READ_DEFAULT_LINES}，"
                            f"最多 {TOOL_READ_MAX_LINES}"
                        ),
                    },
                },
                "required": ["scope"],
            },
            handler=read_handler,
        ),
        FunctionTool(
            name=DOC_TOOL_NAMES[1],
            description=(
                "在本次文档变更前/后的原文中搜索关键词，"
                "返回命中行及前后若干行，带行号。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要搜索的关键词"},
                    "scope": scope,
                },
                "required": ["keyword", "scope"],
            },
            handler=search_handler,
        ),
    ]


def read_lines(
    run: dict,
    scope: str,
    start_line: int = 1,
    line_count: int = TOOL_READ_DEFAULT_LINES,
) -> str:
    """按行号区间读取原文。

    返回内容带行号，便于和 diff 里的 hunk 行号对照定位。

    Args:
        run: 本次变更数据，需含 before / after。
        scope: before 或 after。
        start_line: 起始行号，从 1 开始。
        line_count: 读取行数。

    Returns:
        带行号的原文片段。
    """
    key = "before" if scope == "before" else "after"
    label = "变更前原文" if key == "before" else "变更后原文"
    lines = (run.get(key) or "").splitlines()
    if not lines:
        return f"{label}为空。"
    total = len(lines)
    start = min(max(int(start_line), 1), total)
    limit = min(max(int(line_count), 1), TOOL_READ_MAX_LINES)
    # 行内可能极长（压缩过的 HTML 等），用字符预算兜底，避免一次塞爆上下文
    parts = []
    used = 0
    end = start - 1
    for offset in range(start - 1, min(total, start - 1 + limit)):
        line = f"{offset + 1}: {lines[offset]}"
        if parts and used + len(line) > TOOL_READ_CHARS:
            break
        parts.append(line)
        used += len(line) + 1
        end = offset + 1

    head = f"{label}共 {total} 行，以下是第 {start}-{end} 行：\n\n"
    tip = ""
    if end < total:
        tip = (
            f"\n\n（到第 {end} 行为止，"
            f"继续读请传 start_line={end + 1}；"
            f"单次最多 {TOOL_READ_MAX_LINES} 行）"
        )
    return head + "\n".join(parts) + tip


def search_lines(run: dict, keyword: str, scope: str) -> str:
    """在原文中搜索关键词，返回命中行及其前后的若干行。

    Args:
        run: 本次变更数据，需含 before / after。
        keyword: 搜索关键词。
        scope: before 或 after。

    Returns:
        带行号的命中片段，未命中时给出说明。
    """
    lines = (run.get("before" if scope == "before" else "after") or "").splitlines()
    if not keyword or not lines:
        return "没有可搜索的内容，或关键词为空。"
    hits = []
    for offset, line in enumerate(lines):
        if len(hits) >= TOOL_SEARCH_MAX_HITS:
            break
        if keyword not in line:
            continue
        begin = max(0, offset - TOOL_SEARCH_CONTEXT_LINES)
        end = min(len(lines), offset + TOOL_SEARCH_CONTEXT_LINES + 1)
        hits.append(
            "\n".join(f"{index + 1}: {lines[index]}" for index in range(begin, end))
        )
    if not hits:
        return f"未找到「{keyword}」。"
    return (
        f"找到 {len(hits)} 处「{keyword}」，"
        "以下是各命中处及其前后原文：\n\n" + "\n\n---\n\n".join(hits)
    )
