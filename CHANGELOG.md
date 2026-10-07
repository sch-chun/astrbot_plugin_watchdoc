# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/lang/zh-CN/).

## [0.1.1] - 2026-10-08

### Fixed

- **抓取加响应体上限**（20 MB）：`fetch_html` 原先把整个响应读进内存，一个失控的
  故障页会连带拖死整轮串行检查。改为先校验 `Content-Length`、再按块流式读取并累计
  校验，两处都能卡住上限；超限抛 `PageTooLargeError`，由 `_check_all` 按单个监控项
  失败记录日志，不影响其余项。
- **归一化移出事件循环**：`_normalize` 跑的是用户填的 `ignore_patterns` 正则，长文本上
  灾难性回溯会卡死事件循环。上一行的 Markdown 转换已经在线程里，这行漏了，补上。
- **删除监控项时清掉基线快照**：从配置页移除监控项后 `snapshots/<id>.md` 曾残留，
  同 ID 的新监控项会拿旧快照当基线，第一次检查就误报变更。变更存档是有意保留的
  记录，不在清理范围内。

### Changed

- **探测兜底扫描改为线性**：原先对每个 `div` 都取一次全文，嵌套结构被反复遍历。
  父节点文本必然包含子节点，因此文本量最大的一定在最外层，改为只称顶层 div。
  推荐结果不变，仅消除大页面上的平方级开销。

## [0.1.0] - 2026-10-07

首个版本。

### Added

- **定时文档监控**：按 cron 周期抓取目标页面，按 CSS 选择器裁剪正文后转 Markdown，归一化并与上次快照比对哈希；无变化时直接返回，不走 LLM。
  - 首次检查只建立基线快照，不调用模型、不推送。
  - 命中变更时生成 unified diff，行号基于 `splitlines()`，与查阅工具的取行方式一致。
- **配置页（Page）可视化配置**：输入地址抓取后在 Shadow DOM 里渲染净化后的页面，点选正文块即得到选择器候选；候选按「语义化稳定性优先、其次文本量」排序，构建产物型类名标注为「易失效」。
  - 监控项字段：名称、选择器、推送会话（可多选或手填 UMO）、启用开关、自定义任务指令。
  - 地址在右栏只读，由顶部抓取或点选已有项决定；抓取成功前不显示表单，避免填了才发现抓不到。
- **AI 总结与推送**：按推送会话的人格分组生成总结，人格相同的会话共用一份；人格不同时各生成一份并分别推送。
  - 任务指令可在监控项上单独配置，留空回落到内置默认指令（「变化 / 对调用方的影响 / 需做的调整」）。
  - 有人格时 system prompt 只放人格小节，与主 Agent 的注入结果逐字一致以命中提示缓存；任务约束一律放 user 轮。
  - 推送成功后把正文按 user / assistant 成对追加进会话历史，从未说过话的会话会先建对话。
- **Agent 循环模式**（`agent_mode`，默认关闭）：改用 `tool_loop_agent` 做总结，模型可自行调用人格允许的工具，并用 `watchdoc_read_document` / `watchdoc_search_document` 按行区间或关键词查阅变更前后原文。
  - 工具返回带行号并受字符预算约束；工具只在 agent 循环内可见，不注册进全局工具集。
  - 模型来源统一为默认对话模型（可由 `summary_provider` 指定）；`agent_max_steps` 默认 16，以启用 AstrBot 在步数预算 80% / 90% / 95% 处的收尾提醒。
  - agent 失败时退化为单次调用，保证变更仍有一份总结。
- **托管定时任务**：`initialize()`（插件加载与重载均触发）与保存监控项后同步 `watchdoc:check`——有监控项则创建，清空则撤掉；重新注册时继承既有任务的启用状态。
- **存储分层**：监控项走插件 KV 存储；基线快照与变更存档落 `data/plugin_data/astrbot_plugin_watchdoc/`，每个监控项存档上限 50 份。
- **指令**：`/watchdoc`（立即检查）、`/watchdoc list`、`/watchdoc reset <id>`、`/watchdoc probe <url>`。
- **归一化与忽略规则**：默认只压缩空白，刻意不做日期替换；`ignore_patterns` 可配置全局正则，监控项也可覆盖。
- 文档：`README.md` 与 `CHANGELOG.md`；85 条离线单元测试。
