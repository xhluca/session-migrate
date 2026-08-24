"use client";

import { useState } from "react";

const SESSION_TITLE = "fix-timeline-merging";

function TerminalRun({ run }: { run: number }) {
  return (
    <div className="terminal-run" key={run}>
      <p className="terminal-command">
        <span>❯</span>{" "}
        <i>smigrate transfer --title {SESSION_TITLE} --from claude --to codex</i>
      </p>
      <div className="terminal-output">
        <p><b>source</b><span>Claude Code · {SESSION_TITLE}</span></p>
        <p><b>mapping</b><span className="progress"><i /></span><em>100%</em></p>
        <p><b>target</b><span>Codex · native session ready</span></p>
      </div>
      <p className="terminal-success"><span>✓</span> Continue <strong>{SESSION_TITLE}</strong> in Codex</p>
    </div>
  );
}

export function HeroTerminal() {
  const [run, setRun] = useState(0);

  return (
    <div className="terminal" key={run} aria-label={`Migrate ${SESSION_TITLE} from Claude Code to Codex`}>
      <div className="terminal-bar">
        <div className="window-dots"><i /><i /><i /></div>
        <span>~/project</span>
        <div className="terminal-bar-actions">
          <span className="terminal-state">migration complete</span>
          <button
            type="button"
            className="terminal-reset"
            onClick={() => setRun((value) => value + 1)}
            aria-label="Replay the example migration"
            title="Replay"
          >
            ↻
          </button>
        </div>
      </div>
      <div className="terminal-body"><TerminalRun run={run} /></div>
    </div>
  );
}
