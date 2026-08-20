import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("serves the canonical coding-agent procedure", async () => {
  const [canonical, publicCopy] = await Promise.all([
    readFile(new URL("../../llms.txt", import.meta.url), "utf8"),
    readFile(new URL("../public/llms.txt", import.meta.url), "utf8"),
  ]);
  assert.equal(publicCopy, canonical);
  assert.match(canonical, /https:\/\/pypi\.org\/simple\//);
  assert.match(canonical, /never reproduce or infer a native schema manually/);
  assert.match(canonical, /raw[\s\S]*catalog\/search\/show JSON[\s\S]*never stream/i);
  assert.match(canonical, /--session-id/);
  assert.match(canonical, /Never overwrite an artifact/);
});

test("server-renders the complete project landing page", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>session-migrate — Carry the conversation forward<\/title>/i);
  assert.match(html, /Your conversation/);
  assert.match(html, /should travel with you/);
  assert.match(html, /uv tool install session-migrate/);
  assert.match(html, /Tell your agent/);
  assert.match(html, /Copy coding-agent instruction/);
  assert.match(html, /session-migrate\.github\.io\/llms\.txt/);
  assert.match(html, /\[UUID OR TITLE\] from \[SOURCE\] to \[TARGET\]/);
  assert.match(html, /Native in/);
  assert.match(html, /Native out/);
  assert.match(html, /LIVE NATIVE HANDOFF · 1×/);
  assert.match(html, /real-time typing, history review, and continuation/);
  assert.match(html, /Choose demo target/);
  assert.match(html, /Claude[\s\S]{0,40}→[\s\S]{0,40}Pi/);
  assert.match(html, /Claude[\s\S]{0,40}→[\s\S]{0,40}Codex/);
  assert.match(html, /aria-pressed="true"/);
  assert.match(html, /Pause trajectory animation/);
  assert.doesNotMatch(html, /<video/);
  for (const agent of ["Claude", "Codex", "Pi", "OpenCode", "Copilot", "Antigravity", "Cursor[*]"]) {
    assert.match(html, new RegExp(agent));
  }
  assert.match(html, /49<\/strong><span>source → target routes/);
  assert.match(html, /diagonal routes are portable rewrites/);
  assert.match(html, /Cursor support is experimental, version-pinned, and text-only/);
  assert.match(html, /<details class="snapshots">/);
  assert.match(html, /Compare the native sessions/);
  assert.match(html, /Claude Code native TUI before migration/);
  assert.match(html, /Pi native TUI after migration/);
  assert.match(html, /Actual TUI screenshots/);
  assert.doesNotMatch(html, /<details class="snapshots" open/);
  assert.match(html, /<link rel="canonical" href="https:\/\/session-migrate\.github\.io\/?"\/>/);
  assert.match(html, /<meta property="og:image" content="https:\/\/session-migrate\.github\.io\/og\.png"\/>/);
  assert.doesNotMatch(html, /codex-preview|SkeletonPreview|react-loading-skeleton/);
});
