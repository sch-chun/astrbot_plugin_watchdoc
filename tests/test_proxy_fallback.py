"""代理回退逻辑的单元测试（不依赖真实网络）。

通过替换 ``fetch._fetch_via`` 模拟代理 / 直连的成功与失败，覆盖：
- 未配置代理时走直连（沿用系统环境变量代理）；
- 配置代理且成功时只走代理、不触发直连；
- 代理连接层失败（连接错误 / 超时）回退直连（直连成功 / 直连也失败）；
- 代理返回 HTTP 错误（4xx/5xx）不回退，直接抛出；
- 代理地址漏写 scheme（如 127.0.0.1:7890）自动补全 http:// 后走代理；
- 代理地址非法（InvalidURL）兜底回退直连。
"""

import aiohttp
import pytest
from astrbot_plugin_watchdoc.src import fetch


def _recorder(calls, on_proxy, on_direct):
    """生成一个 ``_fetch_via`` 替身，按是否带代理决定行为并记录调用。"""

    async def _fake(url, proxy, trust_env):
        calls.append((url, proxy, trust_env))
        if proxy:
            return await on_proxy()
        return await on_direct()

    return _fake


async def _ok():
    return "<html>ok</html>"


async def _conn_err():
    raise aiohttp.ClientConnectionError("proxy down")


async def _timeout_err():
    raise TimeoutError()


async def _http_err():
    raise aiohttp.ClientResponseError(request_info=None, history=None, status=502)


async def test_no_proxy_calls_direct(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fetch,
        "_fetch_via",
        _recorder(calls, _ok, _ok),
    )
    out = await fetch.fetch_html("https://example.com")
    assert out == "<html>ok</html>"
    assert len(calls) == 1
    assert calls[0][1] is None  # 无代理
    assert calls[0][2] is True  # 无代理：trust_env 沿用系统代理


async def test_proxy_ok_no_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fetch,
        "_fetch_via",
        _recorder(calls, _ok, _ok),
    )
    out = await fetch.fetch_html("https://example.com", proxy="http://127.0.0.1:7890")
    assert out == "<html>ok</html>"
    assert len(calls) == 1  # 代理成功，不回退直连
    assert calls[0][1] == "http://127.0.0.1:7890"


async def test_proxy_conn_error_falls_back_to_direct(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fetch,
        "_fetch_via",
        _recorder(calls, _conn_err, _ok),
    )
    out = await fetch.fetch_html("https://example.com", proxy="http://127.0.0.1:7890")
    assert out == "<html>ok</html>"
    assert len(calls) == 2
    assert calls[0][1] == "http://127.0.0.1:7890"  # 先走代理
    assert calls[1][1] is None  # 回退直连
    assert calls[1][2] is False  # 回退：真正直连，绕过任何代理


async def test_proxy_timeout_falls_back_to_direct(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fetch,
        "_fetch_via",
        _recorder(calls, _timeout_err, _ok),
    )
    out = await fetch.fetch_html("https://example.com", proxy="http://127.0.0.1:7890")
    assert out == "<html>ok</html>"
    assert len(calls) == 2
    assert calls[1][1] is None


async def test_proxy_http_error_no_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fetch,
        "_fetch_via",
        _recorder(calls, _http_err, _http_err),
    )
    with pytest.raises(aiohttp.ClientResponseError):
        await fetch.fetch_html("https://example.com", proxy="http://127.0.0.1:7890")
    assert len(calls) == 1  # HTTP 错误不回退


async def test_proxy_fails_and_direct_also_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fetch,
        "_fetch_via",
        _recorder(calls, _conn_err, _conn_err),
    )
    with pytest.raises(aiohttp.ClientConnectionError):
        await fetch.fetch_html("https://example.com", proxy="http://127.0.0.1:7890")
    assert len(calls) == 2  # 代理失败后再试直连，直连也失败则抛出


async def test_proxy_missing_scheme_is_completed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fetch,
        "_fetch_via",
        _recorder(calls, _ok, _ok),
    )
    out = await fetch.fetch_html("https://example.com", proxy="127.0.0.1:7890")
    assert out == "<html>ok</html>"
    assert calls[0][1] == "http://127.0.0.1:7890"  # 漏写 scheme 自动补全
    assert calls[0][2] is True  # 仍走代理（trust_env 沿用系统代理）


async def test_proxy_invalid_url_falls_back_to_direct(monkeypatch):
    calls = []

    async def _fake(url, proxy, trust_env):
        calls.append((url, proxy, trust_env))
        if proxy:
            raise aiohttp.InvalidURL("bad proxy")
        return "<html>via-direct</html>"

    monkeypatch.setattr(fetch, "_fetch_via", _fake)
    out = await fetch.fetch_html("https://example.com", proxy="http://!!!")
    assert out == "<html>via-direct</html>"
    assert len(calls) == 2
    assert calls[1][1] is None  # InvalidURL 兜底回退直连
