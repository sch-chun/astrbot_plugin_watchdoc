"""Watchdoc 插件的常量。"""

PLUGIN_NAME = "astrbot_plugin_watchdoc"
CRON_NAME_PREFIX = "watchdoc"
CRON_JOB_NAME = f"{CRON_NAME_PREFIX}:check"
# 监控项存在插件 KV 存储里，不落文件（配置类数据按官方规范走 KV）
TARGETS_KEY = "targets"
DEFAULT_CRON = "0 */6 * * *"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# 单次抓取的响应体上限。定时任务是无人值守的，一个失控的故障页会把整轮检查
# 拖死，宁可放弃这一个监控项也不能连累其它项。
# 实测转换开销：约 7 秒 / MB，进程 RSS 增量约 40~60 倍输入大小（1 MB→+69 MB、
# 5 MB→+307 MB、10 MB→+381 MB）。20 MB 是「明显不是文档页」的量级，再往上
# 内存峰值就到 GB 级了，小内存机器请调低这里。
MAX_HTML_BYTES = 20 * 1024 * 1024
# 分块读取的块大小
FETCH_CHUNK_BYTES = 64 * 1024
MAX_HISTORY_PER_TARGET = 50
# AstrBot 只在步数预算 >= 16 时才在 80%/90%/95% 处追加收尾提醒
# （tool_loop_agent_runner.py 的步数预算提示），低于 16 就只剩到达上限那一次
DEFAULT_AGENT_STEPS = 16

# 默认任务指令，监控项未配置 instruction 时使用
DEFAULT_INSTRUCTION = (
    "请只依据下面的 unified diff 作答，不要臆测。总结：1) 发生了什么变化；"
    "2) 对调用方的影响；3) 需要做什么调整。用中文，300 字以内，直接给结论。"
)
# 与上面同构，只多一句：agent 模式下模型还能用工具取回原文
AGENT_INSTRUCTION = (
    "请只依据下面的 unified diff 和你用工具取回的原文作答，不要臆测。"
    "总结：1) 发生了什么变化；2) 对调用方的影响；3) 需要做什么调整。"
    "用中文，300 字以内，直接给结论。"
)

# 工具返回原文的预算：文档动辄几万字符，整份塞回上下文会挤爆
TOOL_READ_CHARS = 6000
TOOL_READ_DEFAULT_LINES = 200
TOOL_READ_MAX_LINES = 500
# 搜索命中处前后各取多少行
TOOL_SEARCH_CONTEXT_LINES = 8
TOOL_SEARCH_MAX_HITS = 5
DOC_TOOL_NAMES = ("watchdoc_read_document", "watchdoc_search_document")

# 常见文档站的正文容器候选，按语义化优先排列
SELECTOR_CANDIDATES = (
    "main",
    "article",
    "[role=main]",
    "#content",
    "#mainContent",
    "#doc-content",
    ".markdown-body",
    ".doc-content",
    ".doc-body",
    ".help-content",
    ".article-content",
    ".content-main",
    ".theme-doc-markdown",
    ".vp-doc",
)
MIN_TEXT_LEN = 200
