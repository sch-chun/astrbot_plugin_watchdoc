/**
 * Watchdoc 配置页专用 Markdown 渲染器（无第三方依赖）。
 *
 * 只覆盖技术文档的主流语法子集：ATX 标题、段落、有序 / 无序列表（支持一级嵌套）、
 * 围栏代码块（语言后允许附加属性，如 Mintlify 的 `theme={null}`）、GFM 表格、引用、
 * 分割线，独立的 `<video>` / `<audio>` 媒体标签（src 受限白名单），以及行内的代码 /
 * 粗体 / 斜体 / 删除线 / 链接 / 图片 / 自动链接。YAML frontmatter 只用于标题识别，
 * 渲染时整段剥掉。
 *
 * 安全模型：远程 Markdown 不可信。原文一律先转义，渲染器只输出自身产生的标签；
 * 链接 / 图片地址走白名单（http(s) / mailto / 站内相对地址 / 锚点 / base64 图片），
 * 挡住 javascript: 等可执行 scheme。原文里的裸 HTML 不会被执行，按文本显示。
 */

// 单引号一并转义：当前属性全部用双引号包裹，转义它纯属防御纵深
const ESCAPE_MAP = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ESCAPE_MAP[ch]);
}

// 链接 / 图片地址白名单。入参已是转义形态：先把实体还原成原文再校验 scheme，
// 通过则原样返回（转义后的文本直接进属性值是安全的）
const SAFE_URL_RE =
  /^(?:https?:|mailto:|#|\.{0,2}\/|data:image\/(?:png|jpe?g|gif|webp);base64,)/i;

function safeUrl(escapedUrl) {
  const probe = escapedUrl
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'");
  // 浏览器解析 URL 时会吃掉控制字符与空白，"java\tscript:" 之类的绕过必须先剥掉再匹配
  const compact = probe.replace(/[\u0000-\u001f\u007f\s]/g, "");
  return SAFE_URL_RE.test(compact) ? escapedUrl : "";
}

// 围栏代码块起始：行首 3+ 个反引号或波浪线，可带 info string。
// 匹配与赋值分离（v0.2.4 初版把语言直接写进正则字符类，导致 ` ```{=html} ` 整行
// 匹配失败、退化成「段落转义 + 空 <pre>」壳）：这里正则放宽——info string 允许
// 前导空白（CommonMark 允许 ` ``` python ` 这种写法）并捕获首个非空词，其后的
// 附加属性（如 Mintlify 产出的 ` ```python theme={null} `）整体忽略；赋值处再用
// LANG_RE 白名单收紧，只把合法语言标识当作类名。这样 ` ```{=html} ` 仍被正确识别
// 为代码块（只是不出 language-{=html} 类），` ``` python ` 也照常工作。
const FENCE_RE = /^(`{3,}|~{3,})\s*(\S+)?(?:\s.*)?\s*$/;
// 语言标识白名单：仅 [A-Za-z0-9_+#.-]+ 才当作代码块语言类名，其余（如 {=html}）
// 一律视为无语言。
const LANG_RE = /^[A-Za-z0-9_+#.\-]+$/;
// 收尾围栏：同字符、不短于起始、不带 info string
const FENCE_CLOSE_RE = /^(`{3,}|~{3,})\s*$/;

/** 判断是否为某个起始围栏的收尾行。 */
function isFenceClose(line, marker) {
  const m = line.match(FENCE_CLOSE_RE);
  return !!m && m[1][0] === marker[0] && m[1].length >= marker.length;
}
const HEADING_RE = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
const HR_RE = /^\s*(?:-\s*){3,}$|^\s*(?:\*\s*){3,}$|^\s*(?:_\s*){3,}$/;
const LIST_ITEM_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const TABLE_SEP_RE = /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/;

/** 剥掉文档头部的 YAML frontmatter（标题已在后端单独提取，这里只做渲染剥离）。 */
function stripFrontmatter(text) {
  if (!text.startsWith("---")) return text;
  const lines = text.split("\n");
  for (let i = 1; i < lines.length; i++) {
    const t = lines[i].trim();
    if (t === "---" || t === "...") return lines.slice(i + 1).join("\n");
  }
  return text;
}

/** 表格行切分：处理行首尾管道符与转义竖线。 */
function splitRow(row) {
  let r = row.trim();
  if (r.startsWith("|")) r = r.slice(1);
  if (r.endsWith("|")) r = r.slice(0, -1);
  const cells = [];
  let cur = "";
  for (let i = 0; i < r.length; i++) {
    if (r[i] === "\\" && r[i + 1] === "|") {
      cur += "|";
      i++;
      continue;
    }
    if (r[i] === "|") {
      cells.push(cur);
      cur = "";
      continue;
    }
    cur += r[i];
  }
  cells.push(cur);
  return cells.map((c) => c.trim());
}

function alignOf(spec) {
  const s = spec.replace(/\s/g, "");
  if (s.startsWith(":") && s.endsWith(":")) return "center";
  if (s.endsWith(":")) return "right";
  return "";
}

/** 解析 GFM 表格；调用方保证 lines[start+1] 是分隔行。返回 [html, 下一行下标]。 */
function parseTable(lines, start) {
  const header = splitRow(lines[start]);
  const aligns = splitRow(lines[start + 1]).map(alignOf);
  let i = start + 2;
  const rows = [];
  while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
    rows.push(splitRow(lines[i]));
    i++;
  }
  const attr = (idx) => (aligns[idx] ? ` style="text-align:${aligns[idx]}"` : "");
  const head = header
    .map((c, idx) => `<th${attr(idx)}>${inline(c)}</th>`)
    .join("");
  const body = rows
    .map(
      (r) =>
        `<tr>${header
          .map((_, idx) => `<td${attr(idx)}>${inline(r[idx] ?? "")}</td>`)
          .join("")}</tr>`,
    )
    .join("");
  return [`<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`, i];
}

/**
 * 解析列表块（支持一级嵌套）。
 *
 * 先按基础缩进收集整个列表块（到「缩进更浅的非空行」或两个连续空行止），
 * 再在块内按项拆分；每项去掉「标记 + 一个空格」宽度的前缀后，剩余行作为
 * 子块递归走块级渲染——嵌套列表 / 项内代码块由此自然成立。
 */
function parseList(lines, start) {
  const head = lines[start].match(LIST_ITEM_RE);
  const baseIndent = head[1].length;
  const ordered = /^\d/.test(head[2]);

  // 收集列表块范围 [start, end)
  let end = start;
  let pendingBlank = false;
  while (end < lines.length) {
    const line = lines[end];
    if (!line.trim()) {
      pendingBlank = true;
      end++;
      continue;
    }
    const indent = line.match(/^\s*/)[0].length;
    if (indent < baseIndent) break;
    if (indent === baseIndent) {
      if (LIST_ITEM_RE.test(line)) {
        // 同级但切换了有序 / 无序标记：属于新列表，当前列表到此结束
        if ((/^\s*\d/.test(line)) !== ordered) break;
      } else if (pendingBlank) {
        // 空行后回到本级缩进且不是新列表项：是列表结束后新起的块，列表到此为止
        break;
      }
      // 同级的惰性续行（无空行相隔）：继续并入当前项
    }
    pendingBlank = false;
    end++;
  }

  // 按项拆分：非列表项行（惰性续行）并入上一项，绝不静默丢弃内容
  const items = [];
  let current = null;
  let i = start;
  while (i < end) {
    const line = lines[i];
    const m = line.match(LIST_ITEM_RE);
    if (m && m[1].length === baseIndent) {
      const contentIndent = m[0].length;
      let j = i + 1;
      while (
        j < end &&
        (lines[j].trim() === "" || lines[j].match(/^\s*/)[0].length > baseIndent)
      ) {
        j++;
      }
      // 首元素是标记行自身的文本，其后是去宽度的续行；空行归一成空行
      current = [m[3]].concat(
        lines
          .slice(i + 1, j)
          .map((l) =>
            l.trim() ? l.slice(Math.min(contentIndent, l.match(/^\s*/)[0].length)) : "",
          ),
      );
      items.push(current);
      i = j;
      continue;
    }
    if (current) {
      current.push(line.trim());
    }
    i++;
  }

  // 单项只有一段行内文本时不包 <p>（tight list，观感更接近文档站）
  const lis = items
    .map((content) => {
      const html = renderBlocks(content);
      const single = html.match(/^<p>([\s\S]*)<\/p>$/);
      return `<li>${single ? single[1] : html}</li>`;
    })
    .join("");
  const startAttr = ordered && Number(head[2].match(/\d+/)[0]) !== 1
    ? ` start="${Number(head[2].match(/\d+/)[0])}"`
    : "";
  return [`<${ordered ? "ol" : "ul"}${startAttr}>${lis}</${ordered ? "ol" : "ul"}>`, end];
}

/** 行内渲染：入参为原始（未转义）文本，输出为安全 HTML。 */
function inline(text) {
  let src = escapeHtml(text);
  // 行内代码优先抽座占位：其中的 * _ [ ] 等不再被后续规则误伤
  const codes = [];
  src = src.replace(/`([^`\n]+)`/g, (_m, code) => {
    codes.push(code);
    return `\u0000C${codes.length - 1}\u0000`;
  });
  // 图片 / 链接：地址允许一层配对括号（如 https://a.com/(b)、javascript:alert(1)），
  // 过不了白名单就退化成纯文本，不留半个空标签。
  // 入参已转义：标题组的引号形态为 &quot;，按此匹配。
  // 注意配对括号分支必须排在 [^)\s] 之前：后者也能吃掉 "("，若排在前面，
  // 引擎第一次匹配成功即止、不会回溯，括号 URL 会被截断成 "javascript:alert(1"
  const URL_IN_PAREN = String.raw`((?:\([^)\s]*\)|[^)\s])+)`;
  const TITLE_OPT = String.raw`(?:\s+&quot;.*?&quot;)?`;
  const IMG_RE = new RegExp(String.raw`!\[([^\]]*)\]\(` + URL_IN_PAREN + TITLE_OPT + String.raw`\)`, "g");
  const LINK_RE = new RegExp(String.raw`\[([^\]]+)\]\(` + URL_IN_PAREN + TITLE_OPT + String.raw`\)`, "g");
  src = src.replace(IMG_RE, (_m, alt, url) => {
    const safe = safeUrl(url);
    return safe ? `<img src="${safe}" alt="${alt}" loading="lazy" />` : alt;
  });
  src = src.replace(LINK_RE, (_m, label, url) => {
    const safe = safeUrl(url);
    // 不带 target/rel：配置页 onClick 在捕获阶段拦下了所有 <a>，跳转永远不会发生
    return safe ? `<a href="${safe}">${label}</a>` : label;
  });
  // 自动链接 <https://...>
  src = src.replace(/&lt;(https?:\/\/[^&\s]+)&gt;/g, (_m, url) => {
    const safe = safeUrl(url);
    return safe ? `<a href="${safe}">${url}</a>` : _m;
  });
  src = src.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  src = src.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  src = src.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  src = src.replace(/(^|[^a-zA-Z0-9_])_([^_\n]+)_(?![a-zA-Z0-9_])/g, "$1<em>$2</em>");
  src = src.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  // 还原行内代码（占位符只可能是本函数产生的形态）
  src = src.replace(/\u0000C(\d+)\u0000/g, (_m, idx) => `<code>${codes[Number(idx)]}</code>`);
  return src;
}

/**
 * 媒体标签匹配：整行即一个 <video>/<audio> 元素——自闭合 `<video ... />`、同行闭合
 * `<video ...></video>`、或带若干 `<source>` 子标签的多格式回退。仅整行匹配，避免
 * 误吞行内片段。捕获外层标签属性（group 2）与标签内部内容（group 3），由
 * renderMediaLine 决定是从 <source> 子标签还是外层 src 取地址。
 */
const MEDIA_RE = /^\s*<(video|audio)\b([^>]*)>([\s\S]*?)(?:<\/\1>)?\s*$/i;
const SRC_RE = /\bsrc\s*=\s*("([^"]*)"|'([^']*)')/i;

/**
 * 渲染独立的 <video> / <audio> 媒体标签（部分官方 .md 源会直接内联，如 MiniMax 文档）。
 *
 * 兼容三种官方 .md 源写法——自闭合、同行闭合、以及带若干 `<source>` 子标签的多格式
 * 回退（如 `<video><source src="a.mp4"><source src="b.webm"></video>`，常见写法就是
 * 两三个 source）。优先采用 `<source>` 子标签（多格式回退时浏览器自动选首个可播放者），
 * 没有子标签才退回外层标签的 src；任一写法都能渲染。
 * 安全模型：仅当 src 命中 http(s) 白名单时才输出可播放元素，其余属性（autoplay / loop
 * 等）一律丢弃，不执行任何脚本；非白名单或解析失败则回落到默认转义（按普通文本显示）。
 *
 * Args:
 *   line: 待判断的单行原始文本（未转义）。
 *
 * Returns:
 *   渲染后的安全媒体 HTML；不是合法媒体标签则返回 null。
 */
function renderMediaLine(line) {
  const m = line.match(MEDIA_RE);
  if (!m) return null;
  const tag = m[1].toLowerCase();
  const inner = m[3] || "";
  // 先收集所有 <source> 子标签里的 http(s) src（多格式回退常见写法）
  const sourceSrcs = [];
  const sourceRe = /<source\b[^>]*>/gi;
  let srcTag;
  while ((srcTag = sourceRe.exec(inner)) !== null) {
    const sm = srcTag[0].match(SRC_RE);
    if (!sm) continue;
    const raw = sm[2] ?? sm[3];
    if (/^https?:\/\//i.test(raw)) sourceSrcs.push(raw);
  }
  if (sourceSrcs.length > 0) {
    const sources = sourceSrcs
      .map((src) => `<source src="${escapeHtml(src)}">`)
      .join("");
    return `<${tag} controls>${sources}</${tag}>`;
  }
  // 无 <source> 子标签：退回外层标签自身的 src
  const outer = (m[2] || "").match(SRC_RE);
  if (outer) {
    const raw = outer[2] ?? outer[3];
    if (/^https?:\/\//i.test(raw)) {
      return `<${tag} controls src="${escapeHtml(raw)}"></${tag}>`;
    }
  }
  return null;
}

/** 当前行是否会开启一个新块（段落收集到这些行为止）。 */
function isBlockStart(line, next) {
  if (FENCE_RE.test(line) || HEADING_RE.test(line) || HR_RE.test(line)) return true;
  if (/^\s*>/.test(line)) return true;
  if (LIST_ITEM_RE.test(line)) return true;
  if (line.includes("|") && next && TABLE_SEP_RE.test(next) && next.includes("-")) return true;
  return false;
}

/** 块级渲染主循环：把一组行渲染成 HTML 片段。 */
function renderBlocks(lines) {
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    const fence = line.match(FENCE_RE);
    if (fence) {
      const marker = fence[1];
      // 赋值处用 LANG_RE 收紧：放宽后捕获的语言片段若不在白名单内（如 {=html}），
      // 视为无语言，避免生成 language-{=html} 这类无意义类名
      const lang = fence[2] && LANG_RE.test(fence[2]) ? fence[2] : "";
      const buf = [];
      i++;
      // 收尾须同字符且不短于起始：块内更短的同字符行（如 4 重围栏里的 ```）
      // 或另一种字符的围栏（起始为 ~~~ 时的 ```）都不会提前闭合
      while (i < lines.length && !isFenceClose(lines[i], marker)) {
        buf.push(lines[i]);
        i++;
      }
      i++; // 跳过收尾围栏（缺失时越界，由外层循环条件兜住）
      const cls = lang ? ` class="lang-${escapeHtml(lang)}"` : "";
      out.push(`<pre class="md-code"><code${cls}>${escapeHtml(buf.join("\n"))}</code></pre>`);
      continue;
    }
    const heading = line.match(HEADING_RE);
    if (heading) {
      const level = heading[1].length;
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      i++;
      continue;
    }
    if (HR_RE.test(line)) {
      out.push("<hr>");
      i++;
      continue;
    }
    if (/^\s*>/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      out.push(`<blockquote>${renderBlocks(buf)}</blockquote>`);
      continue;
    }
    if (
      line.includes("|") &&
      i + 1 < lines.length &&
      TABLE_SEP_RE.test(lines[i + 1]) &&
      lines[i + 1].includes("-")
    ) {
      const [html, next] = parseTable(lines, i);
      out.push(html);
      i = next;
      continue;
    }
    if (LIST_ITEM_RE.test(line)) {
      const [html, next] = parseList(lines, i);
      out.push(html);
      i = next;
      continue;
    }
    // 独立媒体标签（部分官方 .md 源内联 <video>/<audio>，如 MiniMax 文档）
    const media = renderMediaLine(line);
    if (media) {
      out.push(media);
      i++;
      continue;
    }
    // 段落：连续非空行接到一段，行间折行以空格连接（技术文档的硬折行多为一句话拆行）
    const buf = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i], lines[i + 1])) {
      buf.push(lines[i]);
      i++;
    }
    out.push(`<p>${inline(buf.join(" "))}</p>`);
  }
  return out.join("\n");
}

/**
 * 把 Markdown 原文渲染成「技术文档」视图的 HTML。
 *
 * Args:
 *   md: Markdown 原文。
 *
 * Returns:
 *   包裹在 `.md-doc` 容器内的 HTML 字符串，可直接进 shadow DOM。
 */
export function renderMarkdown(md) {
  // 入口先剥 NUL：行内代码占位符是 \u0000 包裹的，不允许原文注入伪造形态
  const text = stripFrontmatter(String(md ?? "").replace(/\u0000/g, "").replace(/\r\n?/g, "\n"));
  return `<div class="md-doc">${renderBlocks(text.split("\n"))}</div>`;
}

/** 「原文」视图：转义后包进 <pre>，展示将被监控的文本。 */
export function mdSourceHtml(md) {
  return `<pre class="md-source">${escapeHtml(md ?? "")}</pre>`;
}

/**
 * Markdown 视图样式：注入预览 shadow root（文档级样式穿不进 shadow 边界）。
 * 颜色全部走主题自定义属性，它们可继承进 shadow，深浅色切换自动跟随。
 */
export const MD_CSS = `
.md-doc{max-width:860px;margin:0 auto;padding:20px 24px 48px;color:var(--text);
font:14px/1.75 system-ui,-apple-system,"Segoe UI",sans-serif;word-break:break-word;}
.md-doc>:first-child{margin-top:0;}
.md-doc h1,.md-doc h2,.md-doc h3,.md-doc h4,.md-doc h5,.md-doc h6{margin:1.4em 0 .6em;line-height:1.3;font-weight:600;}
.md-doc h1{font-size:24px;padding-bottom:.3em;border-bottom:1px solid var(--border);}
.md-doc h2{font-size:19px;padding-bottom:.3em;border-bottom:1px solid var(--border);}
.md-doc h3{font-size:16px;}
.md-doc h4{font-size:14px;}
.md-doc h5,.md-doc h6{font-size:13px;color:var(--muted);}
.md-doc p{margin:.75em 0;}
.md-doc a{color:var(--accent);}
.md-doc ul,.md-doc ol{margin:.6em 0;padding-left:1.6em;}
.md-doc li{margin:.25em 0;}
.md-doc blockquote{margin:.8em 0;padding:.2em 1em;border-left:3px solid var(--accent);
background:color-mix(in srgb,var(--accent) 8%,transparent);color:var(--muted);}
.md-doc blockquote p{margin:.4em 0;}
.md-doc hr{border:0;border-top:1px solid var(--border);margin:1.6em 0;}
.md-doc code{padding:.15em .35em;border-radius:4px;background:color-mix(in srgb,var(--text) 8%,transparent);
font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92em;}
.md-doc pre.md-code{margin:.8em 0;padding:12px 14px;border:1px solid var(--border);border-radius:8px;
background:color-mix(in srgb,var(--panel) 60%,transparent);overflow-x:auto;}
.md-doc pre.md-code code{padding:0;background:none;font-size:12.5px;line-height:1.6;white-space:pre;}
.md-doc table{margin:.8em 0;border-collapse:collapse;font-size:13px;display:block;overflow-x:auto;}
.md-doc th,.md-doc td{border:1px solid var(--border);padding:6px 10px;text-align:left;}
.md-doc th{background:color-mix(in srgb,var(--panel) 70%,transparent);font-weight:600;}
.md-doc img{max-width:100%;height:auto;border-radius:6px;}
.md-source{margin:0;padding:12px;white-space:pre-wrap;word-break:break-word;
font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;line-height:1.6;color:var(--text);}
`;
