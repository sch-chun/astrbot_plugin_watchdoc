"""Watchdoc（文档监控）插件入口。

实现在 src/ 下；插件类与 handler 必须留在入口模块。

设计要点：
- 调度走 AstrBot 内置的 cron 管理器（`basic` 类型），回调是插件自己的函数，
  不经过 Agent/LLM，因此「无变化」时零 token 消耗。
- 定时任务由插件托管：有监控项就有任务、清空监控项就撤掉任务，注册时机覆盖
  插件加载/重载与监控项保存，用户在面板上误删也能靠保存或重载自愈。
- 变化判定基于「CSS 选择器裁剪 → 转 Markdown → 归一化 → 哈希比对」。
- 归一化默认只压缩空白，刻意不做日期替换：日期可能是有意义的版本标识，
  替换掉会漏掉真正的变更。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
from fastapi.responses import Response

from .src import fetch, persona, storage, summary
from .src.constants import (
    CRON_JOB_NAME,
    CRON_NAME_PREFIX,
    DEFAULT_AGENT_STEPS,
    DEFAULT_CRON,
    DEFAULT_HISTORY_ROUNDS,
    PLUGIN_NAME,
    TARGETS_KEY,
    TOOL_READ_DEFAULT_LINES,
)
from .src.text import build_doc_slug_map, digest, make_diff, normalize, safe_name

NO_RUN_MESSAGE = "当前没有正在处理的文档变更。"


def _build_page_background_response(config, data_dir) -> tuple[dict, str | None]:
    """构造配置页背景图响应数据（纯函数，便于单测）。

    仅当配置了背景图且文件可读时返回 data_url；否则 data_url 为空，前端不渲染背景。
    图片以 base64 Data URL 返回，避免二进制穿桥与跨域/外链，保持离线可用。
    路径解析后强制锁定在插件数据目录内，防止路径穿越。

    同时按文件字节计算内容 ETag：路由据此应答条件请求，文件未变更时返回 304，
    浏览器经 HTTP 缓存把已存 base64 透明交给 XHR，大图不再每次进入页面全量重传。

    Args:
        config: 插件配置（AstrBotConfig 或 dict），可为 None。
        data_dir: 插件数据目录（Path 或等价字符串）。

    Returns:
        (响应 dict, ETag 或 None) 二元组。响应 dict 含 data_url / opacity / blur，
        尚未包裹 JSONResponse；未配置背景图或读取失败时 ETag 为 None，
        路由不附加缓存头（空响应体积极小，无缓存意义）。
    """
    empty = {"code": 0, "data": {"data_url": "", "opacity": 1.0, "blur": 0}}
    if config is None:
        return empty, None
    background_files = config.get("page_background") or []
    if not isinstance(background_files, list) or not background_files:
        return empty, None

    rel_path = background_files[0]
    if not isinstance(rel_path, str) or not rel_path:
        return empty, None

    # 锁定在插件数据目录内，防止路径穿越
    base = Path(data_dir).resolve()
    target = (base / rel_path).resolve()
    if target != base and base not in target.parents:
        return empty, None

    if not target.is_file():
        return empty, None

    try:
        raw = target.read_bytes()
    except OSError:
        return empty, None

    mime, _ = mimetypes.guess_type(target.name)
    if not mime:
        mime = "application/octet-stream"
    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    opacity = config.get("page_background_opacity", 0.2)
    blur = config.get("page_background_blur", 0)
    etag = f'"{hashlib.sha256(raw).hexdigest()[:16]}"'
    return {"code": 0, "data": {"data_url": data_url, "opacity": opacity, "blur": blur}}, etag


def _history_role(msg) -> str:
    """取一条对话消息的角色，兼容 dict（AstrBot 落盘）与对象（部分接口返回）。"""
    if isinstance(msg, dict):
        return str(msg.get("role") or "").strip().lower()
    return str(getattr(msg, "role", "") or "").strip().lower()


def _history_content(msg):
    """取一条对话消息的内容，兼容 dict 与对象两种形态。"""
    if isinstance(msg, dict):
        return msg.get("content")
    return getattr(msg, "content", None)


def _message_text(content) -> str:
    """把消息内容归一化成纯文本。

    AstrBot 对话库里 content 可能是字符串，也可能是多模态组件列表
    （每项含 type=text 或 image_url 等）。只抽取文本部分。

    Args:
        content: 消息内容，字符串或组件列表。

    Returns:
        纯文本；无法提取时返回空串。
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part).strip()
    return ""


class WatchdocPlugin(Star):
    """监控目标文档 URL，发现变化时生成 diff、AI 总结并推送到指定会话。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        self.context = context
        self.config = config if config is not None else {}
        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.snapshot_dir = self.data_dir / "snapshots"
        self.history_dir = self.data_dir / "history"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        # agent 模式下工具靠会话 UMO 取本次变更数据，按 UMO 存以避免多监控项串味
        self._runs: dict[str, dict] = {}

        self.context.register_web_api(
            f"/{PLUGIN_NAME}/targets", self._api_get_targets, ["GET"], "获取监控项"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/targets", self._api_save_targets, ["POST"], "保存监控项"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/preview",
            self._api_preview,
            ["GET"],
            "抓取页面用于可视化选取",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/probe", self._api_probe, ["GET"], "自动探测正文选择器"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/sessions", self._api_sessions, ["GET"], "列出已知会话"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/page-background",
            self._api_page_background,
            ["GET"],
            "获取配置页背景图",
        )

    async def initialize(self) -> None:
        """AstrBot 加载或重载本插件后同步定时任务。

        框架在插件实例化并设置好 plugin_id 之后调用，启动加载与面板重载都会走到。
        """
        await self._sync_cron_job()

    async def _sync_cron_job(self) -> None:
        """按当前监控项同步定时检查任务。

        有监控项时确保任务存在，没有监控项时撤掉任务。重新注册前会保留既有任务
        的启用状态，避免把用户手动暂停过的任务又拉起来。

        `add_basic_job` 每次都会写一条新记录，且处理器只存在于内存里，因此无论
        是配置里的 cron 变了、还是插件重载了，都需要先删后建而不是就地改。
        """
        manager = self.context.cron_manager
        if manager is None:
            logger.warning("[watchdoc] 未获取到 cron 管理器，定时检查不可用")
            return

        jobs = [
            job
            for job in await manager.list_jobs("basic")
            if job.name.startswith(CRON_NAME_PREFIX)
        ]
        enabled = next(
            (job.enabled for job in jobs if job.name == CRON_JOB_NAME),
            True,
        )
        for job in jobs:
            await manager.delete_job(job.job_id)

        if not await self._load_targets():
            logger.info("[watchdoc] 没有配置任何监控项，不注册定时检查任务")
            return

        await manager.add_basic_job(
            name=CRON_JOB_NAME,
            cron_expression=str(self.config.get("cron", DEFAULT_CRON)),
            handler=self._check_all,
            description="Watchdoc 定时检查 · 由插件托管，请勿手动修改。若被改动将在插件重载后按配置重置",
            enabled=enabled,
        )
        logger.info("[watchdoc] 定时检查任务已注册")

    async def _check_all(self) -> None:
        """cron 回调：逐个检查所有启用的监控项。"""
        targets = await self._load_targets()
        if not targets:
            logger.warning("[watchdoc] 没有配置任何监控项")
            return

        for target in targets:
            if not target.get("enabled", True):
                continue
            try:
                await self._check_one(target)
            except Exception as exc:  # noqa: BLE001
                # 单个监控项失败不能影响其余项，也不能让调度器停摆
                logger.error(f"[watchdoc] 检查 {target.get('id')} 失败: {exc}")

    async def _load_targets(self) -> list[dict]:
        """从插件 KV 存储读取监控项。

        Returns:
            监控项列表。KV 里没有或内容不合法时返回空列表。
        """
        return storage.normalize_targets(await self.get_kv_data(TARGETS_KEY, None))

    async def _save_targets(self, targets: list[dict]) -> None:
        """把监控项写入插件 KV 存储。

        Args:
            targets: 监控项列表。
        """
        await self.put_kv_data(TARGETS_KEY, targets)

    def _docs_of(self, target: dict) -> list[dict]:
        """取监控项要检测的文档列表，兼容旧版单 url 配置。

        新结构下文档写在 documents 字段；旧版只有顶层 url/selector 时
        在此兜底成单文档列表，保证已部署的监控项在用户于面板重存前仍能运行。

        Args:
            target: 监控项配置。

        Returns:
            文档字典列表，每项至少含 url。
        """
        docs = target.get("documents")
        if isinstance(docs, list) and docs:
            return [d for d in docs if isinstance(d, dict)]
        url = str(target.get("url") or "").strip()
        if url:
            # 迁移兜底：旧单文档项仍能检测，待用户在面板重存即转为 documents
            return [{"url": url, "selector": str(target.get("selector") or "")}]
        return []

    def _iter_target_umos(self, target: dict) -> list[str]:
        """取监控项要推送的会话列表（去重、去空、保持顺序）。

        Args:
            target: 监控项配置。

        Returns:
            会话标识列表。
        """
        umos = [str(item).strip() for item in target.get("sessions") or []]
        seen: set[str] = set()
        result: list[str] = []
        for item in umos:
            if item and item not in seen:
                seen.add(item)
                result.append(item)
        return result

    async def _check_one(self, target: dict) -> None:
        """检查单个监控项，发现变化时把变化的文档一起总结并推送。

        监控项可持有多个文档（documents）。每次检测逐文档比对快照，
        仅真正变化的文档进入总结；同一次检测里变化的文档会被合并成一份总结。

        Args:
            target: 监控项配置，至少包含 id 与 documents（兼容旧版单 url）。
        """
        tid = str(target.get("id") or target.get("name") or "")
        docs = self._docs_of(target)
        if not docs:
            logger.warning(f"[watchdoc] 监控项 {tid} 没有可监控的文档，跳过")
            return

        doc_dir = self.snapshot_dir / safe_name(tid)
        changed: list[dict] = []
        # 同 target 内文档快照名必须单射：前 64 字符相同的 URL 会因截断撞名、
        # 共用一个基线槽位并循环误报，故按整组 URL 统一去重（撞名追加短哈希）。
        slug_map = build_doc_slug_map(
            [str(doc.get("url") or "").strip() for doc in docs]
        )
        for doc in docs:
            url = str(doc.get("url") or "").strip()
            if not url:
                continue
            slug = slug_map[url]
            html = await self._fetch(url)
            # bs4 与 markitdown 都是同步阻塞的，放进线程避免卡住事件循环。
            # 归一化同理：ignore_patterns 是用户填的正则，长文本上灾难性回溯会
            # 把整个 bot 的事件循环拖死，而这行跑的是无人值守的定时任务。
            current = await asyncio.to_thread(
                self._to_markdown, html, str(doc.get("selector") or ""), tid
            )
            current = await asyncio.to_thread(self._normalize, current, target)
            if not current.strip():
                logger.warning(
                    f"[watchdoc] {tid}/{slug} 抓取内容为空，页面可能需要 JS 渲染"
                )
                continue

            new_path = doc_dir / f"{slug}.md"
            previous = (
                new_path.read_text(encoding="utf-8") if new_path.exists() else None
            )
            if previous is None and len(docs) == 1:
                # 迁移兜底：旧单文档基线在 snapshots/{tid}.md，一次性搬进分组目录
                legacy = self.snapshot_dir / f"{safe_name(tid)}.md"
                if legacy.exists():
                    previous = legacy.read_text(encoding="utf-8")
                    doc_dir.mkdir(parents=True, exist_ok=True)
                    new_path.write_text(previous, encoding="utf-8")
                    legacy.unlink(missing_ok=True)

            if previous is None:
                doc_dir.mkdir(parents=True, exist_ok=True)
                new_path.write_text(current, encoding="utf-8")
                logger.info(
                    f"[watchdoc] {tid}/{slug} 已建立初始基线（{len(current)} 字符）"
                )
                continue

            if digest(current) == digest(previous):
                logger.debug(f"[watchdoc] {tid}/{slug} 无变化")
                continue

            diff = make_diff(previous, current)
            if not diff.strip():
                # 归一化之后没有差异，说明只是空白之类的无意义变动
                new_path.write_text(current, encoding="utf-8")
                logger.info(f"[watchdoc] {tid}/{slug} 仅有无意义变动，已更新快照")
                continue

            # 更新该文档快照（未变化的文档保持原基线，不反复写）
            doc_dir.mkdir(parents=True, exist_ok=True)
            new_path.write_text(current, encoding="utf-8")
            changed.append(
                {
                    "url": url,
                    "selector": str(doc.get("selector") or ""),
                    "name": str(doc.get("name") or doc.get("url") or ""),
                    "before": previous,
                    "after": current,
                    "diff": diff,
                }
            )

        if not changed:
            logger.debug(f"[watchdoc] {tid} 无变化")
            return

        # 人格不同的会话各生成一份总结，人格相同的共用一份；
        # 同一次检测里变化的文档合并进同一份总结
        change = {"docs": changed}
        deliveries = []
        raw = self.config.get("history_rounds")
        rounds = int(raw) if raw is not None else DEFAULT_HISTORY_ROUNDS
        if rounds != 0:
            # 参考会话对话历史（>0=最近 N 轮；-1=全部）：历史是会话私有的、无法跨
            # 会话共享，只能逐会话单独总结（人格相同也不再共用）——代价是 token 翻倍。
            use_persona = bool(self.config.get("use_session_persona", True))
            for umo in self._iter_target_umos(target):
                persona = await self._resolve_persona(umo) if use_persona else None
                group = {
                    "umos": [umo],
                    "prompt": (persona or {}).get("prompt") or "",
                    "tools": (persona or {}).get("tools", None),
                }
                try:
                    history_text = await self._read_session_history(umo, rounds)
                except Exception as exc:  # noqa: BLE001
                    # 单会话历史读不到不能拖垮其余会话，也不影响本次变更总结
                    logger.error(f"[watchdoc] 读取 {umo} 会话历史失败: {exc}")
                    history_text = ""
                summary_text = await self._make_summary(
                    target, change, group, history_text
                )
                deliveries.append((summary_text, [umo]))
        else:
            # 关闭历史参考：按人格分组，人格相同的会话共用一份总结（token 最省）
            for group in await self._group_sessions_by_persona(target):
                summary_text = await self._make_summary(target, change, group)
                deliveries.append((summary_text, group["umos"]))

        self._archive(target, change, deliveries)
        await self._notify(target, change, deliveries)
        logger.info(
            f"[watchdoc] {tid} 检测到 {len(changed)} 篇文档变更并已推送"
            f"（{len(deliveries)} 份总结）"
        )

    async def _fetch(self, url: str) -> str:
        """抓取页面 HTML。

        Args:
            url: 目标地址。

        Returns:
            页面 HTML 文本。
        """
        return await fetch.fetch_html(url)

    def _to_markdown(self, html: str, selector: str, tid: str) -> str:
        """把 HTML 裁剪成正文并转成 Markdown。

        Args:
            html: 原始 HTML。
            selector: 正文容器的 CSS 选择器。
            tid: 监控项 ID，仅用于日志。

        Returns:
            Markdown 文本。
        """
        return fetch.to_markdown(html, selector, tid)

    async def _probe_url(self, url: str) -> str:
        """探测正文容器选择器。

        Args:
            url: 目标地址。

        Returns:
            面向用户的候选清单文本。
        """
        return await fetch.probe_url(url, self._fetch)

    def _sanitize_for_preview(self, html: str, base_url: str) -> str:
        """清洗 HTML 使其能在 Page 中安全渲染。

        Args:
            html: 原始 HTML。
            base_url: 页面地址。

        Returns:
            可安全注入的 HTML。
        """
        return fetch.sanitize_for_preview(html, base_url)

    async def _api_get_targets(self):
        """Page 读取当前监控项，以及用于预填输入框的默认任务指令。"""
        return json_response(
            {
                "targets": await self._load_targets(),
                "default_instruction": self._default_instruction(),
            }
        )

    def _default_instruction(self) -> str:
        """取默认任务指令，供 Page 预填。

        Returns:
            默认指令文本，随 agent 模式切换。
        """
        return summary.default_instruction(bool(self.config.get("agent_mode", False)))

    async def _api_save_targets(self):
        """Page 保存监控项。"""
        payload = await request.json(default={})
        targets = payload.get("targets")
        if not isinstance(targets, list):
            return error_response("targets 必须是数组", status_code=400)

        cleaned = []
        for item in targets:
            if not isinstance(item, dict):
                return error_response("每个监控项都必须是对象", status_code=400)
            docs = item.get("documents")
            if not isinstance(docs, list) or not docs:
                # 要求迁移：旧版单 url 项需先在面板重存为 documents 结构
                return error_response(
                    "每个监控项都必须含 documents 数组", status_code=400
                )
            norm_docs = []
            for d in docs:
                if not isinstance(d, dict) or not str(d.get("url") or "").strip():
                    return error_response(
                        "documents 每项都必须是含 url 的对象", status_code=400
                    )
                norm_docs.append(
                    {
                        "url": str(d["url"]).strip(),
                        "selector": str(d.get("selector") or ""),
                        "name": str(d.get("name") or "").strip(),
                    }
                )
            item["documents"] = norm_docs
            cleaned.append(item)
        previous = await self._load_targets()
        await self._save_targets(cleaned)
        await self._sync_cron_job()
        storage.drop_snapshots(self.snapshot_dir, previous, cleaned)
        return json_response({"saved": True, "count": len(cleaned)})

    async def _api_page_background(self):
        """配置页背景图：未配置时返回空 data_url，前端不渲染背景。

        配置了背景图时附加 ETag 与 Cache-Control: private, no-cache：
        浏览器再次请求自动携带 If-None-Match，文件未变更则返回 304，
        已存 base64 由 HTTP 缓存透明复用，大图不再每次进入页面全量重传。
        """
        payload, etag = _build_page_background_response(self.config, self.data_dir)
        if not etag:
            return json_response(payload)
        cache_headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=cache_headers)
        response = json_response(payload)
        response.headers.update(cache_headers)
        return response

    async def _api_sessions(self):
        """列出 AstrBot 已知的会话，供 Page 选择推送目标。

        与面板内置的会话下拉同源：会话表里出现过的 user_id 就是 UMO。没跟机器人
        说过话的会话不会出现在这里，这种情况可以在 Page 里手动填 UMO。

        Returns:
            含 sessions 列表的响应，每项为带 umo 与 platform 的对象。
        """
        db = self.context.get_db()
        conversations = await db.get_all_conversations(page=1, page_size=500)
        known: dict[str, str] = {}
        for conv in conversations:
            umo = str(getattr(conv, "user_id", "") or "").strip()
            if umo and umo not in known:
                known[umo] = str(getattr(conv, "platform_id", "") or "").strip()
        return json_response(
            {"sessions": [{"umo": u, "platform": p} for u, p in known.items()]}
        )

    async def _api_preview(self):
        """抓取页面并返回清洗后的 HTML 供 Page 渲染。"""
        url = request.query.get("url", "")
        if not url:
            return error_response("缺少 url 参数", status_code=400)
        try:
            html = await self._fetch(url)
        except Exception as exc:  # noqa: BLE001
            return error_response(
                f"抓取失败：{type(exc).__name__}: {exc}", status_code=502
            )

        soup = await asyncio.to_thread(fetch.build_soup, html)
        title = fetch.extract_title(soup)
        return json_response(
            {
                "html": self._sanitize_for_preview(html, url),
                "textLength": len(soup.get_text(" ", strip=True)),
                "title": title,
            }
        )

    async def _api_probe(self):
        """返回自动探测得到的选择器建议文本。"""
        url = request.query.get("url", "")
        if not url:
            return error_response("缺少 url 参数", status_code=400)
        try:
            return json_response({"suggestions": await self._probe_url(url)})
        except Exception as exc:  # noqa: BLE001
            return error_response(
                f"探测失败：{type(exc).__name__}: {exc}", status_code=502
            )

    def _normalize(self, text: str, target: dict) -> str:
        """归一化文本以消除无意义差异。

        Args:
            text: 待归一化文本。
            target: 监控项配置，可携带自己的 ignore_patterns。

        Returns:
            归一化后的文本。
        """
        patterns = (
            target.get("ignore_patterns") or self.config.get("ignore_patterns") or []
        )
        return normalize(text, patterns)

    async def _make_summary(
        self, target: dict, change: dict, group: dict, history: str = ""
    ) -> str:
        """生成一份总结，按配置决定走单次调用还是 agent 循环。

        Args:
            target: 监控项配置。
            change: 含 before / after / diff 的变更数据。
            group: 人格分组。
            history: 推送会话最近的对话历史，非空时追加到 prompt 末尾。

        Returns:
            总结文本。
        """
        if self.config.get("agent_mode", False) and group["umos"]:
            try:
                return await self._summarize_with_agent(target, change, group, history)
            except Exception as exc:  # noqa: BLE001
                # agent 跑挂不能吞掉这次变更，退回单次调用保证仍有总结
                logger.error(f"[watchdoc] agent 总结失败，退回单次调用: {exc}")
        return await self._summarize(target, change["docs"], group["prompt"], history)

    async def _summarize_with_agent(
        self, target: dict, change: dict, group: dict, history: str = ""
    ) -> str:
        """用 agent 循环生成总结，模型可自行调用工具查阅原文。

        Args:
            target: 监控项配置。
            change: 含 before / after / diff 的变更数据。
            group: 人格分组。
            history: 推送会话最近的对话历史，非空时追加到 prompt 末尾。

        Returns:
            总结文本。
        """
        umo = group["umos"][0]
        provider_id = str(self.config.get("summary_provider") or "")
        if not provider_id:
            provider_id = self._default_provider_id()
        max_steps = int(self.config.get("agent_max_steps") or DEFAULT_AGENT_STEPS)

        self._runs[umo] = {"target": target, "docs": change["docs"]}
        try:
            return await summary.summarize_with_agent(
                self.context,
                target,
                change["docs"],
                group,
                provider_id,
                self._build_tool_set(group["tools"]),
                max_steps,
                history,
            )
        finally:
            self._runs.pop(umo, None)

    def _build_tool_set(self, persona_tools: list[str] | None):
        """按人格配置组装可用工具集。

        Args:
            persona_tools: 人格的工具名单。

        Returns:
            工具集。
        """
        return summary.build_tool_set(self.context, persona_tools, self._doc_tools())

    def _doc_tools(self):
        """构造查阅本次变更原文的工具。

        Returns:
            工具列表。
        """
        return summary.doc_tools(self._tool_read_document, self._tool_search_document)

    async def _tool_read_document(
        self,
        event: AstrMessageEvent,
        doc: str = "",
        scope: str = "after",
        start_line: int = 1,
        line_count: int = TOOL_READ_DEFAULT_LINES,
    ) -> str:
        """按行号区间读取某篇变化文档的原文。

        Args:
            event: 触发工具的事件，用来定位本次变更。
            doc: 文档引用，#1 这样的序号或文档地址子串。
            scope: before 或 after。
            start_line: 起始行号。
            line_count: 读取行数。

        Returns:
            带行号的原文片段。
        """
        run = self._runs.get(event.unified_msg_origin)
        if run is None:
            return NO_RUN_MESSAGE
        doc_data = self._doc_by_ref(run, doc)
        if doc_data is None:
            return self._doc_resolution_error(run, doc)
        return summary.read_lines(doc_data, scope, start_line, line_count)

    async def _tool_search_document(
        self,
        event: AstrMessageEvent,
        keyword: str,
        doc: str = "",
        scope: str = "after",
    ) -> str:
        """在某篇变化文档的原文中搜索关键词。

        Args:
            event: 触发工具的事件，用来定位本次变更。
            keyword: 搜索关键词。
            doc: 文档引用，#1 这样的序号或文档地址子串。
            scope: before 或 after。

        Returns:
            带行号的命中片段。
        """
        run = self._runs.get(event.unified_msg_origin)
        if run is None:
            return NO_RUN_MESSAGE
        doc_data = self._doc_by_ref(run, doc)
        if doc_data is None:
            return self._doc_resolution_error(run, doc)
        return summary.search_lines(doc_data, keyword, scope)

    def _doc_by_ref(self, run: dict, doc: str) -> dict | None:
        """按序号（#1）或地址子串从本次变更里取一篇文档。

        Args:
            run: 本次变更数据，含 docs。
            doc: 文档引用串。

        Returns:
            命中的文档字典；未命中返回 None。
        """
        docs = run.get("docs") or []
        if not docs:
            return None
        ref = str(doc).strip()
        if not ref:
            # 单文档监控项省略 doc 时默认取唯一一篇；
            # 多文档监控项必须显式指定，省略则交回 None 由 handler 提示可用列表。
            return docs[0] if len(docs) == 1 else None
        if ref.startswith("#"):
            try:
                idx = int(ref[1:]) - 1
                if 0 <= idx < len(docs):
                    return docs[idx]
            except ValueError:
                pass
        for d in docs:
            if ref in str(d.get("url") or ""):
                return d
        return None

    def _doc_list(self, run: dict) -> str:
        """列出本次变更可用文档，供工具报错时提示。

        Args:
            run: 本次变更数据，含 docs。

        Returns:
            文档序号与名称的可读串。
        """
        docs = run.get("docs") or []
        return "；".join(
            f"#{i + 1} {d.get('name') or d.get('url')}" for i, d in enumerate(docs)
        )

    def _doc_resolution_error(self, run: dict, doc: str) -> str:
        """文档解析失败时的提示语。

        多文档监控项省略 doc 时，给出「必须指定」的明确指引；
        其余情况（未匹配到）沿用「未找到」措辞。两者都附可用文档列表。

        Args:
            run: 本次变更数据，含 docs。
            doc: 传入的文档引用串。

        Returns:
            带可用文档列表的提示语。
        """
        if not str(doc).strip() and len(run.get("docs") or []) > 1:
            return f"多文档监控项必须指定 doc 参数。可用文档：{self._doc_list(run)}"
        return f"未找到指定的文档：{doc}。可用文档：{self._doc_list(run)}"

    async def _summarize(
        self,
        target: dict,
        changed_docs: list[dict],
        persona_prompt: str = "",
        history: str = "",
    ) -> str:
        """调用 LLM 总结若干变化文档。

        Args:
            target: 监控项配置。
            changed_docs: 本次变化的文档列表，每项含 name/url/diff。
            persona_prompt: 会话人格的 system prompt。
            history: 推送会话最近的对话历史，非空时追加到 prompt 末尾。

        Returns:
            总结文本。
        """
        return await summary.summarize(
            self._get_provider(), target, changed_docs, persona_prompt, history
        )

    def _get_provider(self):
        """获取用于总结的模型 Provider。

        Returns:
            Provider 实例，未配置且无可用模型时返回 None。
        """
        return summary.get_provider(
            self.context, str(self.config.get("summary_provider") or "")
        )

    def _default_provider_id(self) -> str:
        """取默认对话模型的 Provider ID。

        agent 循环只认 Provider ID，而默认模型要用不带会话的方式解析——
        否则同一份文档的不同人格分组可能落到不同模型上。

        Returns:
            Provider ID，没有可用模型时返回空字符串。
        """
        provider = self.context.get_using_provider()
        if provider is None:
            return ""
        return str(provider.provider_config.get("id") or "")

    async def _resolve_persona(self, umo: str) -> dict | None:
        """解析会话当前生效的人格。

        Args:
            umo: 会话标识。

        Returns:
            人格对象；无法确定时返回 None。
        """
        return await persona.resolve(umo, self.context)

    async def _group_sessions_by_persona(self, target: dict) -> list[dict]:
        """按会话人格把推送目标分组。

        Args:
            target: 监控项配置。

        Returns:
            分组列表。
        """
        umos = [str(item).strip() for item in target.get("sessions") or []]
        umos = [item for item in umos if item]
        return await persona.group_sessions(
            self.context,
            umos,
            bool(self.config.get("use_session_persona", True)),
        )

    async def _notify(
        self, target: dict, change: dict, deliveries: list[tuple[str, list[str]]]
    ) -> None:
        """把变更总结推送到监控项配置的会话。

        Args:
            target: 监控项配置。
            change: 含本次变化文档（before/after/diff）的变更数据。
            deliveries: (总结, 会话列表) 的列表。
        """
        title = target.get("name") or target.get("id")
        # 只列本次真正变化的文档地址，不再罗列监控项全部链接（P2 修复 v0.1.11）：
        # 否则十几个链接的监控项只变了 1-2 篇，推送开头也会被塞一堆无关 URL。
        # change["docs"] 为空时 _check_one 已提前返回、不会走到这里，必有变化文档。
        seen: set[str] = set()
        changed_urls: list[str] = []
        for d in change.get("docs") or []:
            url = str(d.get("url") or "").strip()
            if url and url not in seen:
                seen.add(url)
                changed_urls.append(url)
        doc_lines = "\n".join(f"- {u}" for u in changed_urls)
        pushed = False
        for summary_text, umos in deliveries:
            if not umos:
                continue
            pushed = True
            text = f"[文档变更] {title}\n{doc_lines}\n\n{summary_text}"
            for umo in umos:
                try:
                    await self.context.send_message(umo, MessageChain([Plain(text)]))
                except Exception as exc:  # noqa: BLE001
                    # 单个会话推送失败不能影响其余会话；没发出去就不写历史
                    logger.error(f"[watchdoc] 推送到 {umo} 失败: {exc}")
                    continue
                try:
                    await self._append_history(umo, str(title), doc_lines, text)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[watchdoc] 追加 {umo} 会话历史失败: {exc}")

        if not pushed:
            logger.warning(f"[watchdoc] 未配置推送会话，结果仅落盘：{title}")

    async def _read_session_history(self, umo: str, rounds: int) -> str:
        """读取会话最近的对话历史，供总结时参考。

        从 AstrBot 对话库取该会话当前对话的消息，转成
        「用户 / 助手」文本。放在总结 prompt 末尾，使稳定的文档 diff
        前缀能多命中提示缓存（跨会话共享）。

        Args:
            umo: 会话标识。
            rounds: 历史轮数。0=关闭（返回空串）；-1=不限制（取全部对话）；
                >0=取最近 N 轮（每轮约 2 条消息，即最近 2N 条）。

        Returns:
            历史文本；无历史或读不到时返回空串。
        """
        manager = self.context.conversation_manager
        if manager is None:
            return ""
        if rounds == 0:
            return ""
        cid = await manager.get_curr_conversation_id(umo)
        if not cid:
            return ""
        conv = await self.context.get_db().get_conversation_by_id(cid=cid)
        content = list((getattr(conv, "content", None) if conv else None) or [])
        if not content:
            return ""
        # rounds > 0：取末尾 rounds 轮（常见交替下即 2×rounds 条消息）；
        # rounds < 0（-1=不限制）：取全部对话历史。
        recent = content if rounds < 0 else content[-(rounds * 2) :]
        lines = []
        for msg in recent:
            role = _history_role(msg)
            if role == "system":
                continue
            text = _message_text(_history_content(msg))
            if not text:
                continue
            label = "用户" if role == "user" else "助手"
            lines.append(f"{label}：{text}")
        return "\n".join(lines)

    async def _append_history(self, umo: str, title: str, url: str, text: str) -> None:
        """把推送正文追加到会话的对话历史。

        Args:
            umo: 会话标识。
            title: 监控项名称。
            url: 文档地址。
            text: 推送正文。
        """
        await storage.append_to_history(self.context, umo, title, url, text)

    def _archive(
        self, target: dict, change: dict, deliveries: list[tuple[str, list[str]]]
    ) -> None:
        """把本次变更存档到插件数据目录。

        Args:
            target: 监控项配置。
            change: 含 docs 的变更数据。
            deliveries: (总结, 会话列表) 的列表。
        """
        storage.archive(self.history_dir, target, change, deliveries)

    async def _format_list(self) -> str:
        """生成监控项清单文本。

        Returns:
            用于回复用户的清单。
        """
        targets = await self._load_targets()
        if not targets:
            return "当前没有配置监控项，请在插件页面里添加。"

        lines = [f"共 {len(targets)} 个监控项："]
        for item in targets:
            doc_dir = self.snapshot_dir / safe_name(str(item.get("id") or ""))
            flag = "启用" if item.get("enabled", True) else "停用"
            status = "已建立基线" if doc_dir.is_dir() else "尚未建立基线"
            sessions = len(item.get("sessions") or [])
            push = f"推送 {sessions} 个会话" if sessions else "未配置推送"
            lines.append(
                f"- [{flag}] {item.get('id')} · {item.get('name', '')} · {status}"
                f" · {push}"
            )
        return "\n".join(lines)

    @filter.command("watchdoc")
    async def _cmd_watchdoc(self, event: AstrMessageEvent):
        """Watchdoc 指令。用法：/watchdoc [check|list|reset <id|url>|probe <url>]

        reset 接受监控项 id（重置该项下全部文档基线）或文档 URL（只重置那一篇）。
        """
        args = event.message_str.strip().split()
        sub = args[1] if len(args) > 1 else "check"

        if sub == "list":
            yield event.plain_result(await self._format_list())
            return

        if sub == "probe" and len(args) > 2:
            yield event.plain_result("正在探测正文容器，请稍候…")
            try:
                yield event.plain_result(await self._probe_url(args[2]))
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"探测失败：{type(exc).__name__}: {exc}")
            return

        if sub == "reset" and len(args) > 2:
            token = args[2].strip()
            targets = await self._load_targets()
            # reset 接受「监控项 id」（重置该项下全部文档基线）或「文档 URL」
            # （只重置那一篇）。文档基线实际落在 snapshot_dir / safe_name(tid)
            # / {slug}.md，必须用 build_doc_slug_map 解析 slug，不能像旧实现那样去
            # 平铺目录找 safe_name(token).md——否则永远 miss（见 v0.1.10 P2 修复）。
            by_id = next((t for t in targets if str(t.get("id") or "") == token), None)
            if by_id is not None:
                tid = str(by_id.get("id") or "")
                doc_dir = self.snapshot_dir / safe_name(tid)
                removed = 0
                if doc_dir.is_dir():
                    for f in doc_dir.glob("*.md"):
                        f.unlink(missing_ok=True)
                        removed += 1
                # 兼容旧版单文件基线
                (self.snapshot_dir / f"{safe_name(tid)}.md").unlink(missing_ok=True)
                if removed:
                    yield event.plain_result(
                        f"已重置监控项 {tid} 的全部 {removed} 个文档基线，"
                        f"下次检查将重新建立。"
                    )
                else:
                    yield event.plain_result(f"监控项 {tid} 没有已建立的基线快照。")
                return

            # token 视为文档 URL：在所有含该 URL 的监控项里清掉对应文档基线
            removed: list[tuple[str, str]] = []
            for t in targets:
                tid = str(t.get("id") or "")
                docs = [
                    str(d.get("url") or "").strip()
                    for d in (t.get("documents") or [])
                    if str(d.get("url") or "").strip()
                ]
                if token not in docs:
                    continue
                slug = build_doc_slug_map(docs)[token]
                snap = self.snapshot_dir / safe_name(tid) / f"{slug}.md"
                if snap.exists():
                    snap.unlink()
                    removed.append((tid, token))
            if removed:
                lines = "\n".join(f"- [{tid}] {url}" for tid, url in removed)
                yield event.plain_result(
                    "已重置以下文档的基线，下次检查将重新建立：\n" + lines
                )
            else:
                yield event.plain_result(
                    f"未找到 {token} 对应的基线快照"
                    f"（检查 URL 是否匹配某监控项下的文档）。"
                )
            return

        yield event.plain_result("开始检查文档变更，结果将推送到配置的目标会话。")
        await self._check_all()
        yield event.plain_result("检查完成。无变化时不会推送任何消息。")

    async def terminate(self) -> None:
        """插件卸载或重载时清理定时任务。"""
        manager = self.context.cron_manager
        if manager is None:
            return
        for job in await manager.list_jobs("basic"):
            if job.name.startswith(CRON_NAME_PREFIX):
                await manager.delete_job(job.job_id)
