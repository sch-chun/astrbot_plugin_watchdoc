"""页面抓取、正文提取与预览净化。"""

from __future__ import annotations

import asyncio
import codecs
import io
import re
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup
from markitdown_no_magika import MarkItDown, StreamInfo

from astrbot.api import logger

from .constants import (
    DEFAULT_UA,
    FETCH_CHUNK_BYTES,
    MAX_HTML_BYTES,
    MIN_TEXT_LEN,
    SELECTOR_CANDIDATES,
)


class PageTooLargeError(Exception):
    """响应体超过上限。"""


async def fetch_html(url: str) -> str:
    """抓取页面 HTML。

    Args:
        url: 目标地址。

    Returns:
        页面 HTML 文本。

    Raises:
        aiohttp.ClientError: 网络请求失败时抛出。
        PageTooLargeError: 响应体超过上限时抛出。
    """
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
        async with session.get(url, headers={"User-Agent": DEFAULT_UA}) as resp:
            resp.raise_for_status()
            return await read_body(resp, MAX_HTML_BYTES)


async def read_body(resp, limit: int = MAX_HTML_BYTES) -> str:
    """按上限读取响应体。

    Content-Length 只能信一半：可能是 None（chunked），也可能与实际不符，
    所以声明值与实际读取量两处都要卡上限。

    Args:
        resp: aiohttp 响应对象，需具备 content_length 与 content。
        limit: 允许的字节数上限。

    Returns:
        按响应声明字符集解码后的文本，声明缺失时按 UTF-8 解码。

    Raises:
        PageTooLargeError: 声明值或实际读取量超过上限时抛出。
    """
    declared = resp.content_length
    if declared is not None and declared > limit:
        raise PageTooLargeError(f"响应过大：声明 {declared:,} 字节，上限 {limit:,}")

    chunks = []
    size = 0
    async for chunk in resp.content.iter_chunked(FETCH_CHUNK_BYTES):
        size += len(chunk)
        if size > limit:
            raise PageTooLargeError(f"响应过大：超过上限 {limit:,} 字节")
        chunks.append(chunk)

    return b"".join(chunks).decode(_resolve_encoding(resp.charset), errors="replace")


def _resolve_encoding(declared: str | None) -> str:
    """把响应声明的字符集规范化成 Python 能用的编码名。

    与 aiohttp 的 `get_encoding()` 对齐：声明缺失或无法识别时回落 UTF-8。
    （aiohttp 默认不再做 chardet 探测，`ClientResponse._resolve_charset` 是个
    直接返回 utf-8 的桩，除非显式传 `fallback_charset_resolver`。）

    Args:
        declared: Content-Type 里的 charset 值，可能为 None。

    Returns:
        可用于 bytes.decode 的编码名。
    """
    if not declared:
        return "utf-8"
    try:
        return codecs.lookup(declared).name
    except (LookupError, ValueError):
        return "utf-8"


def build_soup(html: str) -> BeautifulSoup:
    """解析 HTML 并去掉不参与正文的标签。

    Args:
        html: 原始 HTML。

    Returns:
        清洗后的 BeautifulSoup 对象。
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return soup


def to_markdown(html: str, selector: str, tid: str) -> str:
    """把 HTML 裁剪成正文并转成 Markdown。

    Args:
        html: 原始 HTML。
        selector: 正文容器的 CSS 选择器，为空则使用整页。
        tid: 监控项 ID，仅用于日志。

    Returns:
        Markdown 文本。
    """
    soup = build_soup(html)

    if selector:
        nodes = soup.select(selector)
        if nodes:
            soup = BeautifulSoup("".join(str(n) for n in nodes), "lxml")
        else:
            logger.warning(f"[watchdoc] {tid} 选择器 {selector} 未命中，退化为整页比对")

    return (
        MarkItDown(enable_plugins=False)
        .convert(
            io.BytesIO(str(soup).encode("utf-8")),
            stream_info=StreamInfo(extension=".html"),
        )
        .markdown
    )


def looks_stable(selector: str) -> bool:
    """判断选择器是否语义化，即改版后是否容易失效。

    构建产物型的类名（含 hash、位置编号、多层 class 链）会随前端构建变化，
    不适合作为长期监控的锚点。

    Args:
        selector: 待判断的选择器。

    Returns:
        语义化且稳定时返回 True。
    """
    if "nth-child" in selector or "nth-of-type" in selector:
        return False
    if re.search(r"[0-9a-f]{6,}", selector):
        return False
    return selector.count(".") <= 2


async def probe_url(url: str, fetcher: Callable[[str], Awaitable[str]]) -> str:
    """探测目标页面的正文容器，给出可直接抄用的选择器建议。

    Args:
        url: 目标地址。
        fetcher: 抓取函数，便于测试替换。

    Returns:
        面向用户的候选清单文本。
    """
    html = await fetcher(url)
    soup = await asyncio.to_thread(build_soup, html)

    rows = []
    for selector in SELECTOR_CANDIDATES:
        nodes = soup.select(selector)
        if not nodes:
            continue
        size = len(" ".join(n.get_text(" ", strip=True) for n in nodes))
        if size >= MIN_TEXT_LEN:
            rows.append((selector, size))

    # 兜底：文本量最大的 div，用于候选都没命中的站点。
    # 只称最外层 div——父节点的文本必然包含子节点，因此文本量最大的一定在
    # 顶层；逐个 div 取文本会让嵌套结构被反复遍历，大页面上是平方级开销。
    best_div, best_size = None, 0
    for div in soup.find_all("div"):
        if div.find_parent("div") is not None:
            continue
        size = len(div.get_text(" ", strip=True))
        if size > best_size:
            best_div, best_size = div, size
    if best_div is not None and best_size >= MIN_TEXT_LEN:
        cls = " ".join(best_div.get("class") or [])
        # 没有 class 的 div 写不成可用的选择器，推荐给用户只会让人白试一次
        if cls:
            rows.append((f"div.{cls.replace(' ', '.')}", best_size))

    if not rows:
        return (
            f"没找到可用的正文容器（原始 HTML {len(html):,} 字符）。\n"
            "这个页面可能需要 JS 渲染，纯抓取拿不到正文；请人工确认或换用静态源。"
        )

    # 稳定的排前面，同稳定性下文本量大的优先
    rows.sort(key=lambda item: (-int(looks_stable(item[0])), -item[1]))

    lines = ["候选选择器（稳定的排前面，数字是抓到的正文字符数）："]
    for selector, size in rows[:6]:
        flag = "稳定" if looks_stable(selector) else "易失效"
        lines.append(f"  [{flag}] {size:,}  {selector}")
    lines.append(f"\n建议填：{rows[0][0]}")
    lines.append("填进该监控项的 selector 后，用 /watchdoc reset <id> 重建基线。")
    return "\n".join(lines)


def sanitize_for_preview(html: str, base_url: str) -> str:
    """清洗 HTML，使其能在 Page 的 Shadow DOM 中安全渲染。

    去掉脚本与事件属性。相对资源路径逐个改写成绝对地址——Page 的 CSP 是
    base-uri 'self'，注入 <base> 标签会被拦掉，只能逐个改写。

    Args:
        html: 原始 HTML。
        base_url: 页面地址，用于补全相对路径。

    Returns:
        可安全注入的 HTML。
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "noscript", "template", "iframe"]):
        tag.decompose()

    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.startswith("on"):
                del tag[attr]
        # 预览区不参与交互，去掉跳转避免误触导航
        if tag.name == "a":
            tag.attrs.pop("href", None)
        if tag.name == "img":
            # 懒加载图片在预览区不会触发加载，还会让控制台一直冒提示；
            # 常见写法是把真实地址放在 data-src 上，顺手提升为 src
            tag.attrs.pop("loading", None)
            if not tag.get("src") and tag.get("data-src"):
                tag["src"] = tag["data-src"]
            if tag.get("src"):
                tag["src"] = urljoin(base_url, tag["src"])
        if tag.name == "link" and tag.get("href"):
            tag["href"] = urljoin(base_url, tag["href"])
    return str(soup)
