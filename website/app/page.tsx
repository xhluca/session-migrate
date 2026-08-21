import { CopyCommand } from "./CopyCommand";
import { CopyPrompt } from "./CopyPrompt";
import { LiveTrajectory } from "./LiveTrajectory";

const agents = ["Claude", "Codex", "Pi", "OpenCode", "Copilot", "Antigravity", "Cursor*"];
const compatibility = [
  ["Claude Code", "✓", "✓", "✓", "✓", "✓", "✓", "T"],
  ["Codex", "✓", "✓", "✓", "✓", "✓", "✓", "T"],
  ["Pi", "✓", "✓", "✓", "✓", "✓", "✓", "T"],
  ["OpenCode", "✓", "✓", "✓", "✓", "✓", "✓", "T"],
  ["Copilot", "✓", "✓", "✓", "✓", "✓", "✓", "T"],
  ["Antigravity", "✓", "✓", "✓", "✓", "✓", "✓", "T"],
  ["Cursor*", "T", "T", "T", "T", "T", "T", "T"],
];
const agentInstruction = `Follow https://session-migrate.github.io/llms.txt to migrate session [UUID OR TITLE] from [SOURCE] to [TARGET] now.`;

export default function Home() {
  return (
    <main>
      <nav className="nav shell" aria-label="Main navigation">
        <a className="brand" href="#top" aria-label="session-migrate home">
          <span className="brand-mark" aria-hidden="true">↝</span>
          <span>session-migrate</span>
        </a>
        <div className="nav-links">
          <a href="#demo">Demo</a>
          <a href="#agent-prompt">Agent prompt</a>
          <a href="#compatibility">Compatibility</a>
          <a href="https://github.com/xhluca/session-migrate">GitHub</a>
        </div>
      </nav>

      <section className="hero shell" id="top">
        <div className="eyebrow"><span /> Local sessions, set free</div>
        <h1>Switch agents.<br />Keep your context.</h1>
        <p className="hero-copy">
          Move coding agent sessions among Claude Code, Codex, Pi, OpenCode,
          Copilot, Antigravity, and Cursor—then resume where you left off.
        </p>
        <div className="hero-actions">
          <CopyCommand command="uv tool install session-migrate" />
          <a className="primary-link" href="https://github.com/xhluca/session-migrate">
            View on GitHub <span>↗</span>
          </a>
        </div>

        <div className="agent-row" aria-label="Supported agents">
          {agents.map((agent, index) => (
            <div className="agent" key={agent}>
              <span className="agent-dot" /> {agent}
              {index < agents.length - 1 && <span className="agent-arrow">→</span>}
            </div>
          ))}
        </div>

        <div className="terminal" aria-label="Example session migration">
          <div className="terminal-bar">
            <div className="window-dots"><i /><i /><i /></div>
            <span>~/project</span>
            <span className="terminal-state">migration complete</span>
          </div>
          <div className="terminal-body">
            <p className="terminal-command"><span>❯</span> smigrate transfer c3f7… --from claude --to codex</p>
            <div className="terminal-output">
              <p><b>source</b><span>Claude Code · 42 messages · 7 tools</span></p>
              <p><b>mapping</b><span className="progress"><i /></span><em>100%</em></p>
              <p><b>target</b><span>Codex · native rollout ready</span></p>
            </div>
            <p className="terminal-success"><span>✓</span> Resume with <strong>codex resume c3f7…</strong></p>
          </div>
        </div>
      </section>

      <section className="agent-prompt section shell" id="agent-prompt">
        <div className="section-heading horizontal">
          <div><p>DELEGATE THE MIGRATION</p><h2>Tell your agent.<br />Let it move.</h2></div>
          <span>
            Replace three placeholders, then paste one sentence into a coding
            agent with shell access. The linked runbook carries the safeguards.
          </span>
        </div>
        <CopyPrompt prompt={agentInstruction} />
        <p className="agent-prompt-note">
          The agent reads the full procedure from llms.txt, then dry-runs,
          preserves the source, and returns the native resume command.
          <a href="https://github.com/xhluca/session-migrate/blob/main/docs/coding-agent-instruction.md"> Read the verification notes ↗</a>
        </p>
      </section>

      <section className="section shell" id="demo">
        <div className="section-heading">
          <p>NATIVE CLI · NATIVE OUTPUT</p>
          <h2>From one harness<br />to the next.</h2>
          <span>
            Watch the actual Claude Code and target TUIs—not a simulated
            terminal. Claude review plays at 2×; Pi and Codex continue at 1×.
          </span>
        </div>
        <LiveTrajectory />
      </section>

      <section className="workflow section" id="how-it-works">
        <div className="shell workflow-grid">
          <div className="section-heading compact">
            <p>HOW IT WORKS</p>
            <h2>Native in.<br />Native out.</h2>
            <span>
              No transcript-shaped text dumps. Every migration passes through a
              validated event timeline and a version-aware target writer.
            </span>
          </div>
          <div className="stream-card" aria-label="Animated migration pipeline">
            <div className="stream-labels"><span>SOURCE</span><span>PORTABLE</span><span>TARGET</span></div>
            {[
              ["user.message", "MESSAGE", "response_item"],
              ["tool_use", "TOOL_CALL", "function_call"],
              ["tool_result", "TOOL_RESULT", "function_output"],
              ["compact_summary", "COMPACTION", "compacted"],
            ].map((row, index) => (
              <div className="stream-row" style={{ "--delay": `${index * .55}s` } as React.CSSProperties} key={row[0]}>
                <code>{row[0]}</code><i /><b>{row[1]}</b><i /><code>{row[2]}</code>
              </div>
            ))}
            <div className="stream-status"><span>●</span> schema checked · linkage valid · ready to resume</div>
          </div>
        </div>
      </section>

      <section className="section shell" id="compatibility">
        <div className="section-heading horizontal">
          <div><p>COMPATIBILITY</p><h2>Pick the next agent.</h2></div>
          <span>Every format can be read and written. Cursor support is experimental, version-pinned, and text-only.</span>
        </div>
        <div className="matrix-wrap">
          <table className="matrix">
            <thead><tr><th>Source ↓ / Target →</th>{agents.map(agent => <th key={agent}>{agent}</th>)}</tr></thead>
            <tbody>{compatibility.map(row => (
              <tr key={row[0]}>{row.map((cell, index) => <td key={cell + index} className={cell === "✓" ? "yes" : cell === "T" ? "text" : ""}>{cell}</td>)}</tr>
            ))}</tbody>
          </table>
        </div>
        <p className="matrix-note">✓ portable history · T experimental text-only · diagonal routes are portable rewrites, not byte copies</p>
      </section>

      <section className="feature-grid shell">
        <article>
          <span>01</span><h3>Keep the useful history</h3>
          <p>Messages, tools, results, images, order, and summaries move when both adapters support them.</p>
        </article>
        <article>
          <span>02</span><h3>Find every session</h3>
          <p>Index every recognized session under configured and discovered roots, then search by title or ID.</p>
        </article>
        <article>
          <span>03</span><h3>Know what changed</h3>
          <p>Every unsupported or transformed detail is counted in a content-free manifest.</p>
        </article>
      </section>

      <section className="install section shell" id="install">
        <div>
          <p>INSTALL</p>
          <h2>One command.<br />Then keep moving.</h2>
        </div>
        <div className="install-commands">
          <CopyCommand command="uv tool install session-migrate" />
          <span>or, without uv</span>
          <CopyCommand command="curl -LsSf https://raw.githubusercontent.com/xhluca/session-migrate/main/install.sh | sh" />
          <small>Python 3.11+ · Linux · MIT licensed</small>
        </div>
      </section>

      <footer className="footer shell">
        <a className="brand" href="#top"><span className="brand-mark">↝</span><span>session-migrate</span></a>
        <p>Carry the conversation forward.</p>
        <div><a href="https://github.com/xhluca/session-migrate">GitHub</a><a href="https://pypi.org/project/session-migrate/">PyPI</a><a href="https://github.com/xhluca/session-migrate/tree/main/docs">Docs</a></div>
      </footer>
    </main>
  );
}
