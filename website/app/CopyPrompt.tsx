"use client";

import { useState } from "react";

export function CopyPrompt({ prompt }: { prompt: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    await navigator.clipboard.writeText(prompt);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }

  return (
    <div className="agent-prompt-card">
      <div className="agent-prompt-bar">
        <span>INSTRUCTION.txt</span>
        <button type="button" onClick={copy} aria-label="Copy coding-agent instruction">
          {copied ? "Copied" : "Copy instruction"}
        </button>
      </div>
      <pre><code>{prompt}</code></pre>
    </div>
  );
}
