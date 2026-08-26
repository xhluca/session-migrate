import Image from "next/image";
import { CopyCommand } from "./CopyCommand";
import { CopyPrompt } from "./CopyPrompt";
import { HeroTerminal } from "./HeroTerminal";
import { LiveTrajectory } from "./LiveTrajectory";

const agents = ["Claude", "Codex", "Pi", "Oh My Pi", "OpenCode", "Copilot", "Antigravity", "Vibe", "Muse", "Qwen", "Kimi", "Grok", "Kilo", "OpenHands", "Cursor*"];
const capabilities = [
  ["Claude Code", "Full adapter", "claude-code", "https://github.com/anthropics/claude-code"],
  ["Codex", "Full adapter", "codex", "https://github.com/openai/codex"],
  ["Pi", "Full adapter", "pi", "https://pi.dev"],
  ["Oh My Pi", "Full adapter · 18.0.5", "oh-my-pi", "https://github.com/can1357/oh-my-pi"],
  ["OpenCode", "Full adapter", "opencode", "https://github.com/anomalyco/opencode"],
  ["Copilot", "Full adapter", "copilot", "https://github.com/github/copilot-cli"],
  ["Antigravity", "Full adapter", "antigravity", "https://developers.google.com/antigravity"],
  ["Mistral Vibe", "Full adapter · 2.24.3", "mistral-vibe", "https://github.com/mistralai/mistral-vibe"],
  ["Muse Code", "Full adapter · 0.2.1", "muse", "https://dev.meta.ai/"],
  ["Qwen Code", "Full adapter · 0.22.1", "qwen-code", "https://github.com/QwenLM/qwen-code"],
  ["Kimi Code", "Full adapter · 0.38.0", "kimi-code", "https://github.com/MoonshotAI/kimi-cli"],
  ["Grok", "Full adapter · 1.0.5", "grok", "https://github.com/xai-org/grok-build"],
  ["Kilo Code", "Full adapter · 7.5.0", "kilo-code", "https://github.com/Kilo-Org/kilocode"],
  ["OpenHands", "Full adapter · 1.16.0", "openhands", "https://github.com/All-Hands-AI/OpenHands"],
  ["Cursor", "Text only · experimental", "cursor", "https://cursor.com/cli"],
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
          Move coding agent sessions among Claude Code, Codex, Pi, Oh My Pi, OpenCode,
          Copilot, Antigravity, Mistral Vibe, Muse, Qwen, Kimi, Grok,
          Kilo Code, OpenHands, and Cursor—then resume where you left off.
        </p>
        <div className="hero-actions">
          <CopyCommand command="curl -LsSf https://session-migrate.github.io/install.sh | sh" />
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

        <HeroTerminal />
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

      <section className="section shell" id="compatibility">
        <div className="section-heading horizontal">
          <div><p>COMPATIBILITY</p><h2>Pick the next agent.</h2></div>
          <span>Move between any two listed agents. A same-agent move creates a fresh native session instead of copying bytes.</span>
        </div>
        <div className="capability-list" aria-label="Supported coding agents">
          {capabilities.map(([name, detail, icon, href]) => (
            <a className={name === "Cursor" ? "experimental" : name === "Mistral Vibe" ? "vibe" : ""} href={href} key={name}>
              <Image unoptimized src={`https://session-migrate.github.io/agents/${icon}.svg`} width={42} height={42} alt="" />
              <div><h3>{name}</h3><p>{detail}</p></div>
            </a>
          ))}
        </div>
        <p className="capability-note">All 225 source → target routes are available. Cursor is version-pinned and experimental.</p>
      </section>

      <section className="install section shell" id="install">
        <div>
          <p>INSTALL</p>
          <h2>One command.<br />Then keep moving.</h2>
        </div>
        <div className="install-commands">
          <div className="install-choice"><span>Standalone installer</span><CopyCommand command="curl -LsSf https://session-migrate.github.io/install.sh | sh" /></div>
          <div className="install-choice"><span>With uv</span><CopyCommand command="uv tool install session-migrate" /></div>
          <div className="install-choice"><span>Give it to a coding agent</span><CopyCommand prefix=">" command="Follow https://session-migrate.github.io/llms.txt to migrate my session." /></div>
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
