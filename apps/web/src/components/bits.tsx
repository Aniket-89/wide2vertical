import { useEffect, useState } from "react";
import { api } from "../api";
import { signalValues, stageTrack, timecode } from "../format";
import type { Candidate, Stage } from "../types";

/** Why a moment was picked: one bar per signal, in the colour that signal owns. */
export function SignalStrip({ candidate, large = false }: { candidate: Candidate; large?: boolean }) {
  const values = signalValues(candidate);
  if (values.length === 0) return null;
  return (
    <div className={large ? "signals signals-large" : "signals"} role="img"
      aria-label={values.map((v) => `${v.label} ${Math.round(v.value * 100)}%`).join(", ")}>
      {values.map((v) => (
        <div className="signal" key={v.key} title={`${v.label}: ${Math.round(v.value * 100)}%`}>
          <div className="signal-track">
            <div className="signal-fill" style={{ height: `${Math.max(4, v.value * 100)}%`, background: v.color }} />
          </div>
          {large && <span className="signal-label">{v.label}</span>}
        </div>
      ))}
    </div>
  );
}

const STAGE_NAMES: Record<string, string> = {
  fetch: "Download",
  transcribe: "Transcribe",
  signals: "Signals",
  candidates: "Find moments",
  judge: "AI judge",
  render: "Render",
};

/** Pipeline progress: one segment per stage. */
export function StageBar({ stages, compact = false }: { stages: Stage[]; compact?: boolean }) {
  const track = stageTrack(stages);
  const failed = track.find((s) => s.status === "failed");
  return (
    <div className="stagebar-wrap">
      <ol className="stagebar" aria-label="Pipeline progress">
        {track.map((s) => (
          <li key={s.stage} className={`stage stage-${s.status}`} title={`${STAGE_NAMES[s.stage] ?? s.stage}: ${s.status}`}>
            {!compact && <span>{STAGE_NAMES[s.stage] ?? s.stage}</span>}
          </li>
        ))}
      </ol>
      {failed && !compact && (
        <p className="error">
          {STAGE_NAMES[failed.stage] ?? failed.stage} failed{failed.error ? `: ${failed.error}` : "."}
        </p>
      )}
    </div>
  );
}

export function StatusChip({ status }: { status: string }) {
  const label = { pending: "To review", approved: "Approved", rejected: "Rejected" }[status] ?? status;
  return <span className={`chip chip-${status}`}>{label}</span>;
}

export function TimeRange({ start, end }: { start: number; end: number }) {
  return (
    <span className="mono">
      {timecode(start)}–{timecode(end)} <span className="muted">({Math.round(end - start)} s)</span>
    </span>
  );
}

export function ErrorLine({ message }: { message: string | null | undefined }) {
  return message ? <p className="error" role="alert">{message}</p> : null;
}

/** Plays a render. The download needs the bearer token, so it's fetched as a blob. */
export function ClipPlayer({ clipId, kind, version }: { clipId: string; kind: "preview" | "final"; version: string }) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let revoked = false;
    let objectUrl: string | null = null;
    setUrl(null);
    setError(null);
    api
      .renderBlob(clipId, kind)
      .then((blob) => {
        if (revoked) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch((e: unknown) => !revoked && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      revoked = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [clipId, kind, version]);
  if (error) return <div className="player player-empty">{error}</div>;
  if (!url) return <div className="player player-empty">Loading video…</div>;
  return <video className="player" src={url} controls playsInline preload="metadata" />;
}

/** Save a render to the device. */
export async function downloadRender(clipId: string, kind: "preview" | "final", name: string): Promise<void> {
  const blob = await api.renderBlob(clipId, kind);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${name.replace(/[^A-Za-z0-9._-]+/g, "-").slice(0, 60) || "clip"}-${kind}.mp4`;
  document.body.append(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
