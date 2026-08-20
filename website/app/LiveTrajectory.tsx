"use client";

import Image from "next/image";
import { useEffect, useMemo, useRef, useState } from "react";

type Target = "pi" | "codex";

type TrajectoryLine = {
  at: number;
  kind: "command" | "meta" | "history" | "success";
  prefix: string;
  text: string;
  typed?: boolean;
};

const targetDetails = {
  pi: {
    label: "Pi",
    native: "native v3 JSONL",
    resume: "pi --session 20000000-…",
    reply: "3 tests passed · patch applied in Pi",
    duration: 80_280,
    screenshot: "/demo-after-pi.png",
    screenshotAlt: "Pi native TUI after migration from Claude Code",
  },
  codex: {
    label: "Codex",
    native: "native rollout JSONL",
    resume: "codex resume 30000000-…",
    reply: "3 tests passed · patch applied in Codex",
    duration: 91_320,
    screenshot: "/demo-after-codex.png",
    screenshotAlt: "Codex native TUI after migration from Claude Code",
  },
} as const;

function linesFor(target: Target): TrajectoryLine[] {
  const label = targetDetails[target].label;
  const finishAt = target === "pi" ? 74_000 : 85_000;
  return [
    { at: 250, kind: "meta", prefix: "CLAUDE", text: "native session · timeline project loaded" },
    { at: 900, kind: "command", prefix: "YOU", text: "Keep gap_ms=0 backward compatible. Propose the smallest patch and one regression test that separates touching events from a real 1 ms gap.", typed: true },
    { at: 9_000, kind: "meta", prefix: "CLAUDE", text: "reviews timeline.py and the focused tests…" },
    { at: 17_000, kind: "history", prefix: "CLAUDE", text: "Boundary diagnosis: gap < gap_ms excludes touching events when gap_ms is zero." },
    { at: 34_000, kind: "history", prefix: "CLAUDE", text: "Smallest patch: change < to <=; test gap 0 against a real 1 ms gap." },
    { at: 43_000, kind: "command", prefix: "❯", text: `smigrate transfer 10000000-… --from claude --to ${target}`, typed: true },
    { at: 47_500, kind: "success", prefix: "✓", text: `native ${label} session created · source unchanged` },
    { at: 50_000, kind: "meta", prefix: label.toUpperCase(), text: "migrated history opened in the native TUI" },
    { at: 51_500, kind: "history", prefix: "YOU", text: "Keep gap_ms=0 backward compatible…" },
    { at: 53_000, kind: "history", prefix: "CLAUDE", text: "Change < to <= and add a touching-vs-1ms regression test." },
    { at: 55_000, kind: "command", prefix: "YOU", text: `Continue in ${label}: implement the patch, add the regression test, and run the focused suite.`, typed: true },
    { at: 64_000, kind: "meta", prefix: label.toUpperCase(), text: "reads timeline.py · applies one-line fix · adds regression" },
    { at: finishAt, kind: "success", prefix: label.toUpperCase(), text: targetDetails[target].reply },
    { at: targetDetails[target].duration - 2_500, kind: "success", prefix: "RESUME", text: targetDetails[target].resume },
  ];
}

function phaseFor(elapsed: number, duration: number) {
  if (elapsed < 43_000) return "claude";
  if (elapsed < 50_000) return "migrate";
  if (elapsed < 55_000) return "review";
  if (elapsed < duration - 4_500) return "continue";
  return "ready";
}

export function LiveTrajectory() {
  const frameRef = useRef<HTMLElement>(null);
  const [elapsed, setElapsed] = useState(0);
  const [paused, setPaused] = useState(false);
  const [reducedMotion, setReducedMotion] = useState(false);
  const [inView, setInView] = useState(false);
  const [target, setTarget] = useState<Target>("pi");
  const targetDetail = targetDetails[target];
  const duration = targetDetail.duration;

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const sync = () => {
      setReducedMotion(query.matches);
      if (query.matches) setElapsed(duration - 1);
    };
    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, [duration]);

  useEffect(() => {
    const frame = frameRef.current;
    if (!frame || !("IntersectionObserver" in window)) {
      setInView(true);
      return;
    }
    const observer = new IntersectionObserver(
      ([entry]) => setInView(entry.isIntersecting),
      { threshold: 0.25 },
    );
    observer.observe(frame);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (paused || reducedMotion || !inView) return;
    const timer = window.setInterval(() => {
      setElapsed((value) => (value + 50) % duration);
    }, 50);
    return () => window.clearInterval(timer);
  }, [duration, inView, paused, reducedMotion]);

  const phase = phaseFor(elapsed, duration);
  const progress = Math.min(100, (elapsed / duration) * 100);
  const renderedLines = useMemo(
    () => linesFor(target).map((line) => {
      const visible = reducedMotion || elapsed >= line.at;
      const typedLength = line.typed
        ? Math.max(0, Math.floor((elapsed - line.at) / 45))
        : line.text.length;
      return {
        ...line,
        visible,
        renderedText: reducedMotion ? line.text : line.text.slice(0, typedLength),
      };
    }),
    [elapsed, reducedMotion, target],
  );

  const selectTarget = (value: Target) => {
    setTarget(value);
    setReducedMotion(false);
    setElapsed(0);
    setPaused(false);
  };

  const toggle = () => {
    if (reducedMotion) {
      setReducedMotion(false);
      setElapsed(0);
      setPaused(false);
      return;
    }
    setPaused((value) => !value);
  };

  return (
    <>
      <div className="demo-target-toggle" role="group" aria-label="Choose demo target">
        {(["pi", "codex"] as const).map((value) => (
          <button
            type="button"
            className={target === value ? "is-active" : ""}
            aria-pressed={target === value}
            onClick={() => selectTarget(value)}
            key={value}
          >
            Claude → {targetDetails[value].label}
          </button>
        ))}
      </div>

      <figure className="trajectory-frame" data-phase={phase} ref={frameRef}>
        <div className="trajectory-topbar">
          <div className="trajectory-label"><i /> LIVE NATIVE HANDOFF · 1×</div>
          <div className="trajectory-controls">
            <span>{phase}</span>
            <button type="button" onClick={toggle} aria-label={paused ? "Resume trajectory animation" : "Pause trajectory animation"}>
              {reducedMotion ? "Play" : paused ? "Resume" : "Pause"}
            </button>
            <button type="button" onClick={() => { setReducedMotion(false); setElapsed(0); setPaused(false); }} aria-label="Replay trajectory animation">
              Replay
            </button>
          </div>
        </div>

        <div className="trajectory-route" aria-hidden="true">
          <div className="trajectory-agent source-agent">
            <span>01 · BEFORE</span><strong>Claude Code</strong><em>native JSONL</em>
          </div>
          <div className="trajectory-rail"><i /><span>portable events</span></div>
          <div className="trajectory-agent target-agent">
            <span>02 · AFTER</span><strong>{targetDetail.label}</strong><em>{targetDetail.native}</em>
          </div>
        </div>

        <div className="trajectory-terminal">
          <div className="trajectory-terminal-bar">
            <div className="window-dots"><i /><i /><i /></div>
            <span>{phase === "claude" ? "Claude Code" : phase === "migrate" ? "session-migrate" : targetDetail.label}</span>
            <b>{phase === "ready" ? "continued" : "live · 1×"}</b>
          </div>
          <div className="trajectory-screen" aria-hidden="true">
            {renderedLines.map((line) => (
              <div className={`trajectory-line ${line.kind} ${line.visible ? "is-visible" : ""}`} key={`${target}-${line.at}-${line.prefix}`}>
                <span>{line.prefix}</span>
                <code>{line.renderedText}</code>
                {line.typed && line.visible && line.renderedText.length < line.text.length && <i className="trajectory-cursor" />}
              </div>
            ))}
          </div>
        </div>

        <div className="trajectory-progress" aria-hidden="true"><i style={{ width: `${progress}%` }} /></div>
        <figcaption>
          <span><i /> real-time typing, history review, and continuation</span>
          <em>loops in {Math.round(duration / 1000)} seconds</em>
        </figcaption>
        <p className="sr-only">
          Animated native-session handoff from Claude Code to {targetDetail.label}:
          the user types in Claude, session-migrate creates the target session,
          the imported history is reviewed, and the user continues inside the
          target client at real-time speed.
        </p>
      </figure>

      <details className="snapshots">
        <summary><span>Compare the native sessions</span><em>Actual TUI screenshots</em></summary>
        <div className="snapshot-grid">
          <figure>
            <Image src="/demo-before.png" width={845} height={704} alt="Claude Code native TUI before migration" />
            <figcaption>Before · Claude Code TUI</figcaption>
          </figure>
          <figure>
            <Image src={targetDetail.screenshot} width={845} height={704} alt={targetDetail.screenshotAlt} />
            <figcaption>After · {targetDetail.label} TUI</figcaption>
          </figure>
        </div>
      </details>
    </>
  );
}
