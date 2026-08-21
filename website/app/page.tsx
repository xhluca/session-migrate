import Image from "next/image";
import { CopyCommand } from "./CopyCommand";
import { CopyPrompt } from "./CopyPrompt";
import { LiveTrajectory } from "./LiveTrajectory";

const agents = ["Claude", "Codex", "Pi", "OpenCode", "Copilot", "Antigravity", "Vibe", "Cursor*"];
const capabilities = [
  ["Claude Code", "Full adapter"],
  ["Codex", "Full adapter"],
  ["Pi", "Full adapter"],
  ["OpenCode", "Full adapter"],
  ["Copilot", "Full adapter"],
  ["Antigravity", "Full adapter"],
  ["Mistral Vibe", "Full adapter · 2.24.3"],
  ["Cursor", "Text only · experimental"],
] as const;

function BrandMark({ hero = false }: { hero?: boolean }) {
  return (
    <Image
      className={hero ? "hero-logo" : "brand-mark"}
      src="/logo-mark.svg"
      width={hero ? 60 : 32}
      height={hero ? 60 : 32}
      alt=""
      aria-hidden="true"
    />
  );
}

export default function Home() {
  return (
    <main>
      <nav className="nav shell" aria-label="Main navigation">
        <a className="brand" href="#top" aria-label="session-migrate home">
          <BrandMark />
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
        <BrandMark hero />
        <div className="eyebrow"><span /> Local sessions, set free</div>
        <h1>Switch agents.<br />Keep your context.</h1>
        <p className="hero-copy">
          Move coding agent sessions among Claude Code, Codex, Pi, OpenCode,
          Copilot, Antigravity, Mistral Vibe, and Cursor—then resume where you left off.
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
            Pick a source and target, copy the sentence, then replace its final
            placeholder with the session UUID or title.
          </span>
        </div>
        <CopyPrompt />
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
            Watch the actual Claude Code and target TUIs. The migration command
            runs between them, then the same conversation continues.
          </span>
        </div>
        <LiveTrajectory />
      </section>

      <section className="workflow section" id="how-it-works">
        <div className="shell workflow-grid">
          <div className="section-heading compact">
            <p>HOW IT WORKS</p>
            <h2>Read. Convert.<br />Resume.</h2>
            <span>
              session-migrate reads the source agent&apos;s session, converts the
              history both agents understand, and writes a native session the
              target agent can resume.
            </span>
          </div>
          <div className="stream-card" aria-label="Animated migration pipeline">
            <div className="pipeline-step"><b>01</b><div><span>Read the source</span><p>Open the agent&apos;s native session without changing it.</p></div></div>
            <i className="pipeline-arrow" aria-hidden="true" />
            <div className="pipeline-step"><b>02</b><div><span>Convert the history</span><p>Preserve supported messages, tools, images, and order.</p></div></div>
            <i className="pipeline-arrow" aria-hidden="true" />
            <div className="pipeline-step"><b>03</b><div><span>Write the target</span><p>Create a native session the next agent can resume.</p></div></div>
            <div className="stream-status"><span>●</span> source unchanged · ready to resume</div>
          </div>
        </div>
      </section>

      <section className="section shell" id="compatibility">
        <div className="section-heading horizontal">
          <div><p>COMPATIBILITY</p><h2>Pick the next agent.</h2></div>
          <span>Move between any two listed agents. A same-agent move creates a fresh native session instead of copying bytes.</span>
        </div>
        <div className="capability-list" aria-label="Supported coding agents">
          {capabilities.map(([name, detail]) => (
            <article className={name === "Cursor" ? "experimental" : name === "Mistral Vibe" ? "vibe" : ""} key={name}>
              <i aria-hidden="true" /><div><h3>{name}</h3><p>{detail}</p></div>
            </article>
          ))}
        </div>
        <p className="capability-note">All 64 source → target routes are available. Cursor is version-pinned and experimental.</p>
      </section>

      <section className="feature-grid shell">
        <article>
          <span>01</span><h3>Keep the useful history</h3>
          <p>Messages, tools, results, images, order, and summaries move when both adapters support them.</p>
        </article>
        <article>
          <span>02</span><h3>Find every session</h3>
          <p>Search native titles and names with order-independent keywords like <code>oauth refresh</code>, then select the exact result.</p>
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
          <CopyCommand command="curl -LsSf https://session-migrate.github.io/install.sh | sh" />
          <small>Python 3.11+ · Linux · MIT licensed</small>
        </div>
      </section>

      <footer className="footer shell">
        <a className="brand" href="#top"><BrandMark /><span>session-migrate</span></a>
        <p>Migrate your sessions to any harness.</p>
        <div><a href="https://github.com/xhluca/session-migrate">GitHub</a><a href="https://pypi.org/project/session-migrate/">PyPI</a><a href="https://github.com/xhluca/session-migrate/tree/main/docs">Docs</a></div>
      </footer>
    </main>
  );
}
