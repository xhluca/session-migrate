"use client";

import { useEffect, useMemo, useRef, useState } from "react";

const DURATION = 17_000;

type TrajectoryLine = {
  at: number;
  kind: "command" | "meta" | "map" | "success";
  prefix: string;
  text: string;
  typed?: boolean;
};

const lines: TrajectoryLine[] = [
  { at: 250, kind: "command", prefix: "❯", text: "smigrate transfer c3f7… --from claude --to codex --dry-run", typed: true },
  { at: 2_050, kind: "meta", prefix: "catalog", text: "1 matching Claude session · c3f7…" },
  { at: 2_750, kind: "meta", prefix: "source", text: "42 messages · 7 tools · 1 image · linked" },
  { at: 3_500, kind: "success", prefix: "dry-run", text: "no message loss · target path clear" },
  { at: 4_550, kind: "command", prefix: "❯", text: "smigrate transfer c3f7… --from claude --to codex", typed: true },
  { at: 6_300, kind: "map", prefix: "map", text: "user.message       → response_item" },
  { at: 7_050, kind: "map", prefix: "map", text: "tool_use          → function_call" },
  { at: 7_800, kind: "map", prefix: "map", text: "tool_result       → function_call_output" },
  { at: 8_550, kind: "map", prefix: "map", text: "compact_summary   → compacted" },
  { at: 9_650, kind: "meta", prefix: "write", text: "native Codex rollout · 15 records · manifest saved" },
  { at: 10_650, kind: "success", prefix: "verify", text: "source hash unchanged · tool linkage valid" },
  { at: 11_650, kind: "success", prefix: "✓", text: "codex resume c3f7…" },
];

function phaseFor(elapsed: number) {
  if (elapsed < 2_050) return "command";
  if (elapsed < 4_550) return "inspect";
  if (elapsed < 9_650) return "mapping";
  if (elapsed < 11_650) return "writing";
  return "ready";
}

export function LiveTrajectory() {
  const frameRef = useRef<HTMLElement>(null);
  const [elapsed, setElapsed] = useState(0);
  const [paused, setPaused] = useState(false);
  const [reducedMotion, setReducedMotion] = useState(false);
  const [inView, setInView] = useState(false);

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const sync = () => {
      setReducedMotion(query.matches);
      if (query.matches) setElapsed(DURATION - 1);
    };
    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, []);

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
      setElapsed((value) => (value + 50) % DURATION);
    }, 50);
    return () => window.clearInterval(timer);
  }, [inView, paused, reducedMotion]);

  const phase = phaseFor(elapsed);
  const progress = Math.min(100, (elapsed / DURATION) * 100);
  const renderedLines = useMemo(
    () => lines.map((line) => {
      const visible = reducedMotion || elapsed >= line.at;
      const typedLength = line.typed
        ? Math.max(0, Math.floor((elapsed - line.at) / 22))
        : line.text.length;
      return {
        ...line,
        visible,
        renderedText: reducedMotion ? line.text : line.text.slice(0, typedLength),
      };
    }),
    [elapsed, reducedMotion],
  );

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
    <figure className="trajectory-frame" data-phase={phase} ref={frameRef}>
      <div className="trajectory-topbar">
        <div className="trajectory-label"><i /> LIVE TRAJECTORY</div>
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
          <span>01 · SOURCE</span><strong>Claude Code</strong><em>native JSONL</em>
        </div>
        <div className="trajectory-rail"><i /><span>portable events</span></div>
        <div className="trajectory-agent target-agent">
          <span>02 · TARGET</span><strong>Codex</strong><em>native rollout</em>
        </div>
      </div>

      <div className="trajectory-terminal">
        <div className="trajectory-terminal-bar">
          <div className="window-dots"><i /><i /><i /></div>
          <span>~/project · session-migrate 0.6.2</span>
          <b>{phase === "ready" ? "complete" : "running"}</b>
        </div>
        <div className="trajectory-screen" aria-hidden="true">
          {renderedLines.map((line) => (
            <div className={`trajectory-line ${line.kind} ${line.visible ? "is-visible" : ""}`} key={`${line.at}-${line.prefix}`}>
              <span>{line.prefix}</span>
              <code>{line.renderedText}</code>
              {line.typed && line.visible && line.renderedText.length < line.text.length && <i className="trajectory-cursor" />}
            </div>
          ))}
        </div>
      </div>

      <div className="trajectory-progress" aria-hidden="true"><i style={{ width: `${progress}%` }} /></div>
      <figcaption>
        <span><i /> executable text, not a video</span>
        <em>loops in 17 seconds</em>
      </figcaption>
      <p className="sr-only">
        Animated terminal demonstration: session-migrate finds a Claude session,
        dry-runs the conversion, maps messages and linked tools, writes a native
        Codex rollout and manifest, verifies the unchanged source hash, then prints
        the Codex resume command.
      </p>
    </figure>
  );
}
