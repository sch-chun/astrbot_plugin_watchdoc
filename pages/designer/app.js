const bridge = window.AstrBotPluginPage;

const el = {
  url: document.getElementById("url"),
  fetch: document.getElementById("fetch"),
  pick: document.getElementById("pick"),
  status: document.getElementById("status"),
  preview: document.getElementById("preview"),
  hint: document.getElementById("hint"),
  candidates: document.getElementById("candidates"),
  pickEmpty: document.getElementById("pick-empty"),
  list: document.getElementById("list"),
  formTitle: document.getElementById("form-title"),
  name: document.getElementById("f-name"),
  furl: document.getElementById("f-url"),
  selector: document.getElementById("f-selector"),
  selectorHint: document.getElementById("f-selector-hint"),
  matchText: document.getElementById("match-text"),
  matchMeta: document.getElementById("match-meta"),
  layout: document.getElementById("layout"),
  side: document.getElementById("side"),
  layoutHandle: document.getElementById("layout-handle"),
  matchHandle: document.getElementById("match-handle"),
  formPanel: document.getElementById("form-panel"),
  pickPanel: document.getElementById("pick-panel"),
  enabled: document.getElementById("f-enabled"),
  sessionSelect: document.getElementById("f-session-select"),
  sessionChips: document.getElementById("f-sessions"),
  sessionManual: document.getElementById("f-session-manual"),
  addSession: document.getElementById("add-session"),
  instruction: document.getElementById("f-instruction"),
  save: document.getElementById("save-item"),
  reset: document.getElementById("reset-item"),
};

let targets = [];
let shadow = null;
let selecting = false;
let hovered = null;
let editingId = null;
let selectedSessions = [];
// 后端下发，用于把默认指令预填进输入框；用户清空后由后端兜底
// 初始文案直接取自 HTML，避免同一句话在两处维护
const INITIAL = {
  matchText: el.matchText.textContent,
  selectorHint: el.selectorHint.textContent,
  hint: el.hint.textContent,
};

let defaultInstruction = "";
// 当前正在配置的地址：只在抓取成功或点选已有项时更新，
// 顶部输入框里手打但没抓取的内容不算数
let currentUrl = "";

/* ---------- 选择器生成 ---------- */

// 与后端 _looks_stable 保持同一套规则
function isStable(sel) {
  if (/nth-child|nth-of-type/.test(sel)) return false;
  if (/[0-9a-f]{6,}/.test(sel)) return false;
  return (sel.match(/\./g) || []).length <= 2;
}

function pathOf(node, root) {
  const parts = [];
  let cur = node;
  while (cur && cur !== root && cur.nodeType === 1) {
    let part = cur.tagName.toLowerCase();
    const parent = cur.parentElement;
    if (parent) {
      const idx = Array.prototype.indexOf.call(parent.children, cur) + 1;
      part += `:nth-child(${idx})`;
    }
    parts.unshift(part);
    const joined = parts.join(" > ");
    try {
      if (root.querySelectorAll(joined).length === 1) return joined;
    } catch {
      /* 非法选择器继续向上 */
    }
    cur = parent;
  }
  return parts.join(" > ");
}

function buildCandidates(node, root) {
  const seen = new Set();
  const raw = [];
  const push = (sel) => {
    if (sel && !seen.has(sel)) {
      seen.add(sel);
      raw.push(sel);
    }
  };

  if (node.id) push(`#${node.id}`);
  for (const cls of node.classList) push(`.${cls}`);
  push(node.tagName.toLowerCase());
  push(pathOf(node, root));

  // 命中多个区域的优先级最低：它会把命中的块全部拼起来，
  // 实测中 section 命中 22 个，把整站页脚都吞进来了
  return raw
    .map((selector) => {
      let count = 0;
      try {
        count = root.querySelectorAll(selector).length;
      } catch {
        count = 0;
      }
      return { selector, count, stable: isStable(selector) };
    })
    .filter((item) => item.count > 0)
    .sort(
      (a, b) =>
        a.count - b.count ||
        Number(b.stable) - Number(a.stable) ||
        a.selector.length - b.selector.length,
    );
}

// 命中数优先于稳定性：只命中一个但脆弱，也好过命中一堆把噪音拼进来
function gradeOf(item) {
  if (item.count > 1) {
    return { label: `命中 ${item.count} 处`, cls: "weak" };
  }
  if (!item.stable) {
    return { label: "易失效", cls: "weak" };
  }
  return { label: "精确", cls: "stable" };
}

/* ---------- 预览与选取 ---------- */

function renderPreview(html) {
  el.preview.innerHTML = "";
  const inner = document.createElement("div");
  el.preview.appendChild(inner);
  shadow = inner.attachShadow({ mode: "open" });
  const box = document.createElement("div");
  box.innerHTML = html;
  shadow.appendChild(box);
  // 命中高亮的样式：带 !important，否则会被页面自带的内联样式盖掉。
  // 用 var(--accent) 而非写死颜色——自定义属性可以穿透 shadow 边界继承进来，
  // 深浅色主题切换时高亮颜色会跟着变。
  // 底色那两行：只描边时，元素边界常被相邻元素盖住看不出来，加半透明蒙版才看得出范围。
  // 先给一个写死的 rgba 兜底，支持 color-mix 的浏览器再用主题色覆盖。
  const style = document.createElement("style");
  style.textContent =
    ".watchdoc-match{outline:2px solid var(--accent,#1d9e75)!important;" +
    "outline-offset:1px;" +
    "background:rgba(29,158,117,.18)!important;" +
    "background:color-mix(in srgb,var(--accent,#1d9e75) 22%,transparent)!important;}";
  shadow.appendChild(style);
  shadow.addEventListener("mouseover", onHover);
  shadow.addEventListener("click", onClick, true);
}

// 把选择器命中的元素在预览里圈出来，让人一眼看出有没有点对。
// 选错的代价是「下次检查才发现抓错了东西」，远比多点一下严重。
function highlightMatches(selector) {
  if (!shadow) {
    el.selectorHint.textContent = "先抓取页面才能预览命中情况";
    return;
  }
  for (const node of shadow.querySelectorAll(".watchdoc-match")) {
    node.classList.remove("watchdoc-match");
  }
  if (!selector) {
    el.selectorHint.textContent = "未填选择器，将监控整页";
    el.matchMeta.textContent = "";
    el.matchText.textContent = "抓取页面后，这里显示选择器实际会监控到的全文";
    return;
  }

  let nodes;
  try {
    nodes = [...shadow.querySelectorAll(selector)];
  } catch {
    el.selectorHint.textContent = "选择器写法无效";
    el.matchMeta.textContent = "";
    el.matchText.textContent = "（选择器写法无效）";
    return;
  }
  if (!nodes.length) {
    el.selectorHint.textContent = "没有命中任何元素";
    el.matchMeta.textContent = "";
    el.matchText.textContent = "";
    return;
  }

  for (const node of nodes) node.classList.add("watchdoc-match");
  nodes[0].scrollIntoView({ block: "center" });
  // 后端是把所有命中元素的 HTML 拼在一起再转 Markdown，所以这里也全部拼起来，
  // 显示的才是真正会被监控的内容
  const full = nodes.map((node) => (node.textContent || "").trim()).join("\n\n");
  el.matchText.textContent = full || "（命中元素没有文本内容）";
  el.matchMeta.textContent = `命中 ${nodes.length} 处 · ${full.length} 字符`;
  el.selectorHint.textContent = `命中 ${nodes.length} 处，已在预览中高亮`;
}

function onHover(event) {
  if (!selecting) return;
  if (hovered) hovered.style.outline = "";
  hovered = event.target;
  if (hovered instanceof HTMLElement) {
    hovered.style.outline = "2px solid #1d9e75";
  }
}

function onClick(event) {
  if (!selecting) return;
  event.preventDefault();
  event.stopPropagation();
  selecting = false;
  el.pick.textContent = "选取区域";
  const node = event.target;
  if (!(node instanceof HTMLElement)) return;

  const root = shadow;
  const list = buildCandidates(node, root);
  el.pickEmpty.style.display = "none";
  el.candidates.innerHTML = "";

  for (const item of list) {
    const grade = gradeOf(item);
    const li = document.createElement("li");
    const tag = document.createElement("span");
    tag.className = `tag ${grade.cls}`;
    tag.textContent = grade.label;
    const code = document.createElement("code");
    code.textContent = item.selector;
    const meta = document.createElement("span");
    meta.className = "sel";
    meta.textContent = item.count === 1 ? "命中 1" : `共 ${item.count}`;
    li.append(tag, code, meta);
    li.addEventListener("click", () => {
      el.selector.value = item.selector;
      highlightMatches(item.selector);
      for (const other of el.candidates.children) other.style.borderColor = "";
      li.style.borderColor = "var(--accent)";
    });
    el.candidates.appendChild(li);
  }

  const best = list[0];
  const advice =
    best && best.count > 1
      ? "所有候选都会命中多个区域，选中的话这些块会被拼在一起，容易混入导航或页脚。"
      : "挑一个标着「精确」的候选填入右侧的选择器。";
  el.hint.textContent = `已选中 <${node.tagName.toLowerCase()}>，${advice}`;
}

/* ---------- 推送会话 ---------- */

function renderSessionChips() {
  el.sessionChips.innerHTML = "";
  if (!selectedSessions.length) {
    const empty = document.createElement("span");
    empty.className = "muted small";
    empty.textContent = "未配置推送会话";
    el.sessionChips.appendChild(empty);
    return;
  }
  selectedSessions.forEach((umo, index) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = umo;
    const close = document.createElement("button");
    close.className = "chip-close";
    close.textContent = "×";
    close.title = "移除";
    close.addEventListener("click", () => {
      selectedSessions.splice(index, 1);
      renderSessionChips();
    });
    chip.appendChild(close);
    el.sessionChips.appendChild(chip);
  });
}

function addSession(umo) {
  const value = String(umo || "").trim();
  if (!value || selectedSessions.includes(value)) return;
  selectedSessions.push(value);
  renderSessionChips();
}

async function loadSessions() {
  try {
    const data = await bridge.apiGet("sessions");
    const list = data.sessions || [];
    el.sessionSelect.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = list.length ? "选择会话…" : "暂无已知会话";
    el.sessionSelect.appendChild(placeholder);
    for (const item of list) {
      const option = document.createElement("option");
      option.value = item.umo;
      option.textContent = item.platform ? `${item.platform} · ${item.umo}` : item.umo;
      el.sessionSelect.appendChild(option);
    }
  } catch (error) {
    setStatus(`会话列表加载失败：${error.message}`);
  }
}

/* ---------- 监控项 ---------- */

function renderList() {
  el.list.innerHTML = "";
  targets.forEach((item, index) => {
    const li = document.createElement("li");
    if (item.id && item.id === editingId) li.classList.add("current");

    const top = document.createElement("div");
    top.className = "top";

    const on = item.enabled !== false;
    const state = document.createElement("span");
    state.className = `tag ${on ? "stable" : "weak"}`;
    state.textContent = on ? "启用" : "停用";

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = item.name || item.id;

    const del = document.createElement("button");
    del.className = "remove";
    del.textContent = "删除";
    del.addEventListener("click", async (event) => {
      event.stopPropagation(); // 否则会冒泡到 li，把刚删掉的项又填回表单
      targets.splice(index, 1);
      if (editingId === item.id) {
        editingId = null;
        setFormMode("");
      }
      await persist();
    });

    top.append(state, name, del);

    const sel = document.createElement("span");
    sel.className = "sel";
    const pushCount = (item.sessions || []).length;
    sel.textContent = `${item.selector || "（整页，未指定选择器）"} · 推送 ${pushCount} 个会话`;

    li.addEventListener("click", async () => {
      fillForm(item);
      // 选中即重新抓取，便于直接对照页面调整选择器
      if (item.url) await doFetch(item.url);
    });
    li.append(top, sel);
    el.list.appendChild(li);
  });
}

// 标题和次按钮随状态切换，让「现在是在编辑还是新建」一眼可见。
// 两个状态的按钮文案都保持两个字，切换时按钮宽度不会跳动。
function setFormMode(label) {
  el.formTitle.textContent = label ? `编辑：${label}` : "新建监控项";
  el.reset.textContent = label ? "放弃" : "清空";
}

function fillForm(item) {
  editingId = item.id || null;
  currentUrl = item.url || "";
  el.furl.textContent = currentUrl || "—";
  el.name.value = item.name || "";
  el.selector.value = item.selector || "";
  el.instruction.value = item.instruction || defaultInstruction;
  el.enabled.checked = item.enabled !== false;
  selectedSessions = [...(item.sessions || [])];
  renderSessionChips();
  setFormMode(item.name || item.id || "");
  renderList();
}

// 默认文案只有从后端拿到之后才存在，所以抓取成功、新建两种时机都要补一次
function ensureInstructionDefault() {
  if (!el.instruction.value.trim()) el.instruction.value = defaultInstruction;
}

// 清空到「刚打开页面」的样子：地址栏、预览、命中内容、选择器候选一并清掉。
// 只在启动和「放弃 / 清空」时用——抓取成功后只补默认值，不碰这些
function resetPage() {
  editingId = null;
  currentUrl = "";
  selecting = false;
  hovered = null;
  shadow = null;
  el.preview.innerHTML = "";
  el.url.value = "";
  el.pick.disabled = true;
  el.pick.textContent = "选取区域";
  el.status.textContent = "";
  el.hint.textContent = INITIAL.hint;
  el.pickEmpty.style.display = "";
  el.candidates.innerHTML = "";
  el.matchText.textContent = INITIAL.matchText;
  el.matchMeta.textContent = "";
  el.selectorHint.textContent = INITIAL.selectorHint;
  el.pickPanel.hidden = true;
  el.formPanel.hidden = true;
  el.name.value = "";
  // 地址也一起清掉：没有正在配置的页面时保存会被拦住，
  // 不会留下「同地址、空选择器」的重复条目
  el.furl.textContent = "—";
  el.selector.value = "";
  el.instruction.value = defaultInstruction;
  el.enabled.checked = true;
  selectedSessions = [];
  renderSessionChips();
  setFormMode("");
  renderList();
}

function slugify(url) {
  try {
    const u = new URL(url);
    return (u.hostname + u.pathname)
      .replace(/[^a-zA-Z0-9]+/g, "-")
      .replace(/^-|-$/g, "")
      .toLowerCase()
      .slice(0, 80);
  } catch {
    return "target";
  }
}

// 标识不再让用户填，只能从网址推导。同一站点下的长路径很可能撞名
// （同目录的文档前缀一大段都一样），撞了就加序号，否则后建的那条会
// 静默覆盖前一条——两者共用同一个快照文件，其中一条相当于白配了。
function nextId(url) {
  const base = slugify(url);
  let candidate = base;
  let suffix = 2;
  while (targets.some((item) => item.id === candidate)) {
    candidate = `${base}-${suffix++}`;
  }
  return candidate;
}

async function persist() {
  try {
    await bridge.apiPost("targets", { targets });
    renderList();
    setStatus(`已保存 ${targets.length} 个监控项`);
  } catch (error) {
    setStatus(`保存失败：${error.message}`);
  }
}

function setStatus(text) {
  el.status.textContent = text;
}

/* ---------- 事件绑定 ---------- */

async function doFetch(url) {
  if (!url) return setStatus("请先填写网址");
  setStatus("抓取中…");
  try {
    const data = await bridge.apiGet("preview", { url });
    renderPreview(data.html);
    highlightMatches(el.selector.value.trim());
    el.pick.disabled = false;
    el.url.value = url;
    currentUrl = url;
    el.furl.textContent = url;
    // 没抓到页面就无从选区域、也无从判断监控什么，这两块先不露出来
    el.pickPanel.hidden = false;
    el.formPanel.hidden = false;
    ensureInstructionDefault();
    if (!data.textLength || data.textLength < 500) {
      setStatus("抓到的正文很少，该页面可能需要 JS 渲染，无法可视化选取");
    } else {
      setStatus(`已加载（正文 ${data.textLength} 字符）`);
    }
  } catch (error) {
    setStatus(`抓取失败：${error.message}`);
  }
}

el.fetch.addEventListener("click", () => doFetch(el.url.value.trim()));

el.pick.addEventListener("click", () => {
  selecting = !selecting;
  el.pick.textContent = selecting ? "点页面选区" : "选取区域";
  if (!selecting && hovered) hovered.style.outline = "";
});

el.save.addEventListener("click", async () => {
  const url = currentUrl;
  if (!url) return setStatus("请先抓取页面");
  // 编辑已有项时沿用原标识：换了网址也还是同一条记录，否则会分裂成两条
  const id = editingId || nextId(url);
  const record = {
    id,
    name: el.name.value.trim() || id,
    url,
    selector: el.selector.value.trim(),
    instruction: el.instruction.value.trim(),
    enabled: el.enabled.checked,
    sessions: [...selectedSessions],
  };
  const index = targets.findIndex((item) => item.id === id);
  if (index >= 0) targets[index] = record;
  else targets.push(record);
  editingId = id;
  await persist();
});

let selectorTimer = null;
el.selector.addEventListener("input", () => {
  clearTimeout(selectorTimer);
  // 边打边算太跳，停手 400ms 再高亮
  selectorTimer = setTimeout(() => highlightMatches(el.selector.value.trim()), 400);
});

el.sessionSelect.addEventListener("change", () => {
  addSession(el.sessionSelect.value);
  el.sessionSelect.selectedIndex = 0;
});

el.addSession.addEventListener("click", () => {
  addSession(el.sessionManual.value);
  el.sessionManual.value = "";
});

el.sessionManual.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  addSession(el.sessionManual.value);
  el.sessionManual.value = "";
});

/* ---------- 拖拽调整栏宽 ---------- */

// 沙箱 iframe 没有 allow-same-origin，读 localStorage 会抛 SecurityError，
// 所以尺寸不持久化，刷新即恢复默认
function makeDraggable(handle, apply) {
  handle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add("resizing");
    const onMove = (moveEvent) => apply(moveEvent.clientX, moveEvent.clientY);
    const onUp = () => {
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      document.body.classList.remove("resizing");
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
  });
}

makeDraggable(el.layoutHandle, (x) => {
  const right = el.layout.getBoundingClientRect().right;
  el.side.style.width = `${Math.min(720, Math.max(280, right - x))}px`;
});

makeDraggable(el.matchHandle, (_x, y) => {
  const bottom = el.matchText.getBoundingClientRect().bottom;
  const height = Math.min(600, Math.max(56, bottom - y));
  // height 和 max-height 都得写成内联样式：CSS 里的 max-height 会把更大的值削回去
  el.matchText.style.height = `${height}px`;
  el.matchText.style.maxHeight = `${height}px`;
});

el.reset.addEventListener("click", resetPage);

/* ---------- 启动 ---------- */

await bridge.ready();
const loaded = await bridge.apiGet("targets");
targets = loaded.targets || [];
defaultInstruction = loaded.default_instruction || "";
await loadSessions();
resetPage();
