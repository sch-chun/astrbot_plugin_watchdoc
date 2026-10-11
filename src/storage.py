"""监控项持久化与变更存档。"""

from __future__ import annotations

import json
import shutil
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
        if not isinstance(item, dict):
            continue
        # documents：新结构，每项至少含 url；收敛成去重后的非空列表供下游使用
        raw_docs = item.get("documents")
        if isinstance(raw_docs, list):
            docs = []
            for d in raw_docs:
                if not isinstance(d, dict):
                    continue
                url = str(d.get("url") or "").strip()
                if not url:
                    continue
                docs.append(
                    {
                        "url": url,
                        "selector": str(d.get("selector") or ""),
                        "name": str(d.get("name") or "").strip(),
                    }
                )
            item["documents"] = docs
        # sessions 由 Page 填写，也可能是手改出来的字符串；统一收敛成
        # 去重后的非空列表，下游就不必各自防御了
        if "sessions" not in item:
            continue
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


def drop_snapshots(
    snapshot_dir: Path, previous: list[dict], current: list[dict]
) -> None:
    """删除已移除监控项，以及监控项内被删文档的基线快照。

    留着旧快照会让同 ID 的新监控项拿它当基线，第一次检查就误报变更；
    监控项内文档减少时，多余文档的快照也应当清理。变更存档是有意保留
    的记录，不在这里动。

    Args:
        snapshot_dir: 快照目录。
        previous: 保存前的监控项列表。
        current: 保存后的监控项列表。
    """
    kept = {str(item.get("id") or "") for item in current}
    for item in previous:
        tid = str(item.get("id") or "")
        if not tid or tid not in kept:
            # 整个监控项删除：连分组目录一起清，并兼容旧版单文件基线
            shutil.rmtree(snapshot_dir / safe_name(tid), ignore_errors=True)
            (snapshot_dir / f"{safe_name(tid)}.md").unlink(missing_ok=True)
            continue
        # 监控项保留但文档可能减少：裁掉不再监控的文档快照
        cur = next((c for c in current if str(c.get("id") or "") == tid), None)
        kept_slugs = {
            safe_name(str(d.get("url") or "")) for d in (cur.get("documents") or [])
        }
        doc_dir = snapshot_dir / safe_name(tid)
        if doc_dir.is_dir():
            for f in doc_dir.glob("*.md"):
                if f.stem not in kept_slugs:
                    f.unlink(missing_ok=True)


def archive(
    history_dir: Path,
    target: dict,
    change: dict,
    deliveries: list[tuple[str, list[str]]],
) -> None:
    """把本次变更存档到插件数据目录。

    Args:
        history_dir: 存档根目录。
        target: 监控项配置。
        change: 含 docs 的变更数据。
        deliveries: (总结, 会话列表) 的列表，多份时只存档第一份。
    """
    docs = change.get("docs") or []
    summary = deliveries[0][0] if deliveries else ""
    # 多份总结只在文风上有差别，变更事实一致，因此只留一份并标注份数
    persona_note = ""
    if len(deliveries) > 1:
        persona_note = f"- 人格: 按 {len(deliveries)} 种人格分别生成，此处仅存第一份\n"
    doc_blocks = "".join(
        f"### {d.get('name') or d.get('url')}\n- 地址: {d.get('url')}\n\n"
        f"```diff\n{d.get('diff') or ''}\n```\n\n"
        for d in docs
    )
    folder = history_dir / safe_name(str(target.get("id") or "target"))
    folder.mkdir(parents=True, exist_ok=True)
    # 文件名带微秒：秒级精度会让同一秒内的多次变更互相覆盖
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    (folder / f"{stamp}.md").write_text(
        f"# {target.get('name') or target.get('id')}\n\n"
        f"- 时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"{persona_note}\n"
        f"## AI 总结\n\n{summary}\n\n"
        f"## 变化文档（{len(docs)} 篇）\n\n{doc_blocks}",
        encoding="utf-8",
    )
    # 页面选择器失效时可能每次都判定为变更，限制存档数量避免无限增长
    files = sorted(folder.glob("*.md"))
    for old in files[:-MAX_HISTORY_PER_TARGET]:
        old.unlink(missing_ok=True)
