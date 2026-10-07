"""让测试能够导入 AstrBot 核心与插件本身。

插件位于 data/plugins/<name>/tests/，AstrBot 包在项目根，两者都不在默认
sys.path 上，需要显式补齐。
"""

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents
sys.path.insert(0, str(_ROOT[4]))  # AstrBot 项目根（含 astrbot 包）
sys.path.insert(0, str(_ROOT[1]))  # 插件根目录

# 必须显式指定，否则 AstrBot 会按 cwd 推导数据目录，
# 在插件目录下跑 pytest 时会把 data/ 建进插件目录里
os.environ.setdefault("ASTRBOT_ROOT", str(_ROOT[4]))
