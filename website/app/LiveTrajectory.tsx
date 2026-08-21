"use client";

import Image from "next/image";
import { useEffect, useRef, useState } from "react";

type Target = "pi" | "codex";

const targetDetails = {
  pi: {
    label: "Pi",
    video: "/demo-pi.mp4",
    screenshot: "/demo-after-pi.png",
    screenshotAlt: "Pi native TUI after migration from Claude Code",
  },
  codex: {
    label: "Codex",
    video: "/demo-codex.mp4",
    screenshot: "/demo-after-codex.png",
    screenshotAlt: "Codex native TUI after migration from Claude Code",
  },
} as const;

export function LiveTrajectory() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [target, setTarget] = useState<Target>("pi");
  const [playing, setPlaying] = useState(true);
  const [progress, setProgress] = useState(0);
  const targetDetail = targetDetails[target];

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.load();
    video.currentTime = 0;
    setProgress(0);
    void video.play().then(() => setPlaying(true)).catch(() => setPlaying(false));
  }, [target]);

  const toggle = () => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) {
      void video.play().then(() => setPlaying(true));
    } else {
      video.pause();
      setPlaying(false);
    }
  };

  const replay = () => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = 0;
    setProgress(0);
    void video.play().then(() => setPlaying(true));
  };

  return (
    <>
      <div className="demo-target-toggle" role="group" aria-label="Choose demo target">
        {(["pi", "codex"] as const).map((value) => (
          <button
            type="button"
            className={target === value ? "is-active" : ""}
            aria-pressed={target === value}
            onClick={() => setTarget(value)}
            key={value}
          >
            Claude → {targetDetails[value].label}
          </button>
        ))}
      </div>

      <figure className="trajectory-frame">
        <div className="trajectory-topbar">
          <div className="trajectory-label"><i /> REAL NATIVE TUIS · 1440P</div>
          <div className="trajectory-controls">
            <span>Claude 2× · {targetDetail.label} 1×</span>
            <button type="button" onClick={toggle} aria-label={playing ? "Pause native TUI recording" : "Play native TUI recording"}>
              {playing ? "Pause" : "Play"}
            </button>
            <button type="button" onClick={replay} aria-label="Replay native TUI recording">
              Replay
            </button>
          </div>
        </div>

        <div className="trajectory-video-shell">
          <video
            ref={videoRef}
            key={target}
            muted
            autoPlay
            loop
            playsInline
            preload="metadata"
            poster="/demo-before.png"
            aria-label={`Real terminal recording of a Claude Code session migrated to ${targetDetail.label} and continued there`}
            onPlay={() => setPlaying(true)}
            onPause={() => setPlaying(false)}
            onTimeUpdate={(event) => {
              const video = event.currentTarget;
              setProgress(video.duration ? (video.currentTime / video.duration) * 100 : 0);
            }}
          >
            <source src={targetDetail.video} type="video/mp4" />
          </video>
        </div>

        <div className="trajectory-progress" aria-hidden="true"><i style={{ width: `${progress}%` }} /></div>
        <figcaption>
          <span><i /> actual Claude Code and {targetDetail.label} terminal sessions</span>
          <em>Claude review 2× · target continuation 1×</em>
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
