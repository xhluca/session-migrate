"use client";

import Image from "next/image";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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

const DURATION = 43;
const TARGET_START = 17.5;

const targetDetails = {
  pi: {
    label: "Pi",
    cast: "/demo-pi.cast",
    launch: "pi --session 2000…0000",
    screenshot: "/demo-after-pi.png",
    screenshotAlt: "Pi native TUI after migration from Claude Code",
  },
  codex: {
    label: "Codex",
    cast: "/demo-codex.cast",
    launch: "codex resume 3000…0000",
    screenshot: "/demo-after-codex.png",
    screenshotAlt: "Codex native TUI after migration from Claude Code",
  },
} as const;

function phaseAt(time: number): Phase {
  if (time < 8) return "source";
  if (time < 10.5) return "pullback";
  if (time < 16) return "convert";
  if (time < 18.5) return "launch";
  if (time < 23.5) return "overlap";
  return "target";
}

function typed(text: string, progress: number) {
  return text.slice(0, Math.floor(text.length * Math.max(0, Math.min(1, progress))));
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
      <div className="history-marker" aria-hidden="true"><span>shared history</span></div>
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
  const [target, setTarget] = useState<Target>("pi");
  const [playing, setPlaying] = useState(true);
  const [elapsed, setElapsed] = useState(0);
  const [playersReady, setPlayersReady] = useState(false);
  const targetDetail = targetDetails[target];
  const phase = phaseAt(elapsed);

  const migrationCommand = useMemo(
    () => `smigrate transfer 1000…0000 --from claude --to ${target}`,
    [target],
  );
  const commandText = typed(migrationCommand, (elapsed - 10.8) / 2.7);
  const showScan = elapsed >= 13.4;
  const showWrite = elapsed >= 14.2;
  const showDone = elapsed >= 15;

  const pausePlayers = useCallback(() => {
    sourcePlayer.current?.pause();
    targetPlayer.current?.pause();
  }, []);

  const syncPlayers = useCallback((time: number, shouldPlay: boolean) => {
    if (!sourcePlayer.current || !targetPlayer.current) return;
    if (time < TARGET_START) {
      targetPlayer.current.pause();
      if (shouldPlay) sourcePlayer.current.play();
      else sourcePlayer.current.pause();
    } else {
      sourcePlayer.current.pause();
      if (shouldPlay) targetPlayer.current.play();
      else targetPlayer.current.pause();
    }
  }, []);

  const setTime = useCallback((time: number) => {
    const next = Math.max(0, Math.min(DURATION, time));
    elapsedRef.current = next;
    setElapsed(next);
    sourcePlayer.current?.seek(next * 2);
    targetPlayer.current?.seek(Math.max(0, next - TARGET_START));
    syncPlayers(next, playingRef.current && visibleRef.current);
  }, [syncPlayers]);

  const replay = useCallback(() => {
    sourcePlayer.current?.seek(0);
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
      sourceMount.current.replaceChildren();
      targetMount.current.replaceChildren();
      const common = {
        autoPlay: false,
        controls: false,
        fit: "width",
        idleTimeLimit: 2,
        loop: false,
        theme: "asciinema",
        terminalFontFamily: "Geist Mono, monospace",
        terminalLineHeight: 1.38,
      };
      sourcePlayer.current = window.AsciinemaPlayer.create(
        "/demo-claude.cast",
        sourceMount.current,
        { ...common, speed: 2 },
      );
      targetPlayer.current = window.AsciinemaPlayer.create(
        targetDetail.cast,
        targetMount.current,
        { ...common, speed: 1 },
      );
      sourcePlayer.current.seek(0);
      targetPlayer.current.seek(0);
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
  }, [syncPlayers, targetDetail.cast]);

  useEffect(() => {
    const tick = (now: number) => {
      const previous = lastFrameRef.current || now;
      lastFrameRef.current = now;
      if (playingRef.current && visibleRef.current && playersReady) {
        let next = elapsedRef.current + Math.min((now - previous) / 1000, 0.12);
        if (next >= DURATION) {
          sourcePlayer.current?.seek(0);
          targetPlayer.current?.seek(0);
          next = 0;
        }
        elapsedRef.current = next;
        setElapsed(next);
        syncPlayers(next, true);
      }
      frameRef.current = requestAnimationFrame(tick);
    };
    frameRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frameRef.current);
  }, [playersReady, syncPlayers]);

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
            <span>{phase === "source" ? "Start in Claude" : phase === "convert" ? "Migrate" : phase === "overlap" ? "Same history" : `Continue in ${targetDetail.label}`}</span>
            <button type="button" onClick={toggle} aria-label={playing ? "Pause the migration story" : "Play the migration story"}>
              {playing ? "Pause" : "Play"}
            </button>
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
                  <p className={showDone ? "is-visible migration-done" : ""}><b>✓</b><span>source unchanged · ready to resume</span></p>
                </div>
                <p className={`migration-launch ${showDone ? "is-visible" : ""}`}><span>$</span> {targetDetail.launch}</p>
              </div>
            </div>

            <CastWindow label={targetDetail.label} mountRef={targetMount} kind="target" cast={targetDetail.cast} />
            <div className="history-bridge" aria-hidden="true"><i /><span>same conversation</span><i /></div>
          </div>
        </div>

        <div className="trajectory-progress" aria-hidden="true"><i style={{ width: `${elapsed / DURATION * 100}%` }} /></div>
        <figcaption>
          <span><i /> source · migrate · resume · continue</span>
          <em>Claude Code → {targetDetail.label}</em>
        </figcaption>
      </figure>

      <details className="snapshots">
        <summary><span>Compare the native sessions</span><em>Actual TUI screenshots</em></summary>
        <div className="snapshot-grid">
          <figure>
            <Image src="/demo-before.png" width={1120} height={870} alt="Claude Code native TUI before migration" />
            <figcaption>Before · Claude Code TUI</figcaption>
          </figure>
          <figure>
            <Image src={targetDetail.screenshot} width={1120} height={870} alt={targetDetail.screenshotAlt} />
            <figcaption>After · {targetDetail.label} TUI</figcaption>
          </figure>
        </div>
      </details>
    </>
  );
}
