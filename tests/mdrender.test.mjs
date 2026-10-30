// mdrender.js 单元测试：Node 内置 node --test，零新依赖。
// 覆盖三类官方 .md 源写法、围栏属性容忍、非法 src 回落。
import { test } from "node:test";
import assert from "node:assert/strict";
import { renderMarkdown } from "../pages/designer/mdrender.js";

test("围栏 language 后带属性时仍提取语言", () => {
  const html = renderMarkdown("```python theme={null}\nprint(1)\n```");
  assert.match(html, /class="lang-python"/);
});

test("围栏 {=html} 不产生 language-{=html} 类名", () => {
  const html = renderMarkdown("```{=html}\n<div>x</div>\n```");
  assert.doesNotMatch(html, /language-\{=html\}/);
  // 仍应作为代码块渲染，只是不带语言类
  assert.match(html, /<pre class="md-code"><code>/);
});

test("自闭合 <video /> 渲染为可播放元素", () => {
  const html = renderMarkdown('<video controls src="https://x.com/a.wav" />');
  assert.match(html, /<video controls src="https:\/\/x.com\/a.wav"><\/video>/);
});

test("同行闭合 <video></video> 也能渲染", () => {
  const html = renderMarkdown('<video controls src="https://x.com/a.wav"></video>');
  assert.match(html, /<video controls src="https:\/\/x.com\/a.wav"><\/video>/);
});

test("<video><source src></video> 多格式回退也能渲染", () => {
  const html = renderMarkdown('<video><source src="https://x.com/a.wav"></video>');
  assert.match(html, /<video controls src="https:\/\/x.com\/a.wav"><\/video>/);
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
