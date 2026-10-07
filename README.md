# Watchdoc

> **盯住你依赖的外部文档，变了就告诉你变了什么。**  
> 定时抓取 → 提取正文 → 生成 diff → AI 总结 → 推送到会话，无变化时零 token 消耗。

[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.24.0-blueviolet)](https://github.com/AstrBotDevs/AstrBot)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-v0.1.0-green)](CHANGELOG.md)

---

## 为什么需要它

插件作者最怕的事之一：上游悄悄改了 API 文档，你的插件某天就废了，而你毫不知情。

人工盯不现实——文档站没有统一的变更订阅，RSS 基本绝迹，靠记忆隔三差五去翻又必然漏。Watchdoc 把这件事变成一条流水线：你只要告诉它「盯这个页面的这块区域」，剩下交给定时任务。

**设计上的几个硬约束：**

- **无变化不烧 token** —— 定时回调是插件自己的函数，不走 Agent / LLM，只有真的检测到变更才会调模型。
- **只比对正文** —— 用 CSS 选择器把导航、页脚、"最后更新"之类的噪音裁掉，否则每次构建都会误报。
- **变更即证据** —— 推送的不只是"有变化"，而是一份 AI 总结，并存档可回溯。

---

## 工作流程

```text
定时触发
  │
  ├─ 抓取 HTML（aiohttp，带浏览器 UA）
  ├─ 按 CSS 选择器裁剪正文 → 转 Markdown
  ├─ 归一化（压缩空白 + 剔除忽略规则）
  ├─ 与上次快照比对哈希
  │     └─ 相同 → 直接返回（零 LLM 调用）
  │
  └─ 有变化 → 生成 unified diff
        ├─ 按推送会话的人格分组（人格相同共用一份总结）
        ├─ 调 LLM 总结（单次调用，或开启 Agent 循环让模型自己查原文）
        ├─ 推送到配置的会话，并追加进会话历史
        └─ 存档到插件数据目录
```

首次检查只建立基线，不会调用模型，也不会推送。

---

## 安装

在 AstrBot 面板的「插件」页从 URL 安装：

```text
https://github.com/sch-chun/astrbot_plugin_watchdoc
```

依赖（`beautifulsoup4` / `lxml` / `markitdown-no-magika`）会由 AstrBot 按 `requirements.txt` 自动安装。

---

## 使用

### 1. 配置页添加监控项

插件详情页 → **Watchdoc 配置**：

1. 顶部填入文档地址，点「抓取」；
2. 点「选取区域」，在预览里点选要监控的正文块 —— 右栏会给出可直接抄用的选择器候选，语义化稳定的排前面，构建产物型的类名标注为「易失效」；
3. 在右栏填名称、选择器、推送会话、任务指令，点「保存」。

保存即写入插件存储并生效。也可以在命令行用 `/watchdoc probe <url>` 拿到候选清单。

推送会话从 AstrBot 已知的会话里选，也可以手动填 UMO（形如 `aiocqhttp:GroupMessage:123456`）。可以填多个；留空则检测到变更只落盘、不推送。

### 2. 定时任务

保存后，面板「定时任务」页会自动出现 `watchdoc:check`。它是插件托管的：

- 有监控项时自动创建，清空监控项后自动撤掉；
- 周期由插件配置的 `cron` 决定，改动在插件重载或保存监控项后生效；
- 你手动暂停过的话，重新同步时会保留暂停状态，不会自作主张拉起来。

### 3. 指令

```text
/watchdoc                 立即检查所有监控项
/watchdoc list            列出监控项及其基线、推送情况
/watchdoc reset <id>      删除该监控项的基线快照，下次检查重建
/watchdoc probe <url>     探测该页面的正文容器，给出选择器建议
```

---

## 配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `cron` | `0 */6 * * *` | 检查周期，五段式 cron 表达式 |
| `summary_provider` | 留空 | 总结用模型，留空则使用 AstrBot 的默认对话模型 |
| `use_session_persona` | `true` | 按推送会话的人格生成总结；人格相同的会话共用一份 |
| `agent_mode` | `false` | 用 Agent 循环做总结，模型可自行调用人格允许的工具并查阅变更前后原文 |
| `agent_max_steps` | `16` | Agent 最大轮次。设为 16 及以上时，AstrBot 会在用到 80% / 90% / 95% 时提前追加收尾提醒 |
| `ignore_patterns` | `[]` | 全局忽略规则（正则），在归一化阶段从文本中删除匹配内容 |

每个监控项还可以单独写一条**任务指令**，随 diff 一起送给模型；留空则回落到内置默认指令（「总结变化 / 对调用方的影响 / 需做的调整」）。

---

## 数据都存在哪

| 数据 | 位置 |
| --- | --- |
| 监控项配置 | 插件 KV 存储（按插件隔离，由 AstrBot 管理） |
| 基线快照 | `data/plugin_data/astrbot_plugin_watchdoc/snapshots/` |
| 变更存档 | `data/plugin_data/astrbot_plugin_watchdoc/history/<id>/` |

每个监控项的存档最多保留 50 份，超出删最旧的——选择器失效时可能每次都判定为变更，这是防无限增长的兜底。

---

## 已知限制

- **纯 HTTP 抓取，不执行 JavaScript。** SPA 或需登录才能看到正文的页面抓不到内容，日志会提示「页面可能需要 JS 渲染」，这种情况建议换成静态源（例如文档的 Markdown 源文件）。
- **选择器失效会导致持续误报。** 选中构建产物型的类名（带 hash）等于埋雷，配置页会把这类标注为「易失效」，尽量选语义化的容器。
- **归一化刻意不做日期替换。** 日期可能是有意义的版本标识，替换掉会漏掉真正的变更；确有动态噪音时用 `ignore_patterns` 精确剔除。

---

## 开发

```bash
pytest -q            # 85 条用例，全部离线，不依赖网络
ruff format . && ruff check .
```

测试位于 `tests/`，抓取与预览环节用本地 HTML 桩替换，重点覆盖异常与边界路径。

实现细节：`main.py` 只放插件类与 handler（AstrBot 按入口模块路径索引插件），业务逻辑在 `src/` 下，按抓取 / 人格 / 总结 / 存储 / 文本切分。
