"""页面抓取、正文提取与预览净化。"""

from __future__ import annotations

import asyncio
import codecs
import io
import json
import re
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

import aiohttp
from astrbot.api import logger
from bs4 import BeautifulSoup
from markitdown_no_magika import MarkItDown, StreamInfo

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
    async with (
        aiohttp.ClientSession(trust_env=True, timeout=timeout) as session,
        session.get(url, headers={"User-Agent": DEFAULT_UA}) as resp,
    ):
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


def extract_title(soup: BeautifulSoup) -> str:
    """从页面取出可读标题，供文档列表展示。

    回退链：<title> → og:title → 首 <h1>，都没有返回空串；
    取到的文本会去除首尾空白，空结果继续向下回退。

    Args:
        soup: 已解析的 BeautifulSoup 对象。

    Returns:
        标题字符串，取不到时为空串。
    """
    if soup.title:
        text = soup.title.get_text(" ", strip=True)
        if text:
            return text
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        text = str(og.get("content")).strip()
        if text:
            return text
    h1 = soup.find("h1")
    if h1:
        text = h1.get_text(" ", strip=True)
        if text:
            return text
    return ""


def is_markdown_url(url: str) -> bool:
    """判断地址是否直接指向 Markdown 源。

    去掉片段与查询串、去掉末尾斜杠后再看路径是否以 ``.md`` 结尾——
    这样 ``https://x/y.md?token=1#z`` 与 ``https://x/y.md/`` 都能识别。

    Args:
        url: 目标地址。

    Returns:
        是 Markdown 源时返回 True。
    """
    if not url:
        return False
    core = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return core.lower().endswith(".md")


def md_variant(url: str) -> str:
    """把地址改写成对应的 ``.md`` 官方源候选地址。

    裸拼 ``<url>.md`` 在带查询串或末尾斜杠时会拼错，这里正确处理二者——前端会
    把结果直接展示给用户复制填写，拼错等于把人往坑里带：
      https://x/y?id=1   -> https://x/y.md?id=1
      https://x/y/       -> https://x/y.md
    已是 ``.md`` 源时原样返回（调用方本就不会对 md 源再探，这里只是防御）。

    Args:
        url: 原始（非 Markdown）地址。

    Returns:
        探测用的 ``.md`` 候选地址。
    """
    if is_markdown_url(url):
        return url
    frag = ""
    if "#" in url:
        url, frag = url.split("#", 1)
        frag = "#" + frag
    query = ""
    if "?" in url:
        url, query = url.split("?", 1)
        query = "?" + query
    return url.rstrip("/") + ".md" + query + frag


def looks_like_markdown(text: str) -> bool:
    """判断抓到的文本是否像 Markdown/纯文本而非 HTML 错误页。

    ``.md`` 源缺失时很多站点会回一个 HTML 404 页，这种不能当作 Markdown 源。
    以 ``<!doctype`` / ``<html`` / ``<?xml`` / ``<head`` / ``<!--`` 开头的
    （忽略前导空白）一律视为 HTML 错误页；另挡一层 JSON 错误响应
    （``{"error": ...}`` / ``{"message": ...}``），否则会被误当成 md 源、前端误提示。

    Args:
        text: 抓取到的原始文本。

    Returns:
        看起来是 Markdown/纯文本时返回 True。
    """
    t = text.lstrip().lower()
    if t.startswith(("<!doctype", "<html", "<?xml", "<head", "<!--")):
        return False
    # JSON 错误响应（如 {"error": "not found"}）不是 Markdown 源
    if t.startswith("{"):
        try:
            obj = json.loads(text.lstrip())
        except ValueError:  # json.JSONDecodeError 是 ValueError 的子类，单写即可
            return True
        if isinstance(obj, dict) and ("error" in obj or "message" in obj):
            return False
    return True


def extract_markdown_title(md_text: str, url: str) -> str:
    """从 Markdown 源提取可读标题，供文档列表 / 通知展示。

    回退链：frontmatter ``title:`` → 首个 ATX 标题（``# 标题``）→ URL 末段
    （去掉 ``.md``），都没有返回空串。

    Args:
        md_text: Markdown 源文本。
        url: 文档地址，仅用于兜底取末段。

    Returns:
        标题字符串，取不到时为空串。
    """
    if md_text:
        stripped = md_text.lstrip("﻿")
        if stripped.startswith("---"):
            lines = stripped.splitlines()
            end = None
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    end = i
                    break
            if end is not None:
                title = _parse_frontmatter_title("\n".join(lines[1:end]))
                if title:
                    return title
        for line in stripped.splitlines():
            m = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line.strip())
            if m:
                return m.group(1).strip()
    return _url_last_segment(url)


def _parse_frontmatter_title(frontmatter: str) -> str:
    """从 YAML frontmatter 文本里取 ``title`` 字段（去掉可选的引号包裹）。"""
    for line in frontmatter.splitlines():
        m = re.match(r"^title\s*:\s*(.+)$", line, re.IGNORECASE)
        if m:
            return m.group(1).strip().strip("\"'")
    return ""


def _url_last_segment(url: str) -> str:
    """取 URL 路径末段（去掉 ``.md``）作为兜底标题。"""
    from urllib.parse import urlparse

    seg = urlparse(url).path.rstrip("/").split("/")[-1]
    if seg.lower().endswith(".md"):
        seg = seg[:-3]
    return seg


async def probe_markdown(
    url: str, fetcher: Callable[[str], Awaitable[str]]
) -> str | None:
    """静默探测目标地址是否有对应的 ``.md`` 官方源。

    仅当 ``url`` 本身不是 ``.md`` 时才探测 ``<url>.md``；探测失败（404 / 网络错 /
    拿到的是 HTML 错误页）一律静默返回 ``None``，绝不影响主抓取流程。

    Args:
        url: 目标地址。
        fetcher: 抓取函数，便于测试替换。

    Returns:
        拿到 Markdown 文本时返回该文本，否则返回 None。
    """
    if is_markdown_url(url):
        return None
    try:
        text = await fetcher(md_variant(url))
    except Exception:  # noqa: BLE001 — 探测失败静默：不向上抛、不记日志刷屏
        return None
    if looks_like_markdown(text):
        return text
    return None


def to_markdown(html: str, selector: str, tid: str, is_markdown: bool = False) -> str:
    """把 HTML 裁剪成正文并转成 Markdown。

    Markdown 源（``url`` 以 ``.md`` 结尾）抓到的已是 Markdown 原文，无需再经
    MarkItDown 转换——否则会把 ``# 标题`` 等当 HTML 误转、还可能丢失原始结构。
    此时 ``selector`` 无意义，直接整篇采用。

    Args:
        html: 原始 HTML，或 Markdown 源的原文（``is_markdown=True`` 时）。
        selector: 正文容器的 CSS 选择器，为空则使用整页。
        tid: 监控项 ID，仅用于日志。
        is_markdown: 输入是否已是 Markdown 原文。

    Returns:
        Markdown 文本。
    """
    if is_markdown:
        # 真碰上带 BOM 的源，不剥干净会让快照首行带 ﻿，diff 一直误报变更
        return html.lstrip("﻿")
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
