"use client";

import { useState } from "react";

const agents = [
  ["claude", "Claude Code"],
  ["codex", "Codex"],
  ["pi", "Pi"],
  ["opencode", "OpenCode"],
  ["copilot", "Copilot"],
  ["antigravity", "Antigravity"],
  ["cursor", "Cursor"],
] as const;

function agentLabel(value: string) {
  return agents.find(([id]) => id === value)?.[1] ?? value;
}

function instruction(source: string, target: string) {
  return `Follow https://session-migrate.github.io/llms.txt to migrate a session from ${agentLabel(source)} to ${agentLabel(target)}. Session: [UUID OR TITLE]`;
}

export function CopyPrompt() {
  const [copied, setCopied] = useState(false);
  const [source, setSource] = useState("claude");
  const [target, setTarget] = useState("codex");
  const prompt = instruction(source, target);

  async function copy() {
    await navigator.clipboard.writeText(prompt);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }

  return (
    <div className="agent-prompt-card">
      <div className="agent-prompt-bar">
        <span>INSTRUCTION.txt</span>
        <em>Choose a route, then copy</em>
      </div>
      <pre aria-live="polite"><code>Follow <span className="prompt-url">https://session-migrate.github.io/llms.txt</span> to migrate a session from <span className="prompt-source">{agentLabel(source)}</span> to <span className="prompt-target">{agentLabel(target)}</span>. Session: <span className="prompt-session">[UUID OR TITLE]</span></code></pre>
      <div className="agent-prompt-controls">
        <div className="agent-route" aria-label="Migration route">
          <label>
            <span>Source</span>
            <select className="source-select" value={source} onChange={(event) => setSource(event.target.value)} aria-label="Source harness">
              {agents.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
            </select>
          </label>
          <i aria-hidden="true">→</i>
          <label>
            <span>Target</span>
            <select className="target-select" value={target} onChange={(event) => setTarget(event.target.value)} aria-label="Target harness">
              {agents.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
            </select>
          </label>
        </div>
        <button type="button" className="agent-prompt-copy" onClick={copy} aria-label="Copy coding-agent instruction">
          {copied ? "Copied" : "Copy instruction"}
        </button>
      </div>
    </div>
  );
}
