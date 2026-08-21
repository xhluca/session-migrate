"use client";

import { useState } from "react";

export function CopyCommand({ command, prefix = "$" }: { command: string; prefix?: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    await navigator.clipboard.writeText(command);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }

  return (
    <button className="copy-command" type="button" onClick={copy} aria-label={`Copy ${command}`}>
      <code><span>{prefix}</span> {command}</code>
      <b aria-live="polite">{copied ? "Copied" : "Copy"}</b>
    </button>
  );
}
