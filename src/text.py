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
    """把监控项 ID 转成安全文件名。

    Args:
        raw: 原始 ID。

    Returns:
        只含字母数字下划线和短横线的文件名。
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:64] or "target"
