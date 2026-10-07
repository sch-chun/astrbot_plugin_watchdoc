"""会话人格解析与分组。"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.star import Context


async def resolve(umo: str, context: Context) -> dict | None:
    """解析会话当前生效的人格。

    Args:
        umo: 会话标识。
        context: 插件上下文。

    Returns:
        人格对象；会话不存在或解析失败时返回 None。
    """
    persona_manager = getattr(context, "persona_manager", None)
    if persona_manager is None:
        return None

    persona_id = None
    try:
        db = context.get_db()
        conversations = await db.get_conversations(user_id=umo)
        if conversations:
            persona_id = conversations[0].persona_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[watchdoc] 读取会话 {umo} 的人格失败: {exc}")

    try:
        _, persona, _, _ = await persona_manager.resolve_selected_persona(
            umo=umo,
            conversation_persona_id=persona_id,
            platform_name=umo.split(":", 1)[0],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[watchdoc] 解析会话 {umo} 的人格失败: {exc}")
        return None

    return persona or None


async def group_sessions(
    context: Context, umos: list[str], enabled: bool
) -> list[dict]:
    """按会话人格把推送目标分组。

    Args:
        context: 插件上下文。
        umos: 会话列表。
        enabled: 是否启用人格；关闭时合并为单一分组。

    Returns:
        分组列表，每项含 prompt、tools、umos。
    """
    if not umos or not enabled:
        return [{"prompt": "", "tools": None, "umos": umos}]

    groups: dict[str, dict] = {}
    for umo in umos:
        persona = await resolve(umo, context)
        prompt = (persona or {}).get("prompt") or ""
        tools = (persona or {}).get("tools", None)
        group = groups.setdefault(
            prompt, {"prompt": prompt, "tools": tools, "umos": []}
        )
        group["umos"].append(umo)
        # 同一个人格名下的工具配置应当一致，出现分歧时按"取到就放宽"处理
        if tools is None:
            group["tools"] = None
    return list(groups.values())
