"""文本归一化、摘要与 diff 生成。"""

from __future__ import annotations

import difflib
import hashlib
import re

from astrbot.api import logger


def normalize(text: str, patterns: list) -> str:
    """归一化文本以消除无意义差异。

    默认只压缩空白。刻意不做日期归一化——日期可能是有意义的版本标识，
    替换掉会漏掉真正的变更。确有动态噪音时用 patterns 精确剔除。

    Args:
        text: 待归一化文本。
        patterns: 要从文本中删除的正则表达式。

    Returns:
        归一化后的文本。
    """
    text = text.replace("\r\n", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    for pattern in patterns:
        try:
            text = re.sub(pattern, "", text)
        except re.error as exc:
            logger.warning(f"[watchdoc] 忽略规则非法，已跳过: {pattern} ({exc})")
    return text.strip()


def digest(text: str) -> str:
    """计算文本摘要。

    Args:
        text: 输入文本。

    Returns:
        SHA-256 十六进制摘要。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_diff(old: str, new: str) -> str:
    """生成 unified diff。

    行号基于 splitlines()，与查阅工具的取行方式一致，
    因此 diff 里 @@ 标记的行号可以直接拿去读原文。

    Args:
        old: 上次快照。
        new: 本次抓取。

    Returns:
        unified diff 文本。
    """
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile="上次快照",
            tofile="本次抓取",
            lineterm="",
            n=2,
        )
    )


def safe_name(raw: str) -> str:
    """把监控项 ID / URL 转成安全文件名。

    同名文件以文件名本身为键，必须单射。保留**末尾**最多 64 个字符而不是开头：
    同站点 URL 的差异几乎都在路径末尾（如 voice-cloning-uploadcloneaudio 与
    uploadprompt），保留末尾更能区分不同页面、减少撞名；超过 64 时再从前面砍掉
    公共前缀。

    Args:
        raw: 原始 ID 或 URL。

    Returns:
        只含字母数字下划线和短横线、且从末尾截断的文件名。
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    return (cleaned[-64:] if len(cleaned) > 64 else cleaned) or "target"


def build_doc_slug_map(urls: list[str]) -> dict[str, str]:
    """把一组 URL 映射成同一监控项内互不重名的快照文件名。

    同一监控项（doc_dir）内的文档快照以文件名为键，必须单射——否则前 64 字符
    相同的 URL 会共用一个基线槽位，互相覆盖并循环误报「有变化」、白烧 AI 总结
    token（见 v0.1.9 此修复）。

    基线名取 ``safe_name(url)``（已截断到 64）。若多个 URL 落到同一基线名，则给每个
    追加 ``_`` + URL 短哈希去重；未冲突的 URL 保持原基线名，其旧基线文件不受影响
    （无需重建）。

    去重后若仍有重名（几乎不可能，作为防御），抛出 ValueError。

    Args:
        urls: 同一监控项内的文档 URL 列表（空串/纯空白会被忽略，重复会被合并）。

    Returns:
        ``{url: slug}`` 映射；单 URL 场景等价于 ``{url: safe_name(url)}``。
    """
    cleaned = sorted({u for u in urls if u and u.strip()})
    groups: dict[str, list[str]] = {}
    for u in cleaned:
        groups.setdefault(safe_name(u), []).append(u)
    result: dict[str, str] = {}
    for base, members in groups.items():
        if len(members) == 1:
            result[members[0]] = base
        else:
            for u in members:
                result[u] = f"{base}_{_doc_hash(u)}"
    if len(set(result.values())) != len(result):
        raise ValueError(
            "文档快照名去重后仍冲突，请检查 build_doc_slug_map：" + ", ".join(result)
        )
    return result


def _doc_hash(url: str) -> str:
    """URL 短哈希，仅供文档快照名去重使用（非安全用途）。"""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
