"""监控项持久化与变更存档。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import Context

from .constants import MAX_HISTORY_PER_TARGET
from .text import safe_name


async def append_to_history(
    context: Context, umo: str, title: str, url: str, text: str
) -> None:
    """把推送内容追加到会话的对话历史，让会话里能回溯这次变更。

    按 user / assistant 成对写入：只写一条 assistant 会让从未说过话的会话
    以助手消息开头，部分模型不接受这种开头。user 侧是合成的系统性说明，
    与上游 `persist_agent_history` 的做法一致。

    Args:
        context: 插件上下文。
        umo: 会话标识。
        title: 监控项名称。
        url: 文档地址。
        text: 推送正文。
    """
    manager = context.conversation_manager
    if manager is None:
        return
    # 该会话还没和机器人说过话时没有当前对话，建一个再写
    cid = await manager.get_curr_conversation_id(umo) or await manager.new_conversation(
        umo
    )
    conv = await context.get_db().get_conversation_by_id(cid=cid)
    history = list((conv.content if conv else None) or [])
    history.append(
        {"role": "user", "content": f"（Watchdoc）请总结 {title} 的本次变更。\n{url}"}
    )
    history.append({"role": "assistant", "content": text})
    await manager.update_conversation(umo, cid, history=history)


def normalize_targets(data: object) -> list[dict]:
    """把 KV 里取出的原始监控项收敛成下游可依赖的形状。

    Args:
        data: 原始值，可能是列表、JSON 文本或其它类型。

    Returns:
        监控项列表。不是列表或不是合法 JSON 时返回空列表。
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError) as exc:
            logger.error(f"[watchdoc] 监控项配置不是合法 JSON: {exc}")
            return []
    if not isinstance(data, list):
        return []
    for item in data:
        if not isinstance(item, dict) or "sessions" not in item:
            # 缺字段等价于空列表，不硬塞字段进去改写用户的配置
            continue
        # sessions 由 Page 填写，也可能是手改出来的字符串；统一收敛成
        # 去重后的非空列表，下游就不必各自防御了
        raw_sessions = item.get("sessions")
        values = (
            raw_sessions
            if isinstance(raw_sessions, list)
            else ([raw_sessions] if isinstance(raw_sessions, str) else [])
        )
        sessions = []
        for value in values:
            text = str(value).strip()
            if text and text not in sessions:
                sessions.append(text)
        item["sessions"] = sessions
    return data


def archive(
    history_dir: Path,
    target: dict,
    diff: str,
    deliveries: list[tuple[str, list[str]]],
) -> None:
    """把本次变更存档到插件数据目录。

    Args:
        history_dir: 存档根目录。
        target: 监控项配置。
        diff: unified diff 文本。
        deliveries: (总结, 会话列表) 的列表，多份时只存档第一份。
    """
    summary = deliveries[0][0] if deliveries else ""
    # 多份总结只在文风上有差别，变更事实一致，因此只留一份并标注份数
    persona_note = ""
    if len(deliveries) > 1:
        persona_note = f"- 人格: 按 {len(deliveries)} 种人格分别生成，此处仅存第一份\n"
    folder = history_dir / safe_name(str(target.get("id") or "target"))
    folder.mkdir(parents=True, exist_ok=True)
    # 文件名带微秒：秒级精度会让同一秒内的多次变更互相覆盖
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    (folder / f"{stamp}.md").write_text(
        f"# {target.get('name') or target.get('id')}\n\n"
        f"- 地址: {target.get('url')}\n"
        f"- 时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"{persona_note}\n"
        f"## AI 总结\n\n{summary}\n\n"
        f"## Diff\n\n```diff\n{diff}\n```\n",
        encoding="utf-8",
    )
    # 页面选择器失效时可能每次都判定为变更，限制存档数量避免无限增长
    files = sorted(folder.glob("*.md"))
    for old in files[:-MAX_HISTORY_PER_TARGET]:
        old.unlink(missing_ok=True)
