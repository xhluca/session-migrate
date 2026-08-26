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

test("serves the canonical standalone installer", async () => {
  const [canonical, publicCopy] = await Promise.all([
    readFile(new URL("../../install.sh", import.meta.url), "utf8"),
    readFile(new URL("../public/install.sh", import.meta.url), "utf8"),
  ]);
  assert.equal(publicCopy, canonical);
  assert.match(canonical, /^#!\/bin\/sh\nset -eu/m);
});

test("ships local native casts and the vendored player", async () => {
  const player = await readFile(new URL("../public/asciinema-player.min.js", import.meta.url), "utf8");
  assert.match(player.slice(0, 180), /Apache-2\.0/);

  for (const name of ["demo-claude.cast", "demo-pi.cast", "demo-codex.cast"]) {
    const text = await readFile(new URL(`../public/${name}`, import.meta.url), "utf8");
    const lines = text.trimEnd().split("\n");
    const header = JSON.parse(lines[0]);
    assert.equal(header.version, 2);
    assert.ok(header.width > 0 && header.height > 0);
    assert.ok(lines.length > 20);
    assert.doesNotMatch(text, /access_token|refresh_token|account_id|sk-ant-|Reply with exactly/);
  }

  const logo = await readFile(new URL("../public/logo-mark.svg", import.meta.url), "utf8");
  assert.match(logo, /session-migrate thread handoff mark/);
  assert.match(logo, /<path/);
});

test("preserves the previous twelve-harness social preview artifact", async () => {
  const [source, preview] = await Promise.all([
    readFile(new URL("../assets/og.svg", import.meta.url), "utf8"),
    readFile(new URL("../public/og-twelve-harnesses.png", import.meta.url)),
  ]);

  assert.match(source, /Migrate your sessions to any harness\./);
  for (const agent of ["CLAUDE", "CODEX", "PI", "OH MY PI", "OPENCODE", "COPILOT", "ANTIGRAVITY", "VIBE", "MUSE", "QWEN", "KIMI", "CURSOR"]) {
    assert.match(source, new RegExp(`>${agent}<`));
  }
  assert.match(source, />EXPERIMENTAL</);
  assert.equal(preview.subarray(1, 4).toString(), "PNG");
  assert.equal(preview.readUInt32BE(16), 1731);
  assert.equal(preview.readUInt32BE(20), 909);
});

test("keeps animated terminals inside their native windows", async () => {
  const [trajectory, styles] = await Promise.all([
    readFile(new URL("../app/LiveTrajectory.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(trajectory, /controls: false,\s+fit: "both"/);
  assert.equal(trajectory.match(/terminalLineHeight: 1\.14/g)?.length, 2);
  assert.match(trajectory, />same history</);
  assert.match(trajectory, /anchorSharedHistory\(sourceMount\.current\)/);
  assert.match(trajectory, /two distinguishable cases\./);
  assert.match(trajectory, /SOURCE_STOP = 8\.55/);
  assert.match(trajectory, /SOURCE_SPEED = 4\.6/);
  assert.match(trajectory, /SOURCE_POSTER = 37\.8/);
  assert.match(trajectory, /You've hit your limit · resets 3pm \(America\/Montreal\)/);
  assert.match(trajectory, /claude-limit-injection/);
  assert.doesNotMatch(trajectory, /context-limit/);
  assert.match(trajectory, /const LIMIT_START = 8\.7/);
  assert.match(trajectory, /const LIMIT_END = 12\.1/);
  assert.match(trajectory, /type="range"/);
  assert.match(trajectory, /\(elapsed - 15\.5\) \/ 6/);
  assert.match(trajectory, /if \(time < 27\.5\) return "convert"/);
  assert.match(trajectory, /fix-timeline-merging/);
  assert.doesNotMatch(trajectory, /1000…|2000…|3000…/);
  assert.match(styles, /native-window-source \{ top: 14%; left: 3%; width: 44%;/);
  assert.match(styles, /native-window-target \{ top: 14%; left: 53%; width: 44%;/);
  assert.match(styles, /\.history-bridge \{[^}]*left: 46\.5%; width: 7%;/);
  assert.match(styles, /@keyframes terminal-type/);
  assert.match(styles, /\.progress i \{[^}]*animation: progress 1\.35s[^}]*forwards/);
  assert.doesNotMatch(styles, /\.progress i \{[^}]*infinite/);
});

test("server-renders the complete project landing page", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>session-migrate — Migrate your sessions to any harness<\/title>/i);
  assert.match(html, /Switch agents/);
  assert.match(html, /Keep your context/);
  assert.match(html, /Move coding agent sessions among Claude Code/);
  assert.match(html, /resume where you left off/);
  assert.doesNotMatch(html, /Move real|coding-agent sessions/);
  assert.match(html, /curl -LsSf https:\/\/session-migrate\.github\.io\/install\.sh \| sh/);
  assert.match(html, /uv tool install session-migrate/);
  assert.doesNotMatch(html, /raw\.githubusercontent\.com\/xhluca\/session-migrate\/main\/install\.sh/);
  assert.match(html, /Tell your agent/);
  assert.match(html, /Copy coding-agent instruction/);
  assert.match(html, /session-migrate\.github\.io\/llms\.txt/);
  assert.match(html, /migrate a session from[\s\S]{0,200}Claude Code[\s\S]{0,100}to[\s\S]{0,100}Codex[\s\S]{0,100}Session:[\s\S]{0,100}\[UUID OR TITLE\]/);
  assert.match(html, /aria-label="Source harness"/);
  assert.match(html, /aria-label="Target harness"/);
  assert.match(html, /Choose a route, then copy/);
  assert.match(html, /class="prompt-source"[^>]*>Claude Code</);
  assert.match(html, /class="prompt-target"[^>]*>Codex</);
  assert.match(html, /class="prompt-session"[^>]*>\[UUID OR TITLE\]</);
  assert.doesNotMatch(html, /from \[SOURCE\] to \[TARGET\]/);
  assert.doesNotMatch(html, /Read\. Convert|Keep the useful history|Know what changed/);
  assert.match(html, /ACTUAL NATIVE TUIS · INTERACTIVE CAST/);
  assert.match(html, /source · migrate · resume · continue/);
  assert.doesNotMatch(html, /Claude review|Claude 2×|target continuation 1×/);
  assert.doesNotMatch(html, /Reply with exactly|synthetic compaction/);
  assert.match(html, /Choose demo target/);
  assert.match(html, /Claude[\s\S]{0,40}→[\s\S]{0,40}Pi/);
  assert.match(html, /Claude[\s\S]{0,40}→[\s\S]{0,40}Codex/);
  assert.match(html, /aria-pressed="true"/);
  assert.match(html, /Pause the migration story/);
  assert.match(html, /Rewind the migration story by five seconds/);
  assert.match(html, /Seek through the migration story/);
  assert.match(html, /demo-claude\.cast/);
  assert.match(html, /demo-pi\.cast/);
  assert.match(html, /smigrate transfer/);
  assert.match(html, /Replay the example migration/);
  assert.match(html, /fix-timeline-merging/);
  assert.doesNotMatch(html, /c3f7…|1000…|2000…|3000…/);
  assert.match(html, /history continued · ready to resume/);
  assert.doesNotMatch(html, /<video/);
  for (const agent of ["Claude", "Codex", "Pi", "Oh My Pi", "OpenCode", "Copilot", "Antigravity", "Mistral Vibe", "Muse Code", "Qwen Code", "Kimi Code", "Grok", "Kilo Code", "OpenHands", "Cursor[*]"]) {
    assert.match(html, new RegExp(agent));
  }
  assert.doesNotMatch(html, /OpenCode sessions indexed|Project validation stats/);
  assert.match(html, /logo-mark\.svg/);
  assert.match(html, /All 225 source → target routes are available/);
  for (const icon of ["claude-code", "codex", "pi", "oh-my-pi", "opencode", "copilot", "antigravity", "mistral-vibe", "muse", "qwen-code", "kimi-code", "grok", "kilo-code", "openhands", "cursor"]) {
    assert.match(html, new RegExp(`session-migrate\\.github\\.io/agents/${icon}\\.svg`));
  }
  assert.match(html, /same-agent move creates a fresh native session/);
  assert.match(html, /Text only · experimental/);
  assert.doesNotMatch(html, /<table class="matrix"/);
  assert.match(html, /<details class="snapshots">/);
  assert.match(html, /Compare the native sessions/);
  assert.match(html, /Live terminal renders/);
  assert.doesNotMatch(html, /Actual TUI screenshots/);
  assert.doesNotMatch(html, /demo-before\.png|demo-after-pi\.png/);
  assert.doesNotMatch(html, /<details class="snapshots" open/);
  assert.match(html, /<link rel="canonical" href="https:\/\/session-migrate\.github\.io\/?"\/>/);
  assert.match(html, /<meta property="og:image" content="https:\/\/session-migrate\.github\.io\/og-fifteen-harnesses\.png"\/>/);
  assert.doesNotMatch(html, /codex-preview|SkeletonPreview|react-loading-skeleton/);
});
