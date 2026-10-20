"""LivingMemory 桥接模块的单元测试（P0 / P1-1 / P1-3 / P2）。

全部用例不依赖 LivingMemory 实际安装：引擎与作用域 / 白名单判定所需配置均以
dict 桩注入，人格解析经 monkeypatch 控制返回值，重点覆盖审查清单里的闸门与边界。
"""

from types import SimpleNamespace

from astrbot_plugin_watchdoc.src import livingmemory_bridge as bridge
from astrbot_plugin_watchdoc.src.livingmemory_bridge import (
    GLOBAL_MEMORY_SCOPE,
    LivingMemoryBridge,
    _format_results,
    _resolve_event_identity,
    is_event_memory_allowed,
    resolve_memory_scope,
)

# ---------------------------------------------------------------------------
# 桩对象
# ---------------------------------------------------------------------------


class FakeInitializer:
    def __init__(self, engine):
        self.memory_engine = engine

    async def ensure_initialized(self):
        if self.memory_engine is None:
            self.memory_engine = object()


class FakeLM:
    def __init__(self, config, engine):
        self.config_manager = config
        self.initializer = FakeInitializer(engine)


class FakeStar:
    def __init__(self, name, display_name, activated=True, star_cls=None):
        self.name = name
        self.display_name = display_name
        self.activated = activated
        self.star_cls = star_cls


class FakeContext:
    def __init__(self, stars):
        self._stars = stars

    def get_registered_star(self, name):
        for s in self._stars:
            if s.name == name:
                return s
        return None

    def get_all_stars(self):
        return self._stars


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.results = [SimpleNamespace(content="记忆A")]

    async def search_memories(self, query, k, session_id, persona_id):
        self.calls.append((query, k, session_id, persona_id))
        return self.results


def make_bridge(config, engine):
    lm = FakeLM(config, engine)
    star = FakeStar("astrbot_plugin_livingmemory", "LivingMemory", star_cls=lm)
    ctx = FakeContext([star])
    return LivingMemoryBridge(ctx), engine


async def _persona_none(*_a, **_k):
    return None


async def _persona_p7(*_a, **_k):
    return "p-7"


# ---------------------------------------------------------------------------
# P0：插件名匹配
# ---------------------------------------------------------------------------


async def test_get_engine_finds_by_plugin_name():
    engine = object()
    lm = FakeLM({}, engine)
    star = FakeStar("astrbot_plugin_livingmemory", "LivingMemory", star_cls=lm)
    b = LivingMemoryBridge(FakeContext([star]))
    got_lm, got_engine = await b._get_engine()
    assert got_engine is engine
    assert got_lm is lm


async def test_get_engine_falls_back_to_display_name():
    # 名字不正确，但 display_name 为 livingmemory —— 原实现因用错名字 100% 静默失效
    engine = object()
    lm = FakeLM({}, engine)
    star = FakeStar("wrong-internal-name", "LivingMemory", star_cls=lm)
    b = LivingMemoryBridge(FakeContext([star]))
    got_lm, got_engine = await b._get_engine()
    assert got_engine is engine


async def test_get_engine_returns_none_when_absent():
    b = LivingMemoryBridge(FakeContext([]))
    assert await b._get_engine() == (None, None)


async def test_get_engine_skips_unactivated():
    engine = object()
    lm = FakeLM({}, engine)
    star = FakeStar(
        "astrbot_plugin_livingmemory", "LivingMemory", activated=False, star_cls=lm
    )
    b = LivingMemoryBridge(FakeContext([star]))
    assert await b._get_engine() == (None, None)


async def test_get_engine_skips_missing_star_cls():
    star = FakeStar(
        "astrbot_plugin_livingmemory", "LivingMemory", activated=True, star_cls=None
    )
    b = LivingMemoryBridge(FakeContext([star]))
    assert await b._get_engine() == (None, None)


async def test_get_engine_waits_for_init():
    # engine 初始为 None，ensure_initialized 后补上
    lm = FakeLM({}, None)
    star = FakeStar("astrbot_plugin_livingmemory", "LivingMemory", star_cls=lm)
    b = LivingMemoryBridge(FakeContext([star]))
    got_lm, got_engine = await b._get_engine()
    assert got_lm is lm
    assert got_engine is not None


# ---------------------------------------------------------------------------
# P2：作用域解析
# ---------------------------------------------------------------------------


def test_scope_session():
    cfg = {"filtering_settings": {"memory_scope_mode": "session"}}
    assert resolve_memory_scope(cfg, "qq:friend:123") == "qq:friend:123"


def test_scope_global():
    cfg = {"filtering_settings": {"memory_scope_mode": "global"}}
    assert resolve_memory_scope(cfg, "qq:friend:123") == GLOBAL_MEMORY_SCOPE


def test_scope_legacy_session_filtering():
    cfg = {
        "filtering_settings": {
            "memory_scope_mode": "legacy",
            "use_session_filtering": True,
        }
    }
    assert resolve_memory_scope(cfg, "qq:friend:123") == "qq:friend:123"


def test_scope_legacy_no_session_filtering_no_isolated():
    cfg = {
        "filtering_settings": {
            "memory_scope_mode": "legacy",
            "use_session_filtering": False,
        }
    }
    assert resolve_memory_scope(cfg, "qq:friend:123") is None


def test_scope_legacy_no_session_filtering_with_isolated():
    cfg = {
        "filtering_settings": {
            "memory_scope_mode": "legacy",
            "use_session_filtering": False,
            "isolated_sessions": "qq:friend:9",
        }
    }
    assert resolve_memory_scope(cfg, "qq:friend:123") == GLOBAL_MEMORY_SCOPE


def test_scope_isolated_match():
    cfg = {"filtering_settings": {"isolated_sessions": "qq:friend:123"}}
    assert resolve_memory_scope(cfg, "qq:friend:123") == "qq:friend:123"


def test_scope_user_mode_with_alias():
    cfg = {
        "filtering_settings": {"memory_scope_mode": "user"},
        "access_control": {"identity_aliases": "qq:123=canonical"},
    }
    assert (
        resolve_memory_scope(cfg, "qq:friend:123") == "livingmemory:user:qq:canonical"
    )


def test_scope_user_mode_no_alias():
    cfg = {"filtering_settings": {"memory_scope_mode": "user"}}
    # 私聊 sender_id=123，identity 回退到 sender_id
    assert resolve_memory_scope(cfg, "qq:friend:123") == "livingmemory:user:qq:123"


# ---------------------------------------------------------------------------
# P2：白名单候选匹配
# ---------------------------------------------------------------------------


def test_whitelist_disabled():
    cfg = {"access_control": {"whitelist_enabled": False}}
    assert is_event_memory_allowed(cfg, "qq:friend:123") is True


def test_whitelist_enabled_empty():
    cfg = {"access_control": {"whitelist_enabled": True, "allowed_ids": ""}}
    assert is_event_memory_allowed(cfg, "qq:friend:123") is False


def test_whitelist_by_umo():
    cfg = {
        "access_control": {"whitelist_enabled": True, "allowed_ids": "qq:friend:123"}
    }
    assert is_event_memory_allowed(cfg, "qq:friend:123") is True
    assert is_event_memory_allowed(cfg, "qq:friend:999") is False


def test_whitelist_by_sender_id():
    cfg = {"access_control": {"whitelist_enabled": True, "allowed_ids": "123"}}
    assert is_event_memory_allowed(cfg, "qq:friend:123") is True


def test_whitelist_by_platform_sender():
    cfg = {"access_control": {"whitelist_enabled": True, "allowed_ids": "qq:123"}}
    assert is_event_memory_allowed(cfg, "qq:friend:123") is True


def test_whitelist_group_by_group_id():
    # 群聊时 get_group_id 返回群 ID，白名单按群 ID 配置应命中
    cfg = {"access_control": {"whitelist_enabled": True, "allowed_ids": "gid"}}
    assert is_event_memory_allowed(cfg, "qq:group:gid") is True


def test_whitelist_group_by_platform_group():
    # 群聊时 get_group_id 返回群 ID，完整 UMO 也是候选，按完整 UMO 配置应命中
    cfg = {"access_control": {"whitelist_enabled": True, "allowed_ids": "qq:group:gid"}}
    assert is_event_memory_allowed(cfg, "qq:group:gid") is True


def test_whitelist_group_platform_group_id_not_a_candidate():
    # 候选集含 group_id 与 umo，但不含 platform:group_id；
    # 因此按「平台:群ID」配置不会命中群聊，需直接填群 ID 或完整 UMO
    cfg = {"access_control": {"whitelist_enabled": True, "allowed_ids": "qq:gid"}}
    assert is_event_memory_allowed(cfg, "qq:group:gid") is False


def test_whitelist_group_by_alias():
    cfg = {
        "access_control": {
            "whitelist_enabled": True,
            "allowed_ids": "canonical",
            "identity_aliases": "qq:123=canonical",
        }
    }
    assert is_event_memory_allowed(cfg, "qq:friend:123") is True


# ---------------------------------------------------------------------------
# P2：结果格式化兼容 dict / 对象
# ---------------------------------------------------------------------------


def test_format_results_object():
    results = [SimpleNamespace(content="记住 A"), SimpleNamespace(content="记住 B")]
    out = _format_results(results)
    assert "记住 A" in out and "记住 B" in out
    assert out.startswith("【LivingMemory 长期记忆参考】")


def test_format_results_dict():
    results = [{"content": "dict A"}, {"content": "dict B"}]
    out = _format_results(results)
    assert "dict A" in out and "dict B" in out


def test_format_results_empty():
    assert _format_results([]) == ""
    assert _format_results([SimpleNamespace(content="")]) == ""


# ---------------------------------------------------------------------------
# P2：身份归并
# ---------------------------------------------------------------------------


def test_identity_alias():
    ac = {"identity_aliases": "qq:123=canonical"}
    assert _resolve_event_identity(ac, "qq:friend:123") == "canonical"


def test_identity_fallback_sender():
    assert _resolve_event_identity({}, "qq:friend:123") == "123"


def test_identity_group_fallback_umo():
    assert _resolve_event_identity({}, "qq:group:gid") == "qq:group:gid"


# ---------------------------------------------------------------------------
# P1-3：人格解析失败 fail closed + 正常召回路径
# ---------------------------------------------------------------------------


async def test_recall_happy_path(monkeypatch):
    engine = FakeEngine()
    cfg = {
        "filtering_settings": {
            "memory_scope_mode": "global",
            "use_persona_filtering": False,
        },
        "access_control": {},
    }
    b, engine = make_bridge(cfg, engine)
    monkeypatch.setattr(bridge, "get_lm_persona_id", _persona_none)
    out = await b.recall_into_history(
        target={"name": "示例文档"},
        changed_docs=[{"name": "文档A", "url": "https://x/a"}],
        umo="qq:friend:123",
        use_persona=False,
        history_on=False,
        group_umos=["qq:friend:123"],
        top_k=5,
    )
    assert "记忆A" in out
    assert engine.calls[0][2] == GLOBAL_MEMORY_SCOPE
    assert engine.calls[0][3] is None


async def test_recall_caches_within_cycle(monkeypatch):
    engine = FakeEngine()
    cfg = {
        "filtering_settings": {
            "memory_scope_mode": "global",
            "use_persona_filtering": False,
        },
        "access_control": {},
    }
    b, engine = make_bridge(cfg, engine)
    monkeypatch.setattr(bridge, "get_lm_persona_id", _persona_none)
    kwargs = dict(
        target={"name": "X"},
        changed_docs=[],
        umo="qq:friend:123",
        use_persona=False,
        history_on=False,
        group_umos=["qq:friend:123"],
        top_k=5,
    )
    await b.recall_into_history(**kwargs)
    await b.recall_into_history(**kwargs)
    assert len(engine.calls) == 1  # 第二次命中周期缓存


async def test_recall_fail_closed_when_persona_unresolvable(monkeypatch):
    # 要求按人格过滤却解析不到人格 → 跳过召回，而不是以「不过滤」混进其他人格记忆
    engine = FakeEngine()
    cfg = {
        "filtering_settings": {
            "memory_scope_mode": "global",
            "use_persona_filtering": True,
        },
        "access_control": {},
    }
    b, engine = make_bridge(cfg, engine)
    monkeypatch.setattr(bridge, "get_lm_persona_id", _persona_none)
    out = await b.recall_into_history(
        target={"name": "X"},
        changed_docs=[],
        umo="qq:friend:123",
        use_persona=True,
        history_on=False,
        group_umos=["qq:friend:123"],
        top_k=5,
    )
    assert out == ""
    assert engine.calls == []


async def test_recall_persona_off_proceeds(monkeypatch):
    # 关闭人格开关时即使 LM 开着人格过滤也不应解析人格，正常召回
    engine = FakeEngine()
    cfg = {
        "filtering_settings": {
            "memory_scope_mode": "global",
            "use_persona_filtering": True,
        },
        "access_control": {},
    }
    b, engine = make_bridge(cfg, engine)
    monkeypatch.setattr(bridge, "get_lm_persona_id", _persona_none)
    out = await b.recall_into_history(
        target={"name": "X"},
        changed_docs=[],
        umo="qq:friend:123",
        use_persona=False,
        history_on=False,
        group_umos=["qq:friend:123"],
        top_k=5,
    )
    assert "记忆A" in out


async def test_recall_top_k_zero_is_noop():
    engine = FakeEngine()
    cfg = {
        "filtering_settings": {
            "memory_scope_mode": "global",
            "use_persona_filtering": False,
        },
        "access_control": {},
    }
    b, engine = make_bridge(cfg, engine)
    out = await b.recall_into_history(
        target={"name": "X"},
        changed_docs=[],
        umo="qq:friend:123",
        use_persona=False,
        history_on=False,
        group_umos=["qq:friend:123"],
        top_k=0,
    )
    assert out == ""


async def test_recall_history_on_session_scope_with_persona(monkeypatch):
    engine = FakeEngine()
    cfg = {
        "filtering_settings": {
            "memory_scope_mode": "session",
            "use_persona_filtering": True,
        },
        "access_control": {},
    }
    b, engine = make_bridge(cfg, engine)
    monkeypatch.setattr(bridge, "get_lm_persona_id", _persona_p7)
    out = await b.recall_into_history(
        target={"name": "X"},
        changed_docs=[],
        umo="qq:friend:123",
        use_persona=True,
        history_on=True,
        group_umos=["qq:friend:123"],
        top_k=5,
    )
    assert "记忆A" in out
    assert engine.calls[0][2] == "qq:friend:123"
    assert engine.calls[0][3] == "p-7"


async def test_recall_whitelist_blocks_group(monkeypatch):
    engine = FakeEngine()
    cfg = {
        "filtering_settings": {
            "memory_scope_mode": "global",
            "use_persona_filtering": False,
        },
        "access_control": {
            "whitelist_enabled": True,
            "allowed_ids": "qq:friend:123",
        },
    }
    b, engine = make_bridge(cfg, engine)
    monkeypatch.setattr(bridge, "get_lm_persona_id", _persona_none)
    out = await b.recall_into_history(
        target={"name": "X"},
        changed_docs=[],
        umo="qq:friend:123",
        use_persona=False,
        history_on=False,
        group_umos=["qq:friend:999"],  # 不在白名单内
        top_k=5,
    )
    assert out == ""
    assert engine.calls == []


async def test_recall_whitelist_blocks_single(monkeypatch):
    engine = FakeEngine()
    cfg = {
        "filtering_settings": {
            "memory_scope_mode": "session",
            "use_persona_filtering": False,
        },
        "access_control": {
            "whitelist_enabled": True,
            "allowed_ids": "qq:friend:123",
        },
    }
    b, engine = make_bridge(cfg, engine)
    monkeypatch.setattr(bridge, "get_lm_persona_id", _persona_none)
    out = await b.recall_into_history(
        target={"name": "X"},
        changed_docs=[],
        umo="qq:friend:999",
        use_persona=False,
        history_on=True,
        group_umos=["qq:friend:999"],
        top_k=5,
    )
    assert out == ""


async def test_recall_history_off_requires_global(monkeypatch):
    # 对话历史关却非 global 作用域 → 告警并禁用本组记忆
    engine = FakeEngine()
    cfg = {
        "filtering_settings": {
            "memory_scope_mode": "session",
            "use_persona_filtering": False,
        },
        "access_control": {},
    }
    b, engine = make_bridge(cfg, engine)
    monkeypatch.setattr(bridge, "get_lm_persona_id", _persona_none)
    out = await b.recall_into_history(
        target={"name": "X"},
        changed_docs=[],
        umo="qq:friend:123",
        use_persona=False,
        history_on=False,
        group_umos=["qq:friend:123"],
        top_k=5,
    )
    assert out == ""
