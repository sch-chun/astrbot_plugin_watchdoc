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
  pickCollapse: document.getElementById("pick-collapse"),
  formCollapse: document.getElementById("form-collapse"),
  enabled: document.getElementById("f-enabled"),
  sessionSelect: document.getElementById("f-session-select"),
  sessionChips: document.getElementById("f-sessions"),
  sessionManual: document.getElementById("f-session-manual"),
  addSession: document.getElementById("add-session"),
  instruction: document.getElementById("f-instruction"),
  save: document.getElementById("save-item"),
  reset: document.getElementById("reset-item"),
  previewLoading: document.getElementById("preview-loading"),
  listSpinner: document.getElementById("list-spinner"),
  listSlot: document.getElementById("list-slot"),
  confirmModal: document.getElementById("confirm-modal"),
  confirmText: document.getElementById("confirm-text"),
  confirmOk: document.getElementById("confirm-ok"),
  confirmCancel: document.getElementById("confirm-cancel"),
  matchCollapse: document.getElementById("match-collapse"),
};

let targets = [];
let shadow = null;
let selecting = false;
let hovered = null;
let editingId = null;
let selectedSessions = [];
// 列表是独立加载项，框架不等它；到位前 renderList() 一律不画
let listLoaded = false;
// 拿不到列表时留个标记，让列表自己说失败，而不是画成「暂无监控项」跟顶栏打架
let listFailed = false;
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
  // watchdoc-match 是我们给命中节点打的高亮标记类，不属于页面本身，
  // 不能当成候选选择器——否则在已高亮区域上点选会把它塞进候选列表
  for (const cls of node.classList) {
    if (cls === "watchdoc-match") continue;
    push(`.${cls}`);
  }
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
  // 目标页自己的样式表会原样进来，页面级的 position:fixed（导航、侧栏）
  // 在 shadow 里按视口定位、不被 overflow 裁剪，会盖到预览框外。
  // layout 包裹让本元素成为 fixed 后代的包含块，把它们收进预览区。
  inner.style.contain = "layout";
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
    el.matchCollapse.classList.remove("open");
    return;
  }

  let nodes;
  try {
    nodes = [...shadow.querySelectorAll(selector)];
  } catch {
    el.selectorHint.textContent = "选择器写法无效";
    el.matchMeta.textContent = "";
    el.matchText.textContent = "（选择器写法无效）";
    el.matchCollapse.classList.remove("open");
    return;
  }
  if (!nodes.length) {
    el.selectorHint.textContent = "没有命中任何元素";
    el.matchMeta.textContent = "";
    el.matchText.textContent = "";
    el.matchCollapse.classList.remove("open");
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
  el.matchCollapse.classList.add("open");
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
  // 数据没到之前列表保持收起，别先渲染出「暂无监控项」再被真实数据顶掉
  if (!listLoaded) return;
  el.list.innerHTML = "";
  if (listFailed || !targets.length) {
    const empty = document.createElement("li");
    // 失败和真空是两回事：真空说「没有」，失败要说「没拿到」，否则和顶栏的报错对不上
    empty.className = listFailed ? "empty fail" : "empty";
    empty.textContent = listFailed ? "加载失败，请刷新页面重试" : "暂无监控项";
    el.list.appendChild(empty);
    return;
  }
  targets.forEach((item) => {
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
    del.addEventListener("click", (event) => {
      event.stopPropagation(); // 否则会冒泡到 li，把刚删掉的项又填回表单
      const name = item.name || item.id;
      openConfirm(`确认删除监控项「${name}」？删除后不可恢复。`, () => {
        const i = targets.findIndex((t) => t.id === item.id);
        if (i >= 0) targets.splice(i, 1);
        if (editingId === item.id) {
          editingId = null;
          setFormMode("");
        }
        persist();
      });
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

// 表单面板启动后是隐藏的，只有抓到页面才露出来。默认指令要等一次接口往返，
// 所以放在「露出来之前」补——用户第一眼看到指令框时它就已经有值，
// 不会先空着、过一会儿才被填上
function showForm() {
  ensureInstructionDefault();
  el.formCollapse.classList.add("open");
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
  el.pickCollapse.classList.remove("open");
  el.formCollapse.classList.remove("open");
  el.matchCollapse.classList.remove("open");
  el.name.value = "";
  // 地址也一起清掉：没有正在配置的页面时保存会被拦住，
  // 不会留下「同地址、空选择器」的重复条目
  el.furl.textContent = "—";
  el.selector.value = "";
  // 启动时这里 defaultInstruction 还是空的（要等一次接口往返），但表单面板此刻
  // 是隐藏的，露出之前 showForm() 会补上，用户看不到中间的空框
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

// 删除确认模态框：页面内自建 UI，不依赖原生 confirm（沙箱 iframe 无 allow-modals，会被拦截）。
// 每次打开时绑定确定/取消/背景点击/ESC 监听，关闭时一并解绑，避免监听器泄漏
function openConfirm(message, onConfirm) {
  el.confirmText.textContent = message;
  el.confirmModal.classList.add("open");
  el.confirmCancel.focus();

  const close = () => {
    el.confirmModal.classList.remove("open");
    el.confirmOk.removeEventListener("click", onOk);
    el.confirmCancel.removeEventListener("click", onCancel);
    el.confirmModal.removeEventListener("click", onBackdrop);
    document.removeEventListener("keydown", onKey);
  };
  const onOk = () => {
    close();
    onConfirm();
  };
  const onCancel = () => close();
  const onBackdrop = (event) => {
    if (event.target === el.confirmModal) onCancel();
  };
  const onKey = (event) => {
    if (event.key === "Escape") onCancel();
  };

  el.confirmOk.addEventListener("click", onOk);
  el.confirmCancel.addEventListener("click", onCancel);
  el.confirmModal.addEventListener("click", onBackdrop);
  document.addEventListener("keydown", onKey);
}

// 抓取是真实网络请求，后端超时上限 30s，光靠顶栏一行字撑不住。
// 遮罩盖住旧预览，抓取完再淡出，新内容就是淡入而不是硬弹出来
function setPreviewLoading(on) {
  el.previewLoading.classList.toggle("on", on);
  el.fetch.disabled = on;
  el.pick.disabled = on || !shadow;
}

/* ---------- 事件绑定 ---------- */

// 预览里目标页常带外部 <link rel=stylesheet>，shadow DOM 中它们是异步加载的，
// 首帧往往还没套上样式就被画出来，于是出现「先一帧裸布局、再闪到终态」的闪动。
// 等这些样式表（及字体）真正就绪再淡入，才能消掉这帧。
function whenLinkReady(link) {
  if (link.sheet) return Promise.resolve();
  return new Promise((resolve) => {
    link.addEventListener("load", resolve, { once: true });
    link.addEventListener("error", resolve, { once: true });
  });
}

async function whenPreviewStyled(root) {
  // 用 ~= 而非 =：很多站点（如 VitePress）主样式表写成 rel="preload stylesheet"，
  // 精确匹配 rel="stylesheet" 会漏掉它，导致关键布局 CSS 没被等待、提前淡入就闪裸布局
  const links = [...root.querySelectorAll('link[rel~="stylesheet"]')];
  await Promise.all(links.map(whenLinkReady));
  if (document.fonts?.ready) {
    try {
      await document.fonts.ready;
    } catch {
      // 字体就绪失败不影响淡入，忽略
    }
  }
}

function withTimeout(promise, ms) {
  return Promise.race([promise, new Promise((resolve) => setTimeout(resolve, ms))]);
}

async function doFetch(url) {
  if (!url) return setStatus("请先填写网址");
  setStatus("抓取中…");
  setPreviewLoading(true);
  // 渲染期间先把预览隐掉：大文档同步建 shadow DOM 会整块重绘，直接显示会闪一下
  el.preview.style.opacity = "0";
  try {
    const data = await bridge.apiGet("preview", { url });
    renderPreview(data.html);
    highlightMatches(el.selector.value.trim());
    el.url.value = url;
    currentUrl = url;
    el.furl.textContent = url;
    // 没抓到页面就无从选区域、也无从判断监控什么，这两块先不露出来
    el.pickCollapse.classList.add("open");
    showForm();
    if (!data.textLength || data.textLength < 500) {
      setStatus("抓到的正文很少，该页面可能需要 JS 渲染，无法可视化选取");
    } else {
      setStatus(`已加载（正文 ${data.textLength} 字符）`);
    }
    // 等外部样式表/字体真正就绪再淡入（超时兜底，避免样式卡死时一直转圈）；
    // 等待期间 spinner 仍盖着隐掉的预览，用户看到的是正常的加载态
    await withTimeout(whenPreviewStyled(shadow), 2000);
    setPreviewLoading(false);
    // 双 rAF：先保证有一次 opacity:0 的绘制，否则浏览器可能把「置 0/置 1」合并成一次提交而看不到过渡
    await new Promise((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(resolve)),
    );
    el.preview.style.opacity = "1";
  } catch (error) {
    setPreviewLoading(false);
    el.preview.style.opacity = "1";
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
  // 保存后回到初始态：清空预览与地址、收起表单/选区面板，行为同「放弃」；
  // 先 resetPage 再提示，既回到干净界面又保留一行保存确认
  resetPage();
  setStatus(`已保存 ${targets.length} 个监控项`);
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

// 列表自己跑，不与框架互相等待：拿到就展开，拿不到也标失败态由顶栏说明
async function loadTargets() {
  try {
    const loaded = await bridge.apiGet("targets");
    targets = loaded.targets || [];
    defaultInstruction = loaded.default_instruction || "";
  } catch (error) {
    listFailed = true;
    setStatus(`监控项加载失败：${error.message}`);
  }
  listLoaded = true;
  el.listSpinner.remove();
  el.listSlot.classList.add("open");
  renderList();
}

// data-booting 让整页 opacity:0，摘不掉就什么都看不见——比看到未加载状态更糟。
// bridge.ready() 若既不 resolve 也不 reject，try/catch 两个分支都不会走到，
// 所以单独挂一个超时，保证页面一定露出来
const BOOT_TIMEOUT_MS = 5000;
let bootTimedOut = false;
const bootTimer = setTimeout(() => {
  bootTimedOut = true;
  document.documentElement.removeAttribute("data-booting");
  setStatus("初始化超时：插件接口没有响应，请刷新页面重试");
}, BOOT_TIMEOUT_MS);

try {
  // bridge.ready() 只是等上下文注入，几乎瞬时，框架随即淡入
  await bridge.ready();
  clearTimeout(bootTimer);
  resetPage();
  // 兜底已经把页面露出来了，就别再用空状态盖掉那行超时提示
  if (!bootTimedOut) document.documentElement.removeAttribute("data-booting");
  await Promise.all([loadTargets(), loadSessions()]);
} catch (error) {
  clearTimeout(bootTimer);
  document.documentElement.removeAttribute("data-booting");
  if (!bootTimedOut) setStatus(`初始化失败：${error.message}`);
}