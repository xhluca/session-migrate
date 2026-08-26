"use client";

import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";

type Target = "pi" | "codex";
type Phase = "source" | "pullback" | "convert" | "launch" | "overlap" | "target";

type CastPlayer = {
  play: () => void;
  pause: () => void;
  seek: (time: number) => void;
  dispose?: () => void;
};

declare global {
  interface Window {
    AsciinemaPlayer?: {
      create: (source: string, mount: HTMLElement, options: Record<string, unknown>) => CastPlayer;
    };
    __sessionMigrateDemo?: {
      setTime: (seconds: number) => void;
      play: () => void;
      pause: () => void;
    };
  }
}

const DURATION = 53;
const SOURCE_STOP = 8.55;
const SOURCE_SPEED = 4.6;
const SOURCE_POSTER = 0.1;
const LIMIT_START = 8.7;
const LIMIT_END = 36;
const TARGET_START = 28;
const HIGHLIGHT_START = 30;
const SESSION_TITLE = "fix-timeline-merging";
const SHARED_HISTORY_START = 'So I read "backward compatible"';
const SHARED_HISTORY_END = "two distinguishable cases.";

const targetDetails = {
  pi: {
    label: "Pi",
    cast: "/demo-pi.cast",
    launch: `Continue “${SESSION_TITLE}” in Pi`,
    compareAt: 20,
  },
  codex: {
    label: "Codex",
    cast: "/demo-codex.cast",
    launch: `Continue “${SESSION_TITLE}” in Codex`,
    compareAt: 26,
  },
} as const;

function phaseAt(time: number): Phase {
  if (time < 11.7) return "source";
  if (time < 15) return "pullback";
  if (time < 27.5) return "convert";
  if (time < 30.5) return "launch";
  if (time < 36) return "overlap";
  return "target";
}

const CLAUDE_LINE_CLASSES = [
  "claude-prompt-line",
  "claude-tool-line",
  "claude-thinking-line",
  "claude-tip-line",
  "claude-status-line",
] as const;

function decorateClaudeTerminal(mount: HTMLElement | null) {
  if (!mount) return;
  const lines = Array.from(mount.querySelectorAll<HTMLElement>(".ap-line"));
  let promptContinuation = -1;

  lines.forEach((line, index) => {
    line.classList.remove(...CLAUDE_LINE_CLASSES);
    const text = (line.textContent ?? "").replace(/\s+/g, " ").trim();
    if (text.includes("Keep gap_ms=0")) {
      line.classList.add("claude-prompt-line");
      promptContinuation = index + 1;
    } else if (index === promptContinuation && text) {
      line.classList.add("claude-prompt-line");
    }
    if (/^(●|⎿)|Bash\(|Running…|ctrl\+o to expand/.test(text)) {
      line.classList.add("claude-tool-line");
    }
    if (/Architecting…|Baked for/.test(text)) {
      line.classList.add("claude-thinking-line");
    }
    if (/Tip:|\/config/.test(text)) {
      line.classList.add("claude-tip-line");
    }
    if (/bypass permissions|esc to interrupt|for agents/.test(text)) {
      line.classList.add("claude-status-line");
    }

    line.querySelectorAll<HTMLElement>("span").forEach((span) => {
      span.classList.remove("claude-accent", "claude-command", "claude-muted");
      const spanText = span.textContent ?? "";
      if (/●/.test(spanText)) span.classList.add("claude-accent");
      if (/\/config|Bash/.test(spanText)) span.classList.add("claude-command");
      if (/Running|Tip:|ctrl\+o|bypass|permissions|interrupt/.test(spanText)) {
        span.classList.add("claude-muted");
      }
    });
  });
}

function syncClaudeLimit(mount: HTMLElement | null, visible: boolean) {
  if (!mount) return;
  const existing = mount.querySelector<HTMLElement>(".claude-limit-injection");
  if (!visible) {
    existing?.remove();
    return;
  }
  const terminal = mount.querySelector<HTMLElement>(".ap-term");
  if (!terminal || existing) return;

  const injection = document.createElement("div");
  injection.className = "claude-limit-injection";
  injection.setAttribute("role", "status");
  injection.setAttribute("aria-live", "polite");

  const firstGutter = document.createElement("span");
  firstGutter.className = "claude-limit-gutter";
  firstGutter.textContent = "⎿";
  const message = document.createElement("strong");
  message.textContent = "You've hit your limit · resets 3pm (America/Montreal)";
  const secondGutter = document.createElement("span");
  secondGutter.className = "claude-limit-gutter";
  const suggestion = document.createElement("span");
  suggestion.className = "claude-limit-suggestion";
  suggestion.textContent = "/upgrade to increase your usage limit.";
  injection.append(firstGutter, message, secondGutter, suggestion);
  terminal.append(injection);
}

function CompareCast({
  label,
  cast,
  posterAt,
}: {
  label: string;
  cast: string;
  posterAt: number;
}) {
  const mount = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    let retries = 0;
    let player: CastPlayer | null = null;
    let claudeObserver: MutationObserver | null = null;
    const createPlayer = () => {
      if (cancelled || !mount.current) return;
      if (!window.AsciinemaPlayer) {
        if (retries++ < 100) window.setTimeout(createPlayer, 50);
        return;
      }
      mount.current.replaceChildren();
      player = window.AsciinemaPlayer.create(cast, mount.current, {
        autoPlay: false,
        controls: true,
        fit: "width",
        idleTimeLimit: 2,
        loop: false,
        poster: `npt:${posterAt}`,
        theme: "asciinema",
        terminalFontFamily: "Geist Mono, monospace",
        terminalLineHeight: 1.38,
      });
      if (label === "Claude Code") {
        claudeObserver = new MutationObserver(() => decorateClaudeTerminal(mount.current));
        claudeObserver.observe(mount.current, { childList: true, characterData: true, subtree: true });
        requestAnimationFrame(() => decorateClaudeTerminal(mount.current));
      }
    };
    createPlayer();
    return () => {
      cancelled = true;
      claudeObserver?.disconnect();
      player?.dispose?.();
    };
  }, [cast, label, posterAt]);

  return (
    <figure className={`compare-terminal ${label === "Claude Code" ? "claude-terminal" : ""}`}>
      <div className="native-window-bar">
        <span className="native-window-dots"><i /><i /><i /></span>
        <b>{label}</b>
        <em>native session</em>
      </div>
      <div
        className="cast-mount compare-cast"
        data-compare-cast={cast}
        aria-label={`${label} native terminal recording`}
        ref={mount}
      />
      <figcaption>{label === "Claude Code" ? "Before" : "After"} · {label} TUI</figcaption>
    </figure>
  );
}

function typed(text: string, progress: number) {
  return text.slice(0, Math.floor(text.length * Math.max(0, Math.min(1, progress))));
}

function anchorSharedHistory(mount: HTMLElement | null) {
  if (!mount) return false;
  const windowElement = mount.closest<HTMLElement>(".native-window");
  const marker = windowElement?.querySelector<HTMLElement>(".history-marker");
  if (!windowElement || !marker) return false;

  const lines = Array.from(mount.querySelectorAll<HTMLElement>(".ap-line"));
  const startIndex = lines.findIndex((line) => line.textContent?.includes(SHARED_HISTORY_START));
  let endIndex = -1;
  if (startIndex >= 0) {
    for (let index = startIndex; index < lines.length; index += 1) {
      const sharedText = lines
        .slice(startIndex, index + 1)
        .map((line) => line.textContent ?? "")
        .join(" ")
        .replace(/\s+/g, " ");
      if (sharedText.includes(SHARED_HISTORY_END)) {
        endIndex = index;
        break;
      }
    }
  }
  if (startIndex < 0 || endIndex < startIndex) {
    marker.dataset.anchored = "false";
    return false;
  }

  const windowRect = windowElement.getBoundingClientRect();
  const startRect = lines[startIndex].getBoundingClientRect();
  const endRect = lines[endIndex].getBoundingClientRect();
  marker.style.top = `${Math.max(0, startRect.top - windowRect.top - 5)}px`;
  marker.style.height = `${endRect.bottom - startRect.top + 10}px`;
  marker.dataset.anchored = "true";
  return true;
}

function CastWindow({
  label,
  mountRef,
  kind,
  cast,
}: {
  label: string;
  mountRef: React.RefObject<HTMLDivElement | null>;
  kind: "source" | "target";
  cast: string;
}) {
  return (
    <div className={`native-window native-window-${kind}`}>
      <div className="native-window-bar">
        <span className="native-window-dots"><i /><i /><i /></span>
        <b>{label}</b>
        <em>{kind === "source" ? "source session" : "resumed session"}</em>
      </div>
      <div className="cast-mount" data-cast-src={cast} ref={mountRef} />
      <div className="history-marker" data-anchored="false" aria-hidden="true"><span>shared history</span></div>
    </div>
  );
}

export function LiveTrajectory() {
  const sourceMount = useRef<HTMLDivElement>(null);
  const targetMount = useRef<HTMLDivElement>(null);
  const sourcePlayer = useRef<CastPlayer | null>(null);
  const targetPlayer = useRef<CastPlayer | null>(null);
  const elapsedRef = useRef(0);
  const playingRef = useRef(true);
  const visibleRef = useRef(true);
  const frameRef = useRef(0);
  const lastFrameRef = useRef(0);
  const sourceSettledRef = useRef(false);
  const [target, setTarget] = useState<Target>("pi");
  const [playing, setPlaying] = useState(true);
  const [elapsed, setElapsed] = useState(0);
  const [playersReady, setPlayersReady] = useState(false);
  const [compareOpen, setCompareOpen] = useState(false);
  const targetDetail = targetDetails[target];
  const phase = phaseAt(elapsed);
  const showContextLimit = elapsed >= LIMIT_START && elapsed < LIMIT_END;

  const migrationCommand = useMemo(
    () => `smigrate transfer --title ${SESSION_TITLE} --from claude --to ${target}`,
    [target],
  );
  const commandText = typed(migrationCommand, (elapsed - 15.5) / 6);
  const showScan = elapsed >= 22;
  const showWrite = elapsed >= 23.3;
  const showDone = elapsed >= 24.5;

  const mountSourcePlayer = useCallback((settled: boolean) => {
    if (!window.AsciinemaPlayer || !sourceMount.current) return;
    sourcePlayer.current?.dispose?.();
    sourceMount.current.replaceChildren();
    sourcePlayer.current = window.AsciinemaPlayer.create(
      settled ? "/demo-claude-hold.cast" : "/demo-claude.cast",
      sourceMount.current,
      {
        autoPlay: false,
        controls: false,
        fit: "both",
        idleTimeLimit: 2,
        loop: false,
        poster: settled ? `npt:${SOURCE_POSTER}` : undefined,
        speed: settled ? 1 : SOURCE_SPEED,
        theme: "asciinema",
        terminalFontFamily: "Geist Mono, monospace",
        terminalLineHeight: 1.14,
      },
    );
    sourcePlayer.current.pause();
  }, []);

  const pausePlayers = useCallback(() => {
    sourcePlayer.current?.pause();
    targetPlayer.current?.pause();
  }, []);

  const syncPlayers = useCallback((time: number, shouldPlay: boolean) => {
    if (!sourcePlayer.current || !targetPlayer.current) return;
    if (time < SOURCE_STOP) {
      if (sourceSettledRef.current) {
        sourceSettledRef.current = false;
        mountSourcePlayer(false);
      }
      targetPlayer.current.pause();
      sourcePlayer.current.pause();
      sourcePlayer.current.seek(time * SOURCE_SPEED);
    } else if (time < TARGET_START) {
      if (!sourceSettledRef.current) {
        sourceSettledRef.current = true;
        mountSourcePlayer(true);
      }
      sourcePlayer.current.pause();
      targetPlayer.current.pause();
    } else {
      if (!sourceSettledRef.current) {
        sourceSettledRef.current = true;
        mountSourcePlayer(true);
      }
      sourcePlayer.current.pause();
      if (shouldPlay) targetPlayer.current.play();
      else targetPlayer.current.pause();
    }
  }, [mountSourcePlayer]);

  const setTime = useCallback((time: number) => {
    const next = Math.max(0, Math.min(DURATION, time));
    elapsedRef.current = next;
    setElapsed(next);
    targetPlayer.current?.seek(Math.max(0, next - TARGET_START));
    syncPlayers(next, playingRef.current && visibleRef.current);
  }, [syncPlayers]);

  const replay = useCallback(() => {
    targetPlayer.current?.seek(0);
    elapsedRef.current = 0;
    lastFrameRef.current = performance.now();
    playingRef.current = true;
    setPlaying(true);
    setElapsed(0);
    syncPlayers(0, visibleRef.current);
  }, [syncPlayers]);

  const toggle = useCallback(() => {
    const next = !playingRef.current;
    playingRef.current = next;
    setPlaying(next);
    lastFrameRef.current = performance.now();
    syncPlayers(elapsedRef.current, next && visibleRef.current);
  }, [syncPlayers]);

  const pauseForSeek = useCallback(() => {
    playingRef.current = false;
    setPlaying(false);
    pausePlayers();
  }, [pausePlayers]);

  useEffect(() => {
    let cancelled = false;
    let retries = 0;

    const mount = () => {
      if (cancelled || !sourceMount.current || !targetMount.current) return;
      if (!window.AsciinemaPlayer) {
        if (retries++ < 100) window.setTimeout(mount, 50);
        return;
      }

      sourcePlayer.current?.dispose?.();
      targetPlayer.current?.dispose?.();
      const grid = sourceMount.current.closest<HTMLElement>(".handoff-grid");
      if (grid) grid.dataset.historyAligned = "false";
      sourceMount.current.replaceChildren();
      targetMount.current.replaceChildren();
      const common = {
        autoPlay: false,
        controls: false,
        fit: "both",
        idleTimeLimit: 2,
        loop: false,
        theme: "asciinema",
        terminalFontFamily: "Geist Mono, monospace",
        terminalLineHeight: 1.14,
      };
      mountSourcePlayer(false);
      targetPlayer.current = window.AsciinemaPlayer.create(
        targetDetail.cast,
        targetMount.current,
        { ...common, speed: 1 },
      );
      sourcePlayer.current.seek(0);
      targetPlayer.current.seek(0);
      sourceSettledRef.current = false;
      setPlayersReady(true);
      syncPlayers(elapsedRef.current, playingRef.current && visibleRef.current);
    };

    mount();
    return () => {
      cancelled = true;
      sourcePlayer.current?.dispose?.();
      targetPlayer.current?.dispose?.();
      sourcePlayer.current = null;
      targetPlayer.current = null;
    };
  }, [mountSourcePlayer, syncPlayers, targetDetail.cast]);

  useEffect(() => {
    if (!playersReady || !sourceMount.current) return;
    const mount = sourceMount.current;
    let queued = 0;
    const sync = () => {
      queued = 0;
      decorateClaudeTerminal(mount);
      syncClaudeLimit(mount, showContextLimit);
    };
    const queueSync = () => {
      if (!queued) queued = requestAnimationFrame(sync);
    };
    const observer = new MutationObserver(queueSync);
    observer.observe(mount, { childList: true, characterData: true, subtree: true });
    sync();
    return () => {
      observer.disconnect();
      if (queued) cancelAnimationFrame(queued);
      mount.querySelector(".claude-limit-injection")?.remove();
    };
  }, [playersReady, showContextLimit]);

  useEffect(() => {
    const tick = (now: number) => {
      const previous = lastFrameRef.current || now;
      lastFrameRef.current = now;
      if (playingRef.current && visibleRef.current && playersReady) {
        let next = elapsedRef.current + Math.min((now - previous) / 1000, 0.12);
        const reachedEnd = next >= DURATION;
        if (reachedEnd) {
          next = DURATION;
          playingRef.current = false;
          setPlaying(false);
          pausePlayers();
        }
        elapsedRef.current = next;
        setElapsed(next);
        syncPlayers(next, !reachedEnd);
      }
      frameRef.current = requestAnimationFrame(tick);
    };
    frameRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frameRef.current);
  }, [pausePlayers, playersReady, syncPlayers]);

  useEffect(() => {
    const element = sourceMount.current?.closest(".trajectory-frame");
    if (!element || !("IntersectionObserver" in window)) return;
    const observer = new IntersectionObserver(([entry]) => {
      visibleRef.current = entry.isIntersecting;
      lastFrameRef.current = performance.now();
      syncPlayers(elapsedRef.current, entry.isIntersecting && playingRef.current);
    }, { threshold: 0.25 });
    observer.observe(element);
    return () => observer.disconnect();
  }, [syncPlayers]);

  useEffect(() => {
    if (!playersReady || !sourceMount.current || !targetMount.current) return;
    const mounts = [sourceMount.current, targetMount.current];
    const position = () => {
      const sourceAnchored = anchorSharedHistory(sourceMount.current);
      const targetAnchored = anchorSharedHistory(targetMount.current);
      const grid = sourceMount.current?.closest<HTMLElement>(".handoff-grid");
      if (grid) {
        grid.dataset.historyAligned = String(
          sourceAnchored && targetAnchored && elapsedRef.current >= HIGHLIGHT_START,
        );
      }
    };
    const mutationObserver = new MutationObserver(position);
    const resizeObserver = new ResizeObserver(position);
    for (const mount of mounts) {
      mutationObserver.observe(mount, { childList: true, characterData: true, subtree: true });
      const windowElement = mount.closest<HTMLElement>(".native-window");
      if (windowElement) resizeObserver.observe(windowElement);
    }
    let secondFrame = 0;
    const firstFrame = requestAnimationFrame(() => {
      position();
      secondFrame = requestAnimationFrame(position);
    });
    return () => {
      mutationObserver.disconnect();
      resizeObserver.disconnect();
      cancelAnimationFrame(firstFrame);
      cancelAnimationFrame(secondFrame);
    };
  }, [playersReady, target]);

  useEffect(() => {
    window.__sessionMigrateDemo = {
      setTime,
      play: () => {
        playingRef.current = true;
        setPlaying(true);
        lastFrameRef.current = performance.now();
        syncPlayers(elapsedRef.current, visibleRef.current);
      },
      pause: () => {
        playingRef.current = false;
        setPlaying(false);
        pausePlayers();
      },
    };
    return () => { delete window.__sessionMigrateDemo; };
  }, [pausePlayers, setTime, syncPlayers]);

  return (
    <>
      <div className="demo-target-toggle" role="group" aria-label="Choose demo target">
        {(["pi", "codex"] as const).map((value) => (
          <button
            type="button"
            className={target === value ? "is-active" : ""}
            aria-pressed={target === value}
            onClick={() => { setTarget(value); window.setTimeout(replay, 0); }}
            key={value}
          >
            Claude → {targetDetails[value].label}
          </button>
        ))}
      </div>

      <figure className="trajectory-frame" data-phase={phase} data-target={target}>
        <div className="trajectory-topbar">
          <div className="trajectory-label"><i /> ACTUAL NATIVE TUIS · INTERACTIVE CAST</div>
          <div className="trajectory-controls">
            <span>{phase === "source" ? "Work in Claude" : phase === "pullback" ? "Hand off the session" : phase === "convert" ? "Migrate the session" : phase === "launch" ? "Resume native session" : phase === "overlap" ? "Same history" : `Continue in ${targetDetail.label}`}</span>
            <button type="button" onClick={() => setTime(elapsedRef.current - 5)} aria-label="Rewind the migration story by five seconds">−5s</button>
            <button type="button" onClick={toggle} aria-label={playing ? "Pause the migration story" : "Play the migration story"}>
              {playing ? "Pause" : "Play"}
            </button>
            <button type="button" onClick={() => setTime(elapsedRef.current + 5)} aria-label="Advance the migration story by five seconds">+5s</button>
            <button type="button" onClick={replay} aria-label="Replay the migration story">Replay</button>
          </div>
        </div>

        <div className="handoff-viewport" aria-label={`Claude Code session migrated to ${targetDetail.label} and continued there`}>
          <div className="handoff-grid" data-phase={phase}>
            <CastWindow label="Claude Code" mountRef={sourceMount} kind="source" cast="/demo-claude.cast" />

            <div className="migration-window">
              <div className="native-window-bar">
                <span className="native-window-dots"><i /><i /><i /></span>
                <b>session-migrate</b>
                <em>{showDone ? "complete" : "working"}</em>
              </div>
              <div className="migration-body">
                <p className="migration-command"><span>$</span> {commandText}<i /></p>
                <div className="migration-output">
                  <p className={showScan ? "is-visible" : ""}><b>source</b><span>Claude Code session found</span></p>
                  <p className={showWrite ? "is-visible" : ""}><b>target</b><span>{targetDetail.label} native session written</span></p>
                  <p className={showDone ? "is-visible migration-done" : ""}><b>✓</b><span>history continued · ready to resume</span></p>
                </div>
                <p className={`migration-launch ${showDone ? "is-visible" : ""}`}><span>→</span> {targetDetail.launch}</p>
              </div>
            </div>

            <CastWindow label={targetDetail.label} mountRef={targetMount} kind="target" cast={targetDetail.cast} />
            <div className="history-bridge" aria-hidden="true"><i /><span>same history</span><i /></div>
          </div>
        </div>

        <label className="trajectory-scrubber">
          <span className="sr-only">Migration story position</span>
          <input
            type="range"
            min="0"
            max={DURATION}
            step="0.1"
            value={elapsed}
            onPointerDown={pauseForSeek}
            onChange={(event) => setTime(Number(event.target.value))}
            aria-label="Seek through the migration story"
            style={{ "--story-progress": `${elapsed / DURATION * 100}%` } as CSSProperties}
          />
        </label>
        <figcaption>
          <span><i /> source · migrate · resume · continue</span>
          <em>Claude Code → {targetDetail.label}</em>
        </figcaption>
      </figure>

      <details className="snapshots" onToggle={(event) => setCompareOpen(event.currentTarget.open)}>
        <summary><span>Compare the native sessions</span><em>Live terminal renders</em></summary>
        {compareOpen && (
          <div className="snapshot-grid">
            <CompareCast label="Claude Code" cast="/demo-claude.cast" posterAt={38} />
            <CompareCast
              label={targetDetail.label}
              cast={targetDetail.cast}
              posterAt={targetDetail.compareAt}
            />
          </div>
        )}
      </details>
    </>
  );
}
