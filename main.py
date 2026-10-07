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
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .src import fetch, persona, storage, summary
from .src.constants import (
    CRON_JOB_NAME,
    CRON_NAME_PREFIX,
    DEFAULT_AGENT_STEPS,
    DEFAULT_CRON,
    PLUGIN_NAME,
    TARGETS_KEY,
    TOOL_READ_DEFAULT_LINES,
)
from .src.text import digest, make_diff, normalize, safe_name

NO_RUN_MESSAGE = "当前没有正在处理的文档变更。"


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
            description="Watchdoc 定时检查 · 由插件托管，改动将在重载后按配置重置",
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

    async def _check_one(self, target: dict) -> None:
        """检查单个监控项，发现变化时总结并推送。

        Args:
            target: 监控项配置，至少包含 id 与 url。
        """
        tid = str(target.get("id") or target.get("url") or "")
        url = str(target.get("url") or "")
        if not url:
            logger.warning(f"[watchdoc] 监控项 {tid} 缺少 url，跳过")
            return

        html = await self._fetch(url)
        # bs4 与 markitdown 都是同步阻塞的，放进线程避免卡住事件循环。
        # 归一化同理：ignore_patterns 是用户填的正则，长文本上灾难性回溯会
        # 把整个 bot 的事件循环拖死，而这行跑的是无人值守的定时任务。
        current = await asyncio.to_thread(
            self._to_markdown, html, str(target.get("selector") or ""), tid
        )
        current = await asyncio.to_thread(self._normalize, current, target)
        if not current.strip():
            logger.warning(f"[watchdoc] {tid} 抓取内容为空，页面可能需要 JS 渲染")
            return

        snap_path = self.snapshot_dir / f"{safe_name(tid)}.md"
        previous = snap_path.read_text(encoding="utf-8") if snap_path.exists() else None

        if previous is None:
            snap_path.write_text(current, encoding="utf-8")
            logger.info(f"[watchdoc] {tid} 已建立初始基线（{len(current)} 字符）")
            return

        if digest(current) == digest(previous):
            logger.debug(f"[watchdoc] {tid} 无变化")
            return

        diff = make_diff(previous, current)
        if not diff.strip():
            # 归一化之后没有差异，说明只是空白之类的无意义变动
            snap_path.write_text(current, encoding="utf-8")
            logger.info(f"[watchdoc] {tid} 仅有无意义变动，已更新快照")
            return

        # 人格不同的会话各生成一份总结，人格相同的共用一份
        change = {"before": previous, "after": current, "diff": diff}
        deliveries = []
        for group in await self._group_sessions_by_persona(target):
            summary_text = await self._make_summary(target, change, group)
            deliveries.append((summary_text, group["umos"]))

        snap_path.write_text(current, encoding="utf-8")
        self._archive(target, diff, deliveries)
        await self._notify(target, deliveries)
        logger.info(f"[watchdoc] {tid} 检测到变更并已推送（{len(deliveries)} 份总结）")

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
            if not isinstance(item, dict) or not str(item.get("url") or ""):
                return error_response("每个监控项都必须是对象且带 url", status_code=400)
            cleaned.append(item)
        previous = await self._load_targets()
        await self._save_targets(cleaned)
        await self._sync_cron_job()
        storage.drop_snapshots(self.snapshot_dir, previous, cleaned)
        return json_response({"saved": True, "count": len(cleaned)})

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
        return json_response(
            {
                "html": self._sanitize_for_preview(html, url),
                "textLength": len(soup.get_text(" ", strip=True)),
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

    async def _make_summary(self, target: dict, change: dict, group: dict) -> str:
        """生成一份总结，按配置决定走单次调用还是 agent 循环。

        Args:
            target: 监控项配置。
            change: 含 before / after / diff 的变更数据。
            group: 人格分组。

        Returns:
            总结文本。
        """
        if self.config.get("agent_mode", False) and group["umos"]:
            try:
                return await self._summarize_with_agent(target, change, group)
            except Exception as exc:  # noqa: BLE001
                # agent 跑挂不能吞掉这次变更，退回单次调用保证仍有总结
                logger.error(f"[watchdoc] agent 总结失败，退回单次调用: {exc}")
        return await self._summarize(target, change["diff"], group["prompt"])

    async def _summarize_with_agent(
        self, target: dict, change: dict, group: dict
    ) -> str:
        """用 agent 循环生成总结，模型可自行调用工具查阅原文。

        Args:
            target: 监控项配置。
            change: 含 before / after / diff 的变更数据。
            group: 人格分组。

        Returns:
            总结文本。
        """
        umo = group["umos"][0]
        provider_id = str(self.config.get("summary_provider") or "")
        if not provider_id:
            provider_id = self._default_provider_id()
        max_steps = int(self.config.get("agent_max_steps") or DEFAULT_AGENT_STEPS)

        self._runs[umo] = {"target": target, **change}
        try:
            return await summary.summarize_with_agent(
                self.context,
                target,
                change,
                group,
                provider_id,
                self._build_tool_set(group["tools"]),
                max_steps,
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
        scope: str,
        start_line: int = 1,
        line_count: int = TOOL_READ_DEFAULT_LINES,
    ) -> str:
        """按行号区间读取本次变更的原文。

        Args:
            event: 触发工具的事件，用来定位本次变更。
            scope: before 或 after。
            start_line: 起始行号。
            line_count: 读取行数。

        Returns:
            带行号的原文片段。
        """
        run = self._runs.get(event.unified_msg_origin)
        if run is None:
            return NO_RUN_MESSAGE
        return summary.read_lines(run, scope, start_line, line_count)

    async def _tool_search_document(
        self, event: AstrMessageEvent, keyword: str, scope: str
    ) -> str:
        """在原文中搜索关键词。

        Args:
            event: 触发工具的事件，用来定位本次变更。
            keyword: 搜索关键词。
            scope: before 或 after。

        Returns:
            带行号的命中片段。
        """
        run = self._runs.get(event.unified_msg_origin)
        if run is None:
            return NO_RUN_MESSAGE
        return summary.search_lines(run, keyword, scope)

    async def _summarize(
        self, target: dict, diff: str, persona_prompt: str = ""
    ) -> str:
        """调用 LLM 总结 diff。

        Args:
            target: 监控项配置。
            diff: unified diff 文本。
            persona_prompt: 会话人格的 system prompt。

        Returns:
            总结文本。
        """
        return await summary.summarize(
            self._get_provider(), target, diff, persona_prompt
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
        self, target: dict, deliveries: list[tuple[str, list[str]]]
    ) -> None:
        """把变更总结推送到监控项配置的会话。

        Args:
            target: 监控项配置。
            deliveries: (总结, 会话列表) 的列表。
        """
        title = target.get("name") or target.get("id")
        pushed = False
        for summary_text, umos in deliveries:
            if not umos:
                continue
            pushed = True
            text = f"[文档变更] {title}\n{target.get('url')}\n\n{summary_text}"
            for umo in umos:
                try:
                    await self.context.send_message(umo, MessageChain([Plain(text)]))
                except Exception as exc:  # noqa: BLE001
                    # 单个会话推送失败不能影响其余会话；没发出去就不写历史
                    logger.error(f"[watchdoc] 推送到 {umo} 失败: {exc}")
                    continue
                try:
                    await self._append_history(
                        umo, str(title), str(target.get("url") or ""), text
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[watchdoc] 追加 {umo} 会话历史失败: {exc}")

        if not pushed:
            logger.warning(f"[watchdoc] 未配置推送会话，结果仅落盘：{title}")

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
        self, target: dict, diff: str, deliveries: list[tuple[str, list[str]]]
    ) -> None:
        """把本次变更存档到插件数据目录。

        Args:
            target: 监控项配置。
            diff: unified diff 文本。
            deliveries: (总结, 会话列表) 的列表。
        """
        storage.archive(self.history_dir, target, diff, deliveries)

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
            snap = self.snapshot_dir / f"{safe_name(str(item.get('id') or ''))}.md"
            flag = "启用" if item.get("enabled", True) else "停用"
            status = "已建立基线" if snap.exists() else "尚未建立基线"
            sessions = len(item.get("sessions") or [])
            push = f"推送 {sessions} 个会话" if sessions else "未配置推送"
            lines.append(
                f"- [{flag}] {item.get('id')} · {item.get('name', '')} · {status}"
                f" · {push}"
            )
        return "\n".join(lines)

    @filter.command("watchdoc")
    async def _cmd_watchdoc(self, event: AstrMessageEvent):
        """Watchdoc 指令。用法：/watchdoc [check|list|reset <id>|probe <url>]"""
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
            snap = self.snapshot_dir / f"{safe_name(args[2])}.md"
            if snap.exists():
                snap.unlink()
                yield event.plain_result(
                    f"已重置 {args[2]} 的基线，下次检查将重新建立。"
                )
            else:
                yield event.plain_result(f"未找到 {args[2]} 的基线快照。")
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
