"""watchdoc 插件的单元测试。

全部用例不依赖网络：抓取环节用本地 HTML 桩替换，重点覆盖异常与边界，
而不是「一切正常」的主路径。
"""

import json
import threading

import pytest
from astrbot.api import FunctionTool, ToolSet
from astrbot.api import web as web_mod
from astrbot_plugin_watchdoc import main as main_mod
from astrbot_plugin_watchdoc.main import WatchdocPlugin
from astrbot_plugin_watchdoc.src import constants, fetch, summary, text
from bs4.element import Tag

# 含导航栏噪音（「最后更新」）与正文页面的最小 HTML
HTML = """<html><body>
<nav>导航 最后更新 2026-10-01</nav>
<main class="markdown-body"><h1>非实时语音合成</h1><p>非流式返回音频文件 URL。</p></main>
<footer>页脚</footer>
</body></html>"""

# 正文区更短但语义化，噪音区更长却是构建产物型 class：
# 用于验证排序时「稳定性优先于文本量」
NOISY_HTML = """<html><body>
<div class="css-1a2b3c">这里是一段刻意写得很长的噪音文本，用来验证排序时稳定性优先于
文本量。即使它的字符数明显更多，也不应被推荐给用户，因为这类类名是前端构建产物，
每次构建都可能变化，用它做监控锚点会频繁失效。</div>
<main class="markdown-body">真正的正文。</main>
</body></html>"""

# 文本量最大的容器没有 class，无法写成选择器：用于验证兜底项会被放弃
BARE_DIV_HTML = """<html><body>
<div>这里是一段刻意写得很长的文本，所在的 div 没有任何 class 或 id，
因此无法写成可用的选择器，不应该被推荐给用户。</div>
<main class="markdown-body">真正的正文。</main>
</body></html>"""

# 纯 JS 渲染页面的模样：正文不在 HTML 里
SPA_HTML = """<html><body><div id="root"></div>
<script>window.__DATA__ = {}</script></body></html>"""


class FakeResponse:
    def __init__(self, text):
        self.completion_text = text


class FakeProvider:
    def __init__(self, raise_on_call=False):
        self.calls = []
        self.system_prompts = []
        self.raise_on_call = raise_on_call
        self.provider_config = {"id": "default-provider"}

    async def text_chat(self, prompt, session_id=None, system_prompt=None):
        self.calls.append(prompt)
        self.system_prompts.append(system_prompt)
        if self.raise_on_call:
            raise RuntimeError("provider boom")
        return FakeResponse("模拟总结")


class FakeJob:
    def __init__(self, job_id, name, cron_expression=None, enabled=True):
        self.job_id = job_id
        self.name = name
        self.cron_expression = cron_expression
        self.enabled = enabled


class FakeCronManager:
    def __init__(self):
        self.jobs = {}

    async def list_jobs(self, job_type=None):
        return list(self.jobs.values())

    async def delete_job(self, job_id):
        self.jobs.pop(job_id, None)

    async def add_basic_job(self, name, cron_expression, handler, **kwargs):
        job = FakeJob(name, name, cron_expression, bool(kwargs.get("enabled", True)))
        self.jobs[name] = job
        return job


class FakeStream:
    """aiohttp 响应流的替身，按固定块大小吐数据。"""

    def __init__(self, data: bytes, chunk: int = 8):
        self._data = data
        self._chunk = chunk

    def iter_chunked(self, _size: int):
        async def gen():
            for start in range(0, len(self._data), self._chunk):
                yield self._data[start : start + self._chunk]

        return gen()


class FakeResp:
    """aiohttp 响应的替身，只提供读取响应体需要的字段。"""

    def __init__(self, data: bytes, declared=None, charset=None):
        self.content = FakeStream(data)
        self.content_length = declared
        self.charset = charset


class FakeConversation:
    def __init__(self, user_id, platform_id, persona_id=None):
        self.user_id = user_id
        self.platform_id = platform_id
        self.persona_id = persona_id


class FakeLLMResponse:
    def __init__(self, text, role="assistant"):
        self.completion_text = text
        self.role = role


class FakeEvent:
    """只提供工具需要的会话标识。"""

    def __init__(self, umo):
        self.unified_msg_origin = umo


def make_tool(name, active=True):
    tool = FunctionTool(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
    )
    tool.active = active
    return tool


class FakeToolManager:
    """工具管理器的替身，返回真实的 ToolSet。"""

    def __init__(self, tools):
        self._tools = tools

    def get_full_tool_set(self):
        tool_set = ToolSet()
        for tool in self._tools:
            tool_set.add_tool(tool)
        return tool_set

    def get_func(self, name):
        return next((item for item in self._tools if item.name == name), None)


class FakePersonaManager:
    """按会话 UMO 返回人格 prompt 的替身。"""

    def __init__(self, prompts):
        self.prompts = prompts
        self.calls = []

    async def resolve_selected_persona(
        self,
        umo,
        conversation_persona_id,
        platform_name,
        provider_settings=None,
    ):
        self.calls.append((umo, conversation_persona_id, platform_name))
        prompt = self.prompts.get(umo)
        if prompt is None:
            return None, None, None, False
        return "p", {"prompt": prompt, "name": "p"}, None, False


class FakeConversationV2:
    def __init__(self, content):
        self.content = content


class FakeConversationManager:
    """会话管理器的替身：记录写回了什么历史。"""

    def __init__(self, current=None, histories=None):
        self.current = dict(current or {})
        self.histories = dict(histories or {})
        self.created = []
        self.updates = []

    async def get_curr_conversation_id(self, umo):
        return self.current.get(umo)

    async def new_conversation(self, umo, **kwargs):
        cid = f"cid-{len(self.histories) + 1}"
        self.created.append(umo)
        self.current[umo] = cid
        self.histories[cid] = []
        return cid

    async def update_conversation(self, umo, conversation_id=None, history=None, **kw):
        cid = conversation_id or self.current.get(umo)
        self.updates.append((umo, cid, history))
        self.histories[cid] = history


class FakeDB:
    def __init__(self, conversations, histories=None):
        self._conversations = conversations
        self._histories = histories or {}
        self.calls = []

    async def get_all_conversations(self, page=1, page_size=20):
        self.calls.append((page, page_size))
        return self._conversations

    async def get_conversations(self, user_id=None, platform_id=None):
        if user_id is None:
            return self._conversations
        return [item for item in self._conversations if item.user_id == user_id]

    async def get_conversation_by_id(self, cid=None):
        content = self._histories.get(cid)
        return FakeConversationV2(content) if content is not None else None


class FakeKV:
    """插件 KV 存储的替身。

    plugin_id 是插件管理器加载时才 setattr 到插件类上的，直接造实例拿不到，
    所以这里连 get_kv_data / put_kv_data 一起替掉。
    """

    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.puts = []

    async def get(self, key, default=None):
        return self.data.get(key, default)

    async def put(self, key, value):
        self.puts.append((key, value))
        self.data[key] = value


class FakeContext:
    def __init__(
        self,
        provider=None,
        conversations=None,
        persona_manager=None,
        conversation_manager=None,
    ):
        self.cron_manager = FakeCronManager()
        self.provider = provider if provider is not None else FakeProvider()
        self.sent = []
        self.web_apis = []
        self.db = FakeDB(
            conversations if conversations is not None else [],
            getattr(conversation_manager, "histories", None),
        )
        self.persona_manager = persona_manager
        self.conversation_manager = conversation_manager
        self.llm_tools = FakeToolManager([])
        self.agent_calls = []
        self.agent_response = FakeLLMResponse("agent 总结")

    def get_llm_tool_manager(self):
        return self.llm_tools

    async def get_current_chat_provider_id(self, umo):
        return "session-provider"

    async def tool_loop_agent(self, **kwargs):
        self.agent_calls.append(kwargs)
        return self.agent_response

    def register_web_api(self, route, handler, methods, desc):
        self.web_apis.append((route, handler, methods, desc))

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return True

    def get_db(self):
        return self.db

    def get_using_provider(self):
        return self.provider

    def get_provider_by_id(self, provider_id):
        return None


def make_plugin(
    monkeypatch,
    tmp_path,
    config,
    provider=None,
    conversations=None,
    persona_manager=None,
    conversation_manager=None,
):
    """构造一个数据目录指向 tmp_path 的插件实例。"""
    monkeypatch.setattr(main_mod, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    ctx = FakeContext(provider, conversations, persona_manager, conversation_manager)
    plugin = WatchdocPlugin(ctx, config)
    # 用例里的 config["targets"] 视为已经存进 KV 的监控项
    ctx.kv = FakeKV({"targets": config["targets"]} if "targets" in config else {})
    monkeypatch.setattr(plugin, "get_kv_data", ctx.kv.get)
    monkeypatch.setattr(plugin, "put_kv_data", ctx.kv.put)
    return plugin, ctx


def patch_fetch(monkeypatch, plugin, html):
    async def _fake_fetch(url):
        return html

    monkeypatch.setattr(plugin, "_fetch", _fake_fetch)


def patch_fetch_multi(monkeypatch, plugin, html_a, html_b):
    """按地址后缀返回不同正文，模拟一个监控项下的两篇独立文档。"""

    async def _fake_fetch(url):
        return html_b if "example.com/b" in url else html_a

    monkeypatch.setattr(plugin, "_fetch", _fake_fetch)


class FakeWebRequest:
    """Web API 请求替身，只提供 handler 用到的 json()。"""

    def __init__(self, payload):
        self._payload = payload

    async def json(self, default=None):
        return self._payload


async def _call_api(plugin, payload):
    """在绑好请求上下文的前提下调用保存接口。"""
    token = web_mod._request_var.set(FakeWebRequest(payload))
    try:
        return await plugin._api_save_targets()
    finally:
        web_mod._request_var.reset(token)


TARGET = {
    "id": "demo",
    "name": "示例文档",
    "url": "https://example.com/doc",
    "selector": ".markdown-body",
    "enabled": True,
    "sessions": ["fake:GroupMessage:1"],
}

# 不配置推送会话的监控项：验证结果只落盘、不尝试发送
SILENT_TARGET = {
    "id": "silent",
    "name": "静默文档",
    "url": "https://example.com/silent",
    "selector": ".markdown-body",
    "enabled": True,
}

# 多文档监控项：一次检测里多篇文档同组，变化的文档合并成一份总结
MULTI_TARGET = {
    "id": "multi",
    "name": "多文档示例",
    "documents": [
        {"url": "https://example.com/a", "selector": ".markdown-body", "name": "文档A"},
        {"url": "https://example.com/b", "selector": ".markdown-body", "name": "文档B"},
    ],
    "enabled": True,
}

# 第二篇文档用到的正文（与 HTML 区分，便于断言只变更其中一篇）
HTML2 = """<html><body>
<main class="markdown-body"><h1>批量接口</h1><p>批量返回结果。</p></main>
</body></html>"""


async def test_first_run_builds_baseline_without_llm(monkeypatch, tmp_path):
    """首次检查只建立基线，绝不调用 LLM。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    patch_fetch(monkeypatch, plugin, HTML)

    await plugin._check_all()

    snap = (
        plugin.snapshot_dir
        / text.safe_name("demo")
        / f"{text.safe_name('https://example.com/doc')}.md"
    )
    assert snap.exists()
    assert "非流式返回音频文件 URL" in snap.read_text(encoding="utf-8")
    assert "最后更新" not in snap.read_text(encoding="utf-8")  # 导航噪音被裁剪掉
    assert ctx.provider.calls == []
    assert ctx.sent == []


async def test_unchanged_content_stays_silent(monkeypatch, tmp_path):
    """内容没变时既不调 LLM 也不推送。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    patch_fetch(monkeypatch, plugin, HTML)

    await plugin._check_all()
    await plugin._check_all()

    assert ctx.provider.calls == []
    assert ctx.sent == []


async def test_change_triggers_summary_and_notify(monkeypatch, tmp_path):
    """检测到变化时总结、推送并存档。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    assert len(ctx.provider.calls) == 1
    assert len(ctx.sent) == 1
    assert list(plugin.history_dir.rglob("*.md"))


async def test_whitespace_only_change_is_not_reported(monkeypatch, tmp_path):
    """只有空白变化不应触发告警。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("<p>", "<p>   \n  "))
    await plugin._check_all()

    assert ctx.provider.calls == []


async def test_selector_miss_falls_back_to_full_page(monkeypatch, tmp_path):
    """选择器未命中时退化为整页比对，而不是直接失败。"""
    target = dict(TARGET, selector=".not-exist")
    plugin, _ = make_plugin(monkeypatch, tmp_path, {"targets": [target]})
    patch_fetch(monkeypatch, plugin, HTML)

    await plugin._check_all()

    snap = (
        plugin.snapshot_dir
        / text.safe_name("demo")
        / f"{text.safe_name('https://example.com/doc')}.md"
    )
    assert snap.exists()
    assert "页脚" in snap.read_text(encoding="utf-8")  # 整页包含页脚


async def test_empty_content_skipped(monkeypatch, tmp_path):
    """抓不到正文（如 SPA 页面）时不建立基线，避免污染后续比对。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    patch_fetch(monkeypatch, plugin, SPA_HTML)

    await plugin._check_all()

    assert not (
        plugin.snapshot_dir
        / text.safe_name("demo")
        / f"{text.safe_name('https://example.com/doc')}.md"
    ).exists()
    assert ctx.provider.calls == []


async def test_invalid_json_targets_is_tolerated(monkeypatch, tmp_path):
    """监控项配置不是合法 JSON 时不抛异常，只是没有监控项。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {"targets": "{不是 JSON"})
    patch_fetch(monkeypatch, plugin, HTML)

    await plugin._check_all()

    assert await plugin._load_targets() == []


async def test_ignore_patterns_suppresses_noise_only(monkeypatch, tmp_path):
    """忽略规则应吃掉动态噪音，但不能吃掉真正的正文变化。"""
    plugin, ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"targets": [TARGET], "ignore_patterns": [r"时间戳\d+"]},
    )
    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "非流式 时间戳111"))
    await plugin._check_all()

    # 只有时间戳在变：不应告警
    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "非流式 时间戳222"))
    await plugin._check_all()
    assert ctx.provider.calls == []

    # 正文真的改了：必须告警
    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式 时间戳333"))
    await plugin._check_all()
    assert len(ctx.provider.calls) == 1


async def test_invalid_ignore_pattern_does_not_crash(monkeypatch, tmp_path):
    """非法正则不应让整个检查崩掉。"""
    plugin, _ = make_plugin(
        monkeypatch, tmp_path, {"targets": [TARGET], "ignore_patterns": ["(["]}
    )
    patch_fetch(monkeypatch, plugin, HTML)

    await plugin._check_all()

    assert (
        plugin.snapshot_dir
        / text.safe_name("demo")
        / f"{text.safe_name('https://example.com/doc')}.md"
    ).exists()


async def test_notify_appends_pushed_text_to_history(monkeypatch, tmp_path):
    """推送成功后要写进该会话的对话历史，且是 user / assistant 成对写入。"""
    manager = FakeConversationManager(current={"a:GroupMessage:1": "cid-1"})
    plugin, _ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"targets": [{**TARGET, "sessions": ["a:GroupMessage:1"]}]},
        conversation_manager=manager,
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    umo, cid, history = manager.updates[0]
    assert (umo, cid) == ("a:GroupMessage:1", "cid-1")
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert "Watchdoc" in history[0]["content"]
    assert "示例文档" in history[0]["content"]
    assert "模拟总结" in history[1]["content"]


async def test_notify_creates_conversation_when_missing(monkeypatch, tmp_path):
    """会话从没说过话时没有当前对话，要新建一个再写历史。"""
    manager = FakeConversationManager()
    plugin, _ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"targets": [{**TARGET, "sessions": ["a:GroupMessage:1"]}]},
        conversation_manager=manager,
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    assert manager.created == ["a:GroupMessage:1"]
    assert manager.updates[0][1] == "cid-1"


async def test_history_failure_does_not_break_push(monkeypatch, tmp_path):
    """写历史失败不能连累推送本身。"""
    manager = FakeConversationManager(current={"a:GroupMessage:1": "cid-1"})

    async def boom(*args, **kwargs):
        raise RuntimeError("数据库只读")

    manager.update_conversation = boom
    plugin, ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"targets": [{**TARGET, "sessions": ["a:GroupMessage:1"]}]},
        conversation_manager=manager,
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()  # 不抛异常

    assert len(ctx.sent) == 1


async def test_target_without_sessions_archives_only(monkeypatch, tmp_path):
    """监控项没配推送会话时，变更仍然落盘，只是不推送。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [SILENT_TARGET]})
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    assert ctx.sent == []
    assert list(plugin.history_dir.rglob("*.md"))


async def test_llm_failure_falls_back_to_raw_diff(monkeypatch, tmp_path):
    """LLM 调用失败时降级为原始 diff，而不是丢掉这次变更。"""
    plugin, ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"targets": [TARGET]},
        provider=FakeProvider(raise_on_call=True),
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    assert len(ctx.sent) == 1
    assert "AI 总结失败" in str(ctx.sent[0][1].chain[0])


async def test_summarize_sends_full_diff_without_truncation(monkeypatch, tmp_path):
    """diff 必须完整送进模型，不做截断。

    截断会让模型看不到后半段变更，直接漏掉需要告警的内容——宁可多花 token，
    也不能丢信息。
    """
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    diff = "\n".join(f"+第 {i} 行变更内容" for i in range(2000))

    await plugin._summarize(
        TARGET, [{"url": "https://example.com/doc", "name": "示例文档", "diff": diff}]
    )

    prompt = ctx.provider.calls[0]
    assert "第 0 行" in prompt
    assert "第 1999 行" in prompt
    assert "截断" not in prompt


async def test_notify_pushes_to_every_configured_session(monkeypatch, tmp_path):
    """一份变更要推送到该监控项下的每一个会话，而不是只发第一个。"""
    target = {**TARGET, "sessions": ["a:GroupMessage:1", "b:GroupMessage:2"]}
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [target]})
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    assert [session for session, _ in ctx.sent] == [
        "a:GroupMessage:1",
        "b:GroupMessage:2",
    ]


async def test_notify_keeps_going_when_one_session_fails(monkeypatch, tmp_path):
    """某个会话推送失败不能连累其余会话。"""
    target = {**TARGET, "sessions": ["bad:GroupMessage:1", "good:GroupMessage:2"]}
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [target]})

    async def flaky(session, chain):
        if session.startswith("bad"):
            raise RuntimeError("平台不在线")
        ctx.sent.append((session, chain))
        return True

    monkeypatch.setattr(plugin.context, "send_message", flaky)

    await plugin._notify(
        target, [("变更摘要", ["bad:GroupMessage:1", "good:GroupMessage:2"])]
    )

    assert [session for session, _ in ctx.sent] == ["good:GroupMessage:2"]


ROLE_PROMPT = "你是 API 文档变更分析助手。"
PERSONA_SECTION = "\n# Persona Instructions\n\n{persona}\n"


def persona_section(persona: str) -> str:
    """与主 Agent 注入结果逐字一致的 system prompt，用于断言缓存前缀。"""
    return PERSONA_SECTION.format(persona=persona)


async def test_summarize_uses_persona_section_alone(monkeypatch, tmp_path):
    """有人格时 system 只放人格小节，前缀与普通请求一致以便命中提示缓存。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {})
    await plugin._summarize(TARGET, [{"diff": "diff 内容"}], "你是技术播报员")

    assert ctx.provider.system_prompts[0] == persona_section("你是技术播报员")


async def test_summarize_without_persona_uses_role_prompt(monkeypatch, tmp_path):
    """没有人格时用角色设定，不追加 Persona 小节。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {})
    await plugin._summarize(TARGET, [{"diff": "diff 内容"}])

    assert ctx.provider.system_prompts[0] == ROLE_PROMPT


async def test_summarize_keeps_no_guess_rule_in_user_turn(monkeypatch, tmp_path):
    """任务约束挪到 user 轮，system 前缀才不会被插件独有内容打断。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {})
    await plugin._summarize(TARGET, [{"diff": "diff 内容"}], "你是技术播报员")

    assert "不要臆测" in ctx.provider.calls[0]
    assert "不要臆测" not in ctx.provider.system_prompts[0]


def test_task_instruction_defaults():
    """两条默认指令的措辞差异只体现在「能不能用工具取回原文」。"""
    single = summary.task_instruction({})
    agent = summary.task_instruction({}, agent=True)

    assert single == constants.DEFAULT_INSTRUCTION
    assert "unified diff 作答" in single
    assert agent == constants.AGENT_INSTRUCTION
    assert "unified diff 和你用工具取回的原文作答" in agent
    # 除这一句之外，两条指令的其余部分应当一致
    assert agent.replace(" 和你用工具取回的原文", " ") == single


def test_task_instruction_prefers_custom():
    """监控项配了指令就用它，默认指令整句让位。"""
    target = {"instruction": "只关注鉴权相关的变化"}
    assert summary.task_instruction(target) == "只关注鉴权相关的变化"
    assert summary.task_instruction(target, agent=True) == "只关注鉴权相关的变化"


def test_task_instruction_blank_falls_back_to_default():
    """空白指令等价于没配，不能把空串塞进 prompt。"""
    assert summary.task_instruction({"instruction": "   \n "}) == (
        summary.task_instruction({})
    )
    assert summary.task_instruction({"instruction": None}) == (
        summary.task_instruction({})
    )


async def test_summarize_uses_custom_instruction(monkeypatch, tmp_path):
    """自定义指令要真的送进 user 轮，并取代默认指令。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {})
    await plugin._summarize(
        {**TARGET, "instruction": "只关注鉴权相关的变化"}, [{"diff": "diff"}]
    )

    assert "只关注鉴权相关的变化" in ctx.provider.calls[0]
    assert "300 字以内" not in ctx.provider.calls[0]


async def test_agent_prompt_uses_custom_instruction(monkeypatch, tmp_path):
    """agent 模式同样用自定义指令，且不额外拼接默认措辞。"""
    target = {**TARGET, "instruction": "只关注鉴权相关的变化"}
    plugin, ctx = make_plugin(
        monkeypatch, tmp_path, {"agent_mode": True, "targets": [target]}
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    prompt = ctx.agent_calls[0]["prompt"]
    assert "只关注鉴权相关的变化" in prompt
    assert "300 字以内" not in prompt


def test_default_agent_steps_enables_budget_notices():
    """步数预算 >= 16 时 AstrBot 才会在 80/90/95% 处提前提醒模型收尾。"""
    assert constants.DEFAULT_AGENT_STEPS >= 16


async def test_api_get_targets_exposes_default_instruction(monkeypatch, tmp_path):
    """Page 靠这个字段预填输入框，默认文案由后端单点维护。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})

    data = json.loads((await plugin._api_get_targets()).body)
    assert data["default_instruction"] == constants.DEFAULT_INSTRUCTION
    assert data["targets"][0]["id"] == "demo"


async def test_default_instruction_follows_agent_mode(monkeypatch, tmp_path):
    """开着 agent 时预填的应是 agent 版文案，否则模型不知道可以查原文。"""
    plugin, _ = make_plugin(
        monkeypatch, tmp_path, {"agent_mode": True, "targets": [TARGET]}
    )

    data = json.loads((await plugin._api_get_targets()).body)
    assert data["default_instruction"] == constants.AGENT_INSTRUCTION


async def test_resolve_persona_without_manager(monkeypatch, tmp_path):
    """没有 persona_manager 时返回 None，不能抛异常。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    assert await plugin._resolve_persona("a:GroupMessage:1") is None


async def test_resolve_persona_passes_conversation_persona_id(monkeypatch, tmp_path):
    """会话自带的人格 ID 与平台名要传给人格管理器解析。"""
    conversations = [FakeConversation("a:GroupMessage:1", "a", "persona-7")]
    manager = FakePersonaManager({"a:GroupMessage:1": "人格七"})
    plugin, _ = make_plugin(
        monkeypatch, tmp_path, {}, conversations=conversations, persona_manager=manager
    )

    persona = await plugin._resolve_persona("a:GroupMessage:1")
    assert persona["prompt"] == "人格七"
    assert manager.calls == [("a:GroupMessage:1", "persona-7", "a")]


async def test_resolve_persona_without_conversation(monkeypatch, tmp_path):
    """会话还没建立时 persona_id 为 None，交给管理器走默认人格。"""
    manager = FakePersonaManager({"a:GroupMessage:1": "默认人格"})
    plugin, _ = make_plugin(
        monkeypatch, tmp_path, {}, conversations=[], persona_manager=manager
    )

    persona = await plugin._resolve_persona("a:GroupMessage:1")
    assert persona["prompt"] == "默认人格"
    assert manager.calls[0][1] is None


async def test_each_persona_gets_its_own_summary(monkeypatch, tmp_path):
    """人格不同的会话各生成一份总结，人格相同的共用一份。"""
    target = {
        **TARGET,
        "sessions": ["a:GroupMessage:1", "b:GroupMessage:2", "c:GroupMessage:3"],
    }
    conversations = [
        FakeConversation("a:GroupMessage:1", "a", "p1"),
        FakeConversation("b:GroupMessage:2", "b", "p2"),
        FakeConversation("c:GroupMessage:3", "c", "p2"),
    ]
    manager = FakePersonaManager(
        {
            "a:GroupMessage:1": "人格甲",
            "b:GroupMessage:2": "人格乙",
            "c:GroupMessage:3": "人格乙",
        }
    )
    plugin, ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"targets": [target]},
        conversations=conversations,
        persona_manager=manager,
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    # 两种人格 → 两次 LLM，而不是每个会话一次
    assert len(ctx.provider.calls) == 2
    assert sorted(ctx.provider.system_prompts) == sorted(
        [persona_section("人格甲"), persona_section("人格乙")]
    )
    assert [session for session, _ in ctx.sent] == [
        "a:GroupMessage:1",
        "b:GroupMessage:2",
        "c:GroupMessage:3",
    ]


async def test_persona_disabled_shares_one_summary(monkeypatch, tmp_path):
    """关闭人格开关时所有会话共用一份不含人格的总结。"""
    target = {**TARGET, "sessions": ["a:GroupMessage:1", "b:GroupMessage:2"]}
    manager = FakePersonaManager(
        {"a:GroupMessage:1": "人格甲", "b:GroupMessage:2": "人格乙"}
    )
    plugin, ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"use_session_persona": False, "targets": [target]},
        persona_manager=manager,
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    assert len(ctx.provider.calls) == 1
    assert ctx.provider.system_prompts[0] == ROLE_PROMPT
    assert len(ctx.sent) == 2
    # 关闭时不该再去解析人格
    assert manager.calls == []


async def test_agent_uses_default_provider_when_unset(monkeypatch, tmp_path):
    """没指定总结模型时用默认对话模型，而不是某个推送会话自己的模型。"""
    plugin, ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"agent_mode": True, "targets": [{**TARGET, "sessions": ["a:GroupMessage:1"]}]},
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    assert ctx.agent_calls[0]["chat_provider_id"] == "default-provider"


async def test_agent_prefers_configured_provider(monkeypatch, tmp_path):
    """指定了总结模型时，两条路径都该用它。"""
    plugin, ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {
            "agent_mode": True,
            "summary_provider": "picked-provider",
            "targets": [{**TARGET, "sessions": ["a:GroupMessage:1"]}],
        },
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    assert ctx.agent_calls[0]["chat_provider_id"] == "picked-provider"


async def test_agent_mode_off_keeps_single_call(monkeypatch, tmp_path):
    """默认关闭 agent 循环，成本与行为都和单次调用一致。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    assert ctx.agent_calls == []
    assert len(ctx.provider.calls) == 1


async def test_agent_mode_runs_tool_loop(monkeypatch, tmp_path):
    """开启 agent 后走工具循环，并把人格、轮次上限、diff 一并带过去。"""
    plugin, ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"agent_mode": True, "agent_max_steps": 7, "targets": [TARGET]},
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    assert len(ctx.agent_calls) == 1
    call = ctx.agent_calls[0]
    assert call["max_steps"] == 7
    assert "unified diff" in call["prompt"]
    assert "300 字以内" in call["prompt"]
    # 走 agent 就不该再发一次单次调用
    assert ctx.provider.calls == []
    assert "agent 总结" in str(ctx.sent[0][1].chain[0])


async def test_agent_mode_applies_persona(monkeypatch, tmp_path):
    """agent 模式同样要带上会话人格。"""
    manager = FakePersonaManager({"a:GroupMessage:1": "人格甲"})
    plugin, ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"agent_mode": True, "targets": [{**TARGET, "sessions": ["a:GroupMessage:1"]}]},
        persona_manager=manager,
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    system_prompt = ctx.agent_calls[0]["system_prompt"]
    assert system_prompt.endswith("\n# Persona Instructions\n\n人格甲\n")


async def test_agent_error_falls_back_to_single_call(monkeypatch, tmp_path):
    """agent 返回错误态时退回单次调用，不能把这次变更吞掉。"""
    plugin, ctx = make_plugin(
        monkeypatch, tmp_path, {"agent_mode": True, "targets": [TARGET]}
    )
    ctx.agent_response = FakeLLMResponse("模型崩了", role="err")
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()

    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    assert len(ctx.provider.calls) == 1
    assert len(ctx.sent) == 1


async def test_doc_tools_survive_persona_whitelist(monkeypatch, tmp_path):
    """人格的 tools 白名单没写本插件工具时，查阅原文的工具仍要可用。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {})
    ctx.llm_tools = FakeToolManager([make_tool("some_other_tool")])

    tools = plugin._build_tool_set(["some_other_tool"])
    names = [tool.name for tool in tools]
    assert "some_other_tool" in names
    assert "watchdoc_read_document" in names
    assert "watchdoc_search_document" in names


async def test_build_tool_set_drops_inactive_tools(monkeypatch, tmp_path):
    """人格不限工具时，仍要剔除被停用的工具。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {})
    ctx.llm_tools = FakeToolManager(
        [make_tool("alive"), make_tool("disabled", active=False)]
    )

    names = [tool.name for tool in plugin._build_tool_set(None)]
    assert "alive" in names
    assert "disabled" not in names


def set_run(plugin, after="", before="x"):
    """塞入一次变更数据，供查阅工具使用。"""
    plugin._runs["a:GroupMessage:1"] = {
        "target": TARGET,
        "docs": [
            {
                "url": "https://example.com/doc",
                "name": "示例文档",
                "before": before,
                "after": after,
                "diff": "diff",
            }
        ],
    }
    return FakeEvent("a:GroupMessage:1")


async def test_tool_read_document_reads_line_range(monkeypatch, tmp_path):
    """按行号区间读原文，返回内容带行号，便于和 diff 的行号对照。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    event = set_run(plugin, after="\n".join(f"第{i}行" for i in range(1, 301)))

    result = await plugin._tool_read_document(
        event, scope="after", start_line=100, line_count=3
    )
    assert result.startswith("变更后原文共 300 行，以下是第 100-102 行：")
    assert "100: 第100行" in result
    assert "99: 第99行" not in result
    assert "103: 第103行" not in result
    assert "继续读请传 start_line=103" in result


async def test_tool_read_document_default_range(monkeypatch, tmp_path):
    """不传行号时读开头若干行。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    event = set_run(plugin, after="\n".join(f"第{i}行" for i in range(1, 301)))

    result = await plugin._tool_read_document(event, scope="after")
    assert "以下是第 1-200 行" in result


async def test_tool_read_document_clamps_range(monkeypatch, tmp_path):
    """起始行超出总行数、读取量超出末尾时都要收敛，不能抛异常。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    event = set_run(plugin, after="\n".join(f"第{i}行" for i in range(1, 21)))

    assert "第 20-20 行" in await plugin._tool_read_document(
        event, scope="after", start_line=999, line_count=5
    )
    # 读到末尾就不该再提示继续读
    assert "继续读" not in await plugin._tool_read_document(
        event, scope="after", start_line=18, line_count=10
    )


async def test_tool_read_document_respects_char_budget(monkeypatch, tmp_path):
    """单行极长时按字符预算收住，不能一次塞爆上下文。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    event = set_run(plugin, after="\n".join(["x" * 1000] * 100))

    result = await plugin._tool_read_document(
        event, scope="after", start_line=1, line_count=100
    )
    assert len(result) < constants.TOOL_READ_CHARS * 2


async def test_tool_read_document_scopes_before_after(monkeypatch, tmp_path):
    """scope 决定读变更前还是变更后。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    event = set_run(plugin, before="旧内容", after="新内容")

    assert "旧内容" in await plugin._tool_read_document(event, scope="before")
    assert "新内容" in await plugin._tool_read_document(event, scope="after")


async def test_tool_search_document_returns_line_numbers(monkeypatch, tmp_path):
    """搜索结果带行号，便于接着按行号区间读。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    lines = ["填充"] * 9 + ["语音合成接口"] + ["填充"] * 10
    event = set_run(plugin, after="\n".join(lines))

    result = await plugin._tool_search_document(event, "语音合成", scope="after")
    assert "找到 1 处" in result
    assert "10: 语音合成接口" in result
    assert "未找到「不存在」" in await plugin._tool_search_document(
        event, "不存在", scope="after"
    )


async def test_doc_tools_without_active_run(monkeypatch, tmp_path):
    """没有正在处理的变更时工具要给出说明，而不是抛异常。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    event = FakeEvent("a:GroupMessage:1")

    assert "没有正在处理" in await plugin._tool_read_document(event, "after")
    assert "没有正在处理" in await plugin._tool_search_document(
        event, "关键词", "after"
    )


async def test_sessions_field_is_normalized_on_load(monkeypatch, tmp_path):
    """sessions 可能是手改出来的字符串或含空值，读取时要收敛成去重列表。"""
    plugin, _ctx = make_plugin(monkeypatch, tmp_path, {})
    await plugin._save_targets(
        [
            {**TARGET, "sessions": "solo:GroupMessage:9"},
            {
                **SILENT_TARGET,
                "sessions": ["x:GroupMessage:1", " x:GroupMessage:1 ", ""],
            },
        ]
    )

    loaded = await plugin._load_targets()

    assert loaded[0]["sessions"] == ["solo:GroupMessage:9"]
    assert loaded[1]["sessions"] == ["x:GroupMessage:1"]


async def test_api_sessions_lists_distinct_umos(monkeypatch, tmp_path):
    """会话下拉要给出去重的 UMO，同一会话出现多条对话记录时不能重复。"""
    conversations = [
        FakeConversation("a:GroupMessage:1", "aiocqhttp"),
        FakeConversation("a:GroupMessage:1", "aiocqhttp"),
        FakeConversation("b:GroupMessage:2", "aiocqhttp"),
        FakeConversation("", "aiocqhttp"),
    ]
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {}, conversations=conversations)

    resp = await plugin._api_sessions()
    payload = json.loads(resp.body.decode())

    assert [item["umo"] for item in payload["sessions"]] == [
        "a:GroupMessage:1",
        "b:GroupMessage:2",
    ]
    assert ctx.db.calls == [(1, 500)]


async def test_cron_registration_is_idempotent(monkeypatch, tmp_path):
    """重复注册不应在面板上累积多个任务。"""
    plugin, ctx = make_plugin(
        monkeypatch, tmp_path, {"targets": [TARGET], "cron": "0 */6 * * *"}
    )

    await plugin._sync_cron_job()
    await plugin._sync_cron_job()
    await plugin._sync_cron_job()

    assert len(ctx.cron_manager.jobs) == 1


async def test_initialize_registers_job_when_targets_exist(monkeypatch, tmp_path):
    """插件加载/重载（initialize）就要把任务同步出来。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})

    await plugin.initialize()

    assert "watchdoc:check" in ctx.cron_manager.jobs


async def test_initialize_skips_job_without_targets(monkeypatch, tmp_path):
    """没有监控项时不该凭空占一个未来任务。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {})

    await plugin.initialize()

    assert ctx.cron_manager.jobs == {}


async def test_sync_removes_job_after_targets_cleared(monkeypatch, tmp_path):
    """监控项清空后任务要跟着撤掉。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    await plugin.initialize()
    assert "watchdoc:check" in ctx.cron_manager.jobs

    await plugin._save_targets([])
    await plugin._sync_cron_job()

    assert ctx.cron_manager.jobs == {}


async def test_sync_restores_manually_deleted_job(monkeypatch, tmp_path):
    """用户在面板上删掉任务后，再次同步监控项要能自愈。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    await plugin.initialize()
    await ctx.cron_manager.delete_job("watchdoc:check")
    assert ctx.cron_manager.jobs == {}

    await plugin._sync_cron_job()

    assert "watchdoc:check" in ctx.cron_manager.jobs
    assert ctx.cron_manager.jobs["watchdoc:check"].cron_expression == "0 */6 * * *"


async def test_sync_preserves_manually_disabled_job(monkeypatch, tmp_path):
    """手动暂停过的任务，重新注册时不应被悄悄拉起来。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    await plugin.initialize()
    ctx.cron_manager.jobs["watchdoc:check"].enabled = False

    await plugin._sync_cron_job()

    assert ctx.cron_manager.jobs["watchdoc:check"].enabled is False


async def test_sync_picks_up_cron_config_change(monkeypatch, tmp_path):
    """配置里的 cron 改了，重新同步要换上新表达式。"""
    plugin, ctx = make_plugin(
        monkeypatch, tmp_path, {"targets": [TARGET], "cron": "0 */6 * * *"}
    )
    await plugin.initialize()
    plugin.config["cron"] = "0 * * * *"

    await plugin._sync_cron_job()

    assert ctx.cron_manager.jobs["watchdoc:check"].cron_expression == "0 * * * *"


async def test_api_save_targets_syncs_cron_job(monkeypatch, tmp_path):
    """面板保存监控项后未来任务要立刻出现。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {})
    await plugin.initialize()
    assert ctx.cron_manager.jobs == {}

    resp = await _call_api(
        plugin,
        {
            "targets": [
                {
                    **TARGET,
                    "documents": [
                        {"url": "https://example.com/doc", "selector": ".markdown-body"}
                    ],
                }
            ]
        },
    )

    assert json.loads(resp.body.decode())["saved"] is True
    assert "watchdoc:check" in ctx.cron_manager.jobs


async def test_terminate_clears_own_jobs(monkeypatch, tmp_path):
    """插件卸载时只清理自己的任务。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    await plugin.initialize()
    ctx.cron_manager.jobs["other:job"] = FakeJob("other:job", "other:job")

    await plugin.terminate()

    assert "watchdoc:check" not in ctx.cron_manager.jobs
    assert "other:job" in ctx.cron_manager.jobs


async def test_history_rotation_keeps_limit(monkeypatch, tmp_path):
    """存档数量超过上限时删除最旧的，避免无限增长。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    for i in range(constants.MAX_HISTORY_PER_TARGET + 10):
        plugin._archive(
            TARGET,
            {
                "docs": [
                    {
                        "url": "https://example.com/doc",
                        "name": "示例文档",
                        "diff": f"diff-{i}",
                    }
                ]
            },
            [("summary", [])],
        )

    files = sorted(plugin.history_dir.rglob("*.md"))
    assert len(files) == constants.MAX_HISTORY_PER_TARGET


def test_safe_name_rejects_path_traversal():
    """监控项 ID 不能用来穿越目录。"""
    assert "/" not in text.safe_name("../../etc/passwd")
    assert "\\" not in text.safe_name("..\\windows")
    assert text.safe_name("") == "target"


async def test_format_list_reports_missing_baseline(monkeypatch, tmp_path):
    """清单里应区分「已建立基线」与「尚未建立基线」。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    out = await plugin._format_list()
    assert "尚未建立基线" in out


async def test_empty_targets_message(monkeypatch, tmp_path):
    """没有监控项时给出可操作的提示，而不是空白。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {"targets": "[]"})
    assert "监控项" in await plugin._format_list()


@pytest.mark.parametrize("bad", ["{", "[1,2", "null", '{"a": 1}'])
async def test_load_targets_rejects_non_list(monkeypatch, tmp_path, bad):
    """KV 里取到的不是数组时一律当作没有监控项。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {"targets": bad})
    assert await plugin._load_targets() == []


async def test_targets_accepts_real_list(monkeypatch, tmp_path):
    """配置已是列表（而非 JSON 文本）时直接返回。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    assert await plugin._load_targets() == [TARGET]


def test_snapshot_dir_under_plugin_data(monkeypatch, tmp_path):
    """快照必须落在 plugin_data 下，不能写进插件自身目录（否则更新会丢失）。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    assert plugin.data_dir == tmp_path / "astrbot_plugin_watchdoc"
    assert plugin.snapshot_dir.parent == plugin.data_dir
    assert plugin.history_dir.parent == plugin.data_dir


async def test_probe_prefers_stable_over_larger(monkeypatch, tmp_path):
    """语义化选择器应优先于文本量更大的构建产物型选择器。"""
    # 测试用 HTML 远小于真实页面，临时调低最小文本阈值
    monkeypatch.setattr(fetch, "MIN_TEXT_LEN", 5)
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    patch_fetch(monkeypatch, plugin, NOISY_HTML)

    out = await plugin._probe_url("https://example.com/doc")

    # 稳定的必须整体排在易失效的之前
    assert out.index("[稳定]") < out.index("[易失效]")
    assert out.index(".markdown-body") < out.index(".css-1a2b3c")
    # 建议本身绝不能是构建产物型那个，即使它文本量最大
    suggestion = out.split("建议填：")[1].split("\n")[0]
    assert "css-1a2b3c" not in suggestion


async def test_probe_skips_classless_fallback(monkeypatch, tmp_path):
    """没有 class 的兜底 div 无法写成选择器，不能推荐给用户。"""
    monkeypatch.setattr(fetch, "MIN_TEXT_LEN", 5)
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    patch_fetch(monkeypatch, plugin, BARE_DIV_HTML)

    out = await plugin._probe_url("https://example.com/doc")

    assert "div（无 class）" not in out
    suggestion = out.split("建议填：")[1].split("\n")[0]
    assert suggestion in ("main", ".markdown-body")


async def test_read_body_rejects_oversized_declared_length():
    """声明的 Content-Length 超限时，一个字节都不该读。"""
    resp = FakeResp(b"x" * 1024, declared=10 * 1024 * 1024)

    with pytest.raises(fetch.PageTooLargeError):
        await fetch.read_body(resp, 1024)


async def test_read_body_rejects_oversized_stream():
    """chunked 响应没有声明长度，必须靠实际读取量卡住。"""
    resp = FakeResp(b"x" * 4096, declared=None)

    with pytest.raises(fetch.PageTooLargeError):
        await fetch.read_body(resp, 1024)


async def test_read_body_reads_within_limit():
    """未超限时正常读完，并按响应的字符集解码。"""
    resp = FakeResp("正文".encode(), declared=6, charset="utf-8")

    assert await fetch.read_body(resp, 1024) == "正文"


async def test_read_body_falls_back_on_unusable_charset():
    """声明的字符集不可识别时回落 UTF-8，而不是把解码异常抛出去。"""
    resp = FakeResp("正文".encode(), declared=6, charset="not-a-codec")

    assert await fetch.read_body(resp, 1024) == "正文"

    resp = FakeResp("正文".encode(), declared=6, charset=None)
    assert await fetch.read_body(resp, 1024) == "正文"


async def test_normalize_runs_off_the_event_loop(monkeypatch, tmp_path):
    """归一化跑在线程里，避免用户正则的灾难性回溯拖死事件循环。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    patch_fetch(monkeypatch, plugin, HTML)

    threads = []
    real = plugin._normalize

    def spy(text, target):
        threads.append(threading.current_thread())
        return real(text, target)

    monkeypatch.setattr(plugin, "_normalize", spy)

    await plugin._check_all()

    assert threads, "归一化没有被调用"
    assert all(t is not threading.main_thread() for t in threads)


async def test_probe_fallback_only_weighs_top_level_divs(monkeypatch, tmp_path):
    """兜底扫描只称最外层 div，嵌套 div 不应被反复取文本。"""
    monkeypatch.setattr(fetch, "MIN_TEXT_LEN", 5)
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    nested = (
        "<html><body><div class='outer'>"
        + "<div class='mid'>" * 6
        + "正文正文正文正文正文正文"
        + "</div>" * 6
        + "</div></body></html>"
    )
    patch_fetch(monkeypatch, plugin, nested)

    weighed = []
    real = Tag.get_text

    def spy(self, *args, **kwargs):
        if self.name == "div":
            weighed.append(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Tag, "get_text", spy)

    await plugin._probe_url("https://example.com/doc")

    assert weighed, "没有扫描任何 div"
    assert all(div.find_parent("div") is None for div in weighed)


async def test_api_save_targets_drops_snapshot_of_removed_item(monkeypatch, tmp_path):
    """删掉的监控项要连基线快照一起清掉。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {"targets": [TARGET]})
    snap = (
        plugin.snapshot_dir
        / text.safe_name("demo")
        / f"{text.safe_name('https://example.com/doc')}.md"
    )
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text("旧基线", encoding="utf-8")

    await _call_api(plugin, {"targets": []})

    assert not snap.exists()


async def test_api_save_targets_keeps_snapshot_of_remaining_item(monkeypatch, tmp_path):
    """仍在列表里的监控项，其每篇文档的基线快照不能被误删。"""
    target = {
        **TARGET,
        "documents": [{"url": "https://example.com/doc", "selector": ".markdown-body"}],
    }
    plugin, _ = make_plugin(monkeypatch, tmp_path, {"targets": [target]})
    doc_dir = plugin.snapshot_dir / text.safe_name("demo")
    doc_dir.mkdir(parents=True, exist_ok=True)
    snap = doc_dir / f"{text.safe_name('https://example.com/doc')}.md"
    snap.write_text("旧基线", encoding="utf-8")

    await _call_api(plugin, {"targets": [target]})

    assert snap.exists()


async def test_probe_reports_js_rendered_page(monkeypatch, tmp_path):
    """抓不到正文时应明确提示可能需要 JS 渲染，而不是给个空建议。"""
    monkeypatch.setattr(fetch, "MIN_TEXT_LEN", 5)
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    patch_fetch(monkeypatch, plugin, SPA_HTML)

    out = await plugin._probe_url("https://example.com/spa")

    assert "JS 渲染" in out
    assert "建议填" not in out


async def test_targets_roundtrip_through_kv(monkeypatch, tmp_path):
    """监控项存进插件 KV 后能原样读回。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {})

    await plugin._save_targets([{"id": "from-pages"}])

    assert await plugin._load_targets() == [{"id": "from-pages"}]
    assert ctx.kv.puts == [(constants.TARGETS_KEY, [{"id": "from-pages"}])]


async def test_corrupted_kv_value_does_not_break_plugin(monkeypatch, tmp_path):
    """KV 里存了坏数据时当作没有监控项，巡检本身不能崩。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {})
    ctx.kv.data[constants.TARGETS_KEY] = "{坏掉的 JSON"

    await plugin._check_all()

    assert await plugin._load_targets() == []
    assert ctx.sent == []


def test_web_apis_use_plugin_name_prefix(monkeypatch, tmp_path):
    """Page 后端路由必须带插件名前缀，否则 Dashboard 转发不到。"""
    _, ctx = make_plugin(monkeypatch, tmp_path, {})

    routes = [route for route, _handler, _m, _d in ctx.web_apis]
    assert routes, "没有注册任何 Web API"
    for route in routes:
        assert route.startswith(f"/{constants.PLUGIN_NAME}/")

    endpoints = {route.rsplit("/", 1)[-1] for route in routes}
    assert {"targets", "preview", "probe"} <= endpoints


def test_sanitize_for_preview_strips_scripts_and_handlers(monkeypatch, tmp_path):
    """预览用的 HTML 不能带脚本和事件属性，否则等于在同源环境执行外部代码。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    raw = (
        "<html><body><script>alert(1)</script>"
        '<img src="/img/a.png" onclick="steal()" />'
        '<a href="/other">跳转</a><iframe src="x"></iframe></body></html>'
    )

    out = plugin._sanitize_for_preview(raw, "https://example.com/docs/page")

    assert "alert(1)" not in out
    assert "onclick" not in out
    assert "<iframe" not in out
    assert 'href="/other"' not in out
    # 相对路径必须改写成绝对地址，Page 的 CSP 不允许注入 base 标签
    assert "https://example.com/img/a.png" in out


def test_sanitize_for_preview_makes_lazy_images_loadable(monkeypatch, tmp_path):
    """预览区的图片必须能直接加载。

    原页面常带 `loading="lazy"` 且把真实地址藏在 data-src 里。Shadow DOM 预览
    没有滚动视口，懒加载永不触发，浏览器会一直抛 Intervention 警告，用户看到
    的也是一片空白。
    """
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    raw = (
        "<html><body>"
        '<img data-src="/img/lazy.png" loading="lazy" />'
        '<img data-src="/img/bare.png" />'
        "</body></html>"
    )

    out = plugin._sanitize_for_preview(raw, "https://example.com/docs/page")

    assert "loading=" not in out
    assert "https://example.com/img/lazy.png" in out
    assert "https://example.com/img/bare.png" in out


@pytest.mark.parametrize(
    "selector,expected",
    [
        ("main", True),
        ("article", True),
        (".markdown-body", True),
        (".theme-doc-markdown", True),
        ("#content", True),
        ("div:nth-child(2)", False),
        (".css-1a2b3c", False),
        ("div.relative.antialiased.text-gray-500", False),
    ],
)
def test_looks_stable_judges_selector(selector, expected):
    """稳定性判断要能挡住位置编号、构建 hash 与过长的 class 链。"""
    assert fetch.looks_stable(selector) is expected


# ---------------------------------------------------------------------------
# 自动抓取文档标题（extract_title + 预览接口回传 title）
# ---------------------------------------------------------------------------

TITLE_HTML = """<html><head><title>非实时语音合成 · 接口文档</title></head>
<body><main class="markdown-body"><h1>非实时语音合成</h1><p>内容</p></main></body></html>"""

OG_TITLE_HTML = """<html><head>
<meta property="og:title" content="OpenGraph 标题" />
</head><body><h1>正文 H1</h1></body></html>"""

H1_TITLE_HTML = (
    """<html><body><h1>  仅 H1 标题  </h1><p>无 title 无 og</p></body></html>"""
)

NO_TITLE_HTML = """<html><body><p>没有任何标题标签的页面</p></body></html>"""


@pytest.mark.parametrize(
    "html,expected",
    [
        (TITLE_HTML, "非实时语音合成 · 接口文档"),
        (OG_TITLE_HTML, "OpenGraph 标题"),
        (H1_TITLE_HTML, "仅 H1 标题"),
        (NO_TITLE_HTML, ""),
    ],
)
def test_extract_title_falls_back(html, expected):
    """extract_title 的回退链：<title> → og:title → 首 <h1> → 空串。"""
    soup = fetch.build_soup(html)
    assert fetch.extract_title(soup) == expected


class FakePreviewRequest:
    """预览接口只用到 request.query.get，这里给一个最小替身。"""

    def __init__(self, query):
        self.query = query


async def test_api_preview_returns_title(monkeypatch, tmp_path):
    """预览接口必须把抓取页的标题回传给前端，供文档列表展示。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    patch_fetch(monkeypatch, plugin, TITLE_HTML)

    token = web_mod._request_var.set(
        FakePreviewRequest({"url": "https://example.com/doc"})
    )
    try:
        resp = await plugin._api_preview()
    finally:
        web_mod._request_var.reset(token)

    body = json.loads(resp.body.decode())
    assert body["title"] == "非实时语音合成 · 接口文档"
    # 原有字段不能丢
    assert "html" in body
    assert isinstance(body["textLength"], int)


async def test_api_save_targets_preserves_doc_name(monkeypatch, tmp_path):
    """保存监控项时文档的 name（自动抓取标题）必须被保留，不能静默丢弃。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {})
    resp = await _call_api(
        plugin,
        {
            "targets": [
                {
                    "id": "demo",
                    "documents": [
                        {
                            "url": "https://example.com/doc",
                            "selector": ".markdown-body",
                            "name": "非实时语音合成 · 接口文档",
                        }
                    ],
                }
            ]
        },
    )
    assert json.loads(resp.body.decode())["saved"] is True
    saved = ctx.kv.puts[-1][1]
    doc = saved[0]["documents"][0]
    assert doc["name"] == "非实时语音合成 · 接口文档"
    assert doc["url"] == "https://example.com/doc"
    assert doc["selector"] == ".markdown-body"


# ---------------------------------------------------------------------------
# 文档监控组（多文档）回归
# ---------------------------------------------------------------------------


async def test_multi_doc_builds_per_doc_snapshots(monkeypatch, tmp_path):
    """多文档监控项给每篇文档单独建档，目录按监控项 ID 归组。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {"targets": [MULTI_TARGET]})
    patch_fetch_multi(monkeypatch, plugin, HTML, HTML2)

    await plugin._check_all()

    dir_a = (
        plugin.snapshot_dir
        / text.safe_name("multi")
        / f"{text.safe_name('https://example.com/a')}.md"
    )
    dir_b = (
        plugin.snapshot_dir
        / text.safe_name("multi")
        / f"{text.safe_name('https://example.com/b')}.md"
    )
    assert dir_a.exists()
    assert "非流式返回音频文件 URL" in dir_a.read_text(encoding="utf-8")
    assert dir_b.exists()
    assert "批量返回结果" in dir_b.read_text(encoding="utf-8")


async def test_multi_doc_only_changed_doc_summarized(monkeypatch, tmp_path):
    """一次检测里只有部分文档变化时，未变的文档不进总结、也不重复调用。"""
    target = {**MULTI_TARGET, "sessions": ["fake:GroupMessage:1"]}
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [target]})
    patch_fetch_multi(monkeypatch, plugin, HTML, HTML2)
    await plugin._check_all()

    changed_a = HTML.replace("非流式", "流式")

    async def _fake(url):
        return changed_a if "example.com/a" in url else HTML2

    monkeypatch.setattr(plugin, "_fetch", _fake)
    await plugin._check_all()

    assert len(ctx.provider.calls) == 1
    prompt = ctx.provider.calls[0]
    assert "https://example.com/a" in prompt
    assert "https://example.com/b" not in prompt
    # 合并成一份总结的措辞应出现
    assert "同属一个监控组" in prompt
    assert len(ctx.sent) == 1
    assert list(plugin.history_dir.rglob("*.md"))


async def test_multi_doc_both_changed_merge_into_one_summary(monkeypatch, tmp_path):
    """两篇都变化时不该各来一份，而应并成一份关联总结。"""
    target = {**MULTI_TARGET, "sessions": ["fake:GroupMessage:1"]}
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [target]})
    patch_fetch_multi(monkeypatch, plugin, HTML, HTML2)
    await plugin._check_all()

    async def _fake(url):
        return (
            HTML2.replace("批量", "单条")
            if "example.com/b" in url
            else HTML.replace("非流式", "流式")
        )

    monkeypatch.setattr(plugin, "_fetch", _fake)
    await plugin._check_all()

    assert len(ctx.provider.calls) == 1
    prompt = ctx.provider.calls[0]
    assert "https://example.com/a" in prompt
    assert "https://example.com/b" in prompt
    assert "文档 2" in prompt  # 两篇分别编号列出


async def test_multi_doc_distinct_targets_summarize_independently(
    monkeypatch, tmp_path
):
    """文档集不同的监控项各自独立总结，不会把别的组的文档混进来。"""
    other = {
        "id": "other",
        "name": "另一组",
        "documents": [{"url": "https://example.com/c", "selector": ".markdown-body"}],
        "enabled": True,
        "sessions": ["fake:GroupMessage:2"],
    }
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {"targets": [MULTI_TARGET, other]})
    html_c = (
        "<html><body><main class='markdown-body'>"
        "<h1>配套文档</h1><p>旧内容</p></main></body></html>"
    )

    async def _fake(url):
        return (
            html_c
            if "example.com/c" in url
            else HTML2
            if "example.com/b" in url
            else HTML
        )

    monkeypatch.setattr(plugin, "_fetch", _fake)
    await plugin._check_all()

    # 第二次：改 A（属 MULTI_TARGET）与 C（属 other）
    changed_a = HTML.replace("非流式", "流式")
    changed_c = html_c.replace("旧内容", "新内容")

    async def _fake(url):
        return (
            changed_c
            if "example.com/c" in url
            else HTML2
            if "example.com/b" in url
            else changed_a
        )

    monkeypatch.setattr(plugin, "_fetch", _fake)
    await plugin._check_all()

    assert len(ctx.provider.calls) == 2
    # 每个监控项的总结只包含自己的文档，不串到另一组
    a_idx = 0 if "https://example.com/a" in ctx.provider.calls[0] else 1
    assert "https://example.com/a" in ctx.provider.calls[a_idx]
    assert "https://example.com/c" not in ctx.provider.calls[a_idx]
    assert "https://example.com/c" in ctx.provider.calls[1 - a_idx]
    assert "https://example.com/a" not in ctx.provider.calls[1 - a_idx]


async def test_tool_read_document_selects_doc_by_index(monkeypatch, tmp_path):
    """多文档变更里用 #2 取第二篇；省略 doc 必须显式指定而非默认第一篇。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    plugin._runs["a:GroupMessage:1"] = {
        "target": MULTI_TARGET,
        "docs": [
            {
                "url": "https://example.com/a",
                "name": "文档A",
                "before": "旧A",
                "after": "新A",
            },
            {
                "url": "https://example.com/b",
                "name": "文档B",
                "before": "旧B",
                "after": "新B",
            },
        ],
    }
    event = FakeEvent("a:GroupMessage:1")

    assert "新B" in await plugin._tool_read_document(event, "#2", "after")
    assert "新A" in await plugin._tool_read_document(event, "#1", "after")
    # 多文档省略 doc 必须指定，返回可用文档列表而非默认第一篇
    result = await plugin._tool_read_document(event, scope="after")
    assert "必须指定" in result
    assert "#1 文档A" in result
    assert "#2 文档B" in result
    assert "新A" not in result


async def test_tool_read_document_omits_doc_for_single_doc(monkeypatch, tmp_path):
    """单文档监控项省略 doc 时默认取唯一一篇，无需显式指定。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    event = set_run(plugin, before="旧内容", after="新内容")

    assert "新内容" in await plugin._tool_read_document(event, scope="after")
    assert "必须指定" not in await plugin._tool_read_document(event, scope="after")


async def test_tool_search_document_selects_doc_by_index(monkeypatch, tmp_path):
    """search 工具同样按 #序号 选中指定文档，避免只在第一篇里找。"""
    plugin, _ = make_plugin(monkeypatch, tmp_path, {})
    plugin._runs["a:GroupMessage:1"] = {
        "target": MULTI_TARGET,
        "docs": [
            {
                "url": "https://example.com/a",
                "name": "文档A",
                "before": "填充",
                "after": "关键词甲",
            },
            {
                "url": "https://example.com/b",
                "name": "文档B",
                "before": "填充",
                "after": "关键词乙",
            },
        ],
    }
    event = FakeEvent("a:GroupMessage:1")

    assert "关键词乙" in await plugin._tool_search_document(
        event, "关键词乙", "#2", "after"
    )
    assert "未找到" in await plugin._tool_search_document(
        event, "关键词乙", "#1", "after"
    )


async def test_api_save_targets_requires_documents(monkeypatch, tmp_path):
    """缺少 documents 的监控项必须被拒（要求迁移到多文档结构）。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {})

    resp = await _call_api(plugin, {"targets": [TARGET]})
    assert resp.status_code == 400
    # 没有落盘
    assert ctx.kv.puts == []

    resp = await _call_api(
        plugin,
        {"targets": [{**TARGET, "documents": [{"url": "https://example.com/doc"}]}]},
    )
    assert json.loads(resp.body.decode())["saved"] is True


async def test_api_save_targets_rejects_doc_without_url(monkeypatch, tmp_path):
    """documents 里缺 url 的项是非法配置，整条监控项被拒。"""
    plugin, ctx = make_plugin(monkeypatch, tmp_path, {})
    resp = await _call_api(
        plugin,
        {"targets": [{"id": "x", "documents": [{"selector": ".markdown-body"}]}]},
    )
    assert resp.status_code == 400
    assert ctx.kv.puts == []


async def test_history_rounds_positive_appends_session_history_to_prompt(
    monkeypatch, tmp_path
):
    """history_rounds 为正数时，总结 prompt 末尾携带该会话历史。"""
    umo = "fake:GroupMessage:1"
    history = [
        {"role": "user", "content": "用户：之前在讨论部署流程"},
        {"role": "assistant", "content": "助手：建议用蓝绿发布"},
    ]
    manager = FakeConversationManager(
        current={umo: "cid-1"}, histories={"cid-1": history}
    )
    plugin, ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"history_rounds": 2, "targets": [TARGET]},
        conversations=[FakeConversation(umo, "fake")],
        conversation_manager=manager,
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()
    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    assert len(ctx.provider.calls) == 1
    prompt = ctx.provider.calls[0]
    # 历史块出现在 prompt 末尾：稳定前缀（文档 diff + 任务指令）在前，利于提示缓存
    assert "以下是该推送会话最近的对话历史" in prompt
    assert "之前在讨论部署流程" in prompt
    assert prompt.strip().endswith("助手：建议用蓝绿发布")
    # 历史位于任务指令（稳定前缀）之后，而不是插在中间破坏前缀
    assert prompt.rfind("之前在讨论部署流程") > prompt.find("对调用方的影响")


async def test_history_rounds_zero_disables_history(monkeypatch, tmp_path):
    """history_rounds=0（关闭）时，prompt 不含会话历史块（行为不变）。"""
    umo = "fake:GroupMessage:1"
    history = [{"role": "user", "content": "用户：之前在讨论部署流程"}]
    manager = FakeConversationManager(
        current={umo: "cid-1"}, histories={"cid-1": history}
    )
    plugin, ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"history_rounds": 0, "targets": [TARGET]},
        conversations=[FakeConversation(umo, "fake")],
        conversation_manager=manager,
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()
    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    assert len(ctx.provider.calls) == 1
    assert "以下是该推送会话最近的对话历史" not in ctx.provider.calls[0]


async def test_history_rounds_positive_summarizes_each_session_separately(
    monkeypatch, tmp_path
):
    """同人格的多个会话，history_rounds 为正数时各自单独总结（不再共用一份）。"""
    target = {**TARGET, "sessions": ["a:GroupMessage:1", "b:GroupMessage:2"]}
    history_a = [{"role": "user", "content": "用户A：之前在讨论 X"}]
    history_b = [{"role": "user", "content": "用户B：之前在讨论 Y"}]
    manager = FakeConversationManager(
        current={"a:GroupMessage:1": "cid-a", "b:GroupMessage:2": "cid-b"},
        histories={"cid-a": history_a, "cid-b": history_b},
    )
    persona = FakePersonaManager(
        {"a:GroupMessage:1": "人格甲", "b:GroupMessage:2": "人格甲"}
    )
    plugin, ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"history_rounds": 2, "targets": [target]},
        conversations=[
            FakeConversation("a:GroupMessage:1", "a"),
            FakeConversation("b:GroupMessage:2", "b"),
        ],
        persona_manager=persona,
        conversation_manager=manager,
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()
    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    # 两会话同人格：不开历史会共用 1 份，开了历史各自 1 份 → 2 次 LLM 调用
    assert len(ctx.provider.calls) == 2
    # 两份总结各自只带自己会话的历史，互不串味（顺序与 sessions 一致）
    assert "之前在讨论 X" in ctx.provider.calls[0]
    assert "之前在讨论 Y" in ctx.provider.calls[1]
    assert "之前在讨论 Y" not in ctx.provider.calls[0]
    assert "之前在讨论 X" not in ctx.provider.calls[1]
    assert sorted(session for session, _ in ctx.sent) == [
        "a:GroupMessage:1",
        "b:GroupMessage:2",
    ]


async def test_history_rounds_positive_empty_when_no_conversation(
    monkeypatch, tmp_path
):
    """会话无对话历史时，history_rounds 为正数也不报错，照常生成不含历史的总结。"""
    umo = "fake:GroupMessage:1"
    # 管理器在位但查不到该会话的当前对话 → 历史为空串
    manager = FakeConversationManager(current={}, histories={})
    plugin, ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"history_rounds": 2, "targets": [TARGET]},
        conversations=[FakeConversation(umo, "fake")],
        conversation_manager=manager,
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()
    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    assert len(ctx.provider.calls) == 1
    assert "以下是该推送会话最近的对话历史" not in ctx.provider.calls[0]


async def test_history_rounds_negative_unlimited_appends_all_history(
    monkeypatch, tmp_path
):
    """history_rounds=-1 不限制轮数，取全部对话历史（不被按轮数截断）。"""
    umo = "fake:GroupMessage:1"
    # 6 轮（12 条），远超任何合理 N；-1 应全取而非按 2N 截断
    history = []
    for i in range(6):
        history.append({"role": "user", "content": f"用户：第{i}轮问题"})
        history.append({"role": "assistant", "content": f"助手：第{i}轮回答"})
    manager = FakeConversationManager(
        current={umo: "cid-1"}, histories={"cid-1": history}
    )
    plugin, ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"history_rounds": -1, "targets": [TARGET]},
        conversations=[FakeConversation(umo, "fake")],
        conversation_manager=manager,
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()
    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    assert len(ctx.provider.calls) == 1
    prompt = ctx.provider.calls[0]
    assert "以下是该推送会话最近的对话历史" in prompt
    # 首轮与末轮都在，说明没有被按轮数截断
    assert "第0轮问题" in prompt
    assert "第5轮回答" in prompt


async def test_use_session_persona_off_blocks_injection_when_history_on(
    monkeypatch, tmp_path
):
    """use_session_persona=false + history_rounds>0：历史照常、但人格不被注入。

    会话本身配有人格（FakePersonaManager 能解析出「人格甲」），开关关闭时即便
    走了逐会话历史分支，也不应把会话人格注入 system prompt——与开关语义、文档
    一致：关闭后所有会话共用一份不含人格的总结。
    """
    umo = "fake:GroupMessage:1"
    history = [{"role": "user", "content": "用户：之前在讨论部署流程"}]
    manager = FakeConversationManager(
        current={umo: "cid-1"}, histories={"cid-1": history}
    )
    persona = FakePersonaManager({umo: "人格甲"})
    plugin, ctx = make_plugin(
        monkeypatch,
        tmp_path,
        {"use_session_persona": False, "history_rounds": 2, "targets": [TARGET]},
        conversations=[FakeConversation(umo, "fake")],
        persona_manager=persona,
        conversation_manager=manager,
    )
    patch_fetch(monkeypatch, plugin, HTML)
    await plugin._check_all()
    patch_fetch(monkeypatch, plugin, HTML.replace("非流式", "流式"))
    await plugin._check_all()

    # 逐会话历史分支被触发：1 次 LLM 调用，且 prompt 末尾带该会话历史
    assert len(ctx.provider.calls) == 1
    assert "以下是该推送会话最近的对话历史" in ctx.provider.calls[0]
    # 但人格未被注入：system prompt 回落到无人格的 ROLE_PROMPT
    assert ctx.provider.system_prompts[0] == ROLE_PROMPT
    assert "人格甲" not in ctx.provider.system_prompts[0]
