import assert from "node:assert/strict";
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

test("server-renders the complete project landing page", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>session-migrate — Carry the conversation forward<\/title>/i);
  assert.match(html, /Your conversation/);
  assert.match(html, /should travel with you/);
  assert.match(html, /uv tool install session-migrate/);
  assert.match(html, /Native in/);
  assert.match(html, /Native out/);
  assert.match(html, /\/demo\.mp4/);
  assert.match(html, /<meta property="og:image" content="https?:\/\/[^"/]+\/og\.png">/);
  assert.doesNotMatch(html, /codex-preview|SkeletonPreview|react-loading-skeleton/);
});
