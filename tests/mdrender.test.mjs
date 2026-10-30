// mdrender.js 单元测试：Node 内置 node --test，零新依赖。
// 覆盖三类官方 .md 源写法、围栏属性容忍、非法 src 回落。
import { test } from "node:test";
import assert from "node:assert/strict";
import { renderMarkdown } from "../pages/designer/mdrender.js";

test("围栏 language 后带属性时仍提取语言", () => {
  const html = renderMarkdown("```python theme={null}\nprint(1)\n```");
  assert.match(html, /class="lang-python"/);
});

test("围栏 {=html} 不产生 language-{=html} 类名且仍是真正的代码块", () => {
  const html = renderMarkdown("```{=html}\n<div>x</div>\n```");
  // 必须是真正的代码块：内容 <div>x</div> 落在 <code> 内，而非段落转义后的空壳 <pre>
  assert.match(html, /<pre class="md-code"><code>&lt;div&gt;x&lt;\/div&gt;<\/code><\/pre>/);
  // 不得出现空 <code></code> 壳（v0.2.4 初版退化成「段落转义 + 空 <pre>」的标志）
  assert.doesNotMatch(html, /<code><\/code>/);
  // 不得把 {=html} 当成语言
  assert.doesNotMatch(html, /language-\{=html\}/);
});

test("围栏后带前导空格（``` python）仍正确识别为代码块", () => {
  const html = renderMarkdown("``` python\nprint(1)\n```");
  // 内容 print(1) 必须在 <code> 内、带语言类，而非退化成段落；也不得有空 <code></code> 壳
  assert.match(html, /<pre class="md-code"><code class="lang-python">print\(1\)<\/code><\/pre>/);
  assert.doesNotMatch(html, /<code><\/code>/);
  assert.doesNotMatch(html, /<p>``` python/);
});

test("自闭合 <video /> 渲染为可播放元素", () => {
  const html = renderMarkdown('<video controls src="https://x.com/a.wav" />');
  assert.match(html, /<video controls src="https:\/\/x.com\/a.wav"><\/video>/);
});

test("同行闭合 <video></video> 也能渲染", () => {
  const html = renderMarkdown('<video controls src="https://x.com/a.wav"></video>');
  assert.match(html, /<video controls src="https:\/\/x.com\/a.wav"><\/video>/);
});

test("<video><source src></video> 多格式回退渲染为可播放元素", () => {
  // 单 <source> 子标签：优先以 <source> 形式渲染
  const html = renderMarkdown('<video><source src="https://x.com/a.wav"></video>');
  assert.match(html, /<video controls><source src="https:\/\/x.com\/a.wav"><\/video>/);
});

test("多个 <source> 多格式回退全部保留", () => {
  // 双 source 是常见写法（如 a.mp4 + b.webm），必须都渲染，不能静默回落成转义文本
  const html = renderMarkdown(
    '<video><source src="https://x.com/a.mp4"><source src="https://x.com/b.webm"></video>',
  );
  assert.match(
    html,
    /<video controls><source src="https:\/\/x.com\/a.mp4"><source src="https:\/\/x.com\/b.webm"><\/video>/,
  );
});

test("<audio> 自闭合渲染为可播放元素", () => {
  const html = renderMarkdown('<audio controls src="https://x.com/a.mp3" />');
  assert.match(html, /<audio controls src="https:\/\/x.com\/a.mp3"><\/audio>/);
});

test("非 http(s) 的 src 回落为转义文本", () => {
  const html = renderMarkdown('<video controls src="javascript:alert(1)" />');
  assert.match(html, /&lt;video/);
  assert.doesNotMatch(html, /<video controls src="javascript/);
});

test("非媒体行不被误判为媒体", () => {
  // 行内出现 <video> 但前后有文本，不应被当作独立媒体渲染
  const html = renderMarkdown("看这个 <video controls src='https://x.com/a.wav' /> 结束");
  assert.match(html, /&lt;video/);
  assert.doesNotMatch(html, /<video controls src=/);
});
