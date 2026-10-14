"""校验 README 徽标 / metadata.yaml / CHANGELOG 三处版本号一致。

插件发版时这三处极易漏改（已连续多次把 README 徽标落在旧版本），
用 CI 卡住，避免再靠记性。任一不一致即非零退出。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def main() -> int:
    meta = _read("metadata.yaml")
    m = re.search(r"^version:\s*(\S+)\s*$", meta, re.MULTILINE)
    meta_ver = m.group(1).strip() if m else ""

    readme = _read("README.md")
    rb = re.search(r"version-(v[\d.]+)-", readme)
    readme_ver = rb.group(1) if rb else ""

    changelog = _read("CHANGELOG.md")
    cl = re.search(r"^##\s*\[([\d.]+)\]", changelog, re.MULTILINE)
    cl_ver = f"v{cl.group(1)}" if cl else ""

    print(f"metadata={meta_ver!r}  readme={readme_ver!r}  changelog={cl_ver!r}")
    if not (meta_ver and readme_ver and cl_ver):
        print("ERROR: 有一处版本号解析不到", file=sys.stderr)
        return 1
    if not (meta_ver == readme_ver == cl_ver):
        print("ERROR: 三处版本号不一致", file=sys.stderr)
        return 1
    print("OK: 三处版本号一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
