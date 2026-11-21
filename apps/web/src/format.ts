// Pure helpers shared by the pages (unit-tested in format.test.ts).
import type { Candidate, ScoringProfile, Stage } from "./types";

/** 3725.4 -> "1:02:05.4"; 65 -> "1:05.0". Timecodes into a stream, tenths shown. */
export function timecode(seconds: number): string {
  const tenths = Math.max(0, Math.round(seconds * 10));
  const h = Math.floor(tenths / 36000);
  const m = Math.floor((tenths % 36000) / 600);
  const s = ((tenths % 600) / 10).toFixed(1).padStart(4, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

/** Video length for lists: "48 min", "2 h 5 min". */
export function duration(seconds: number | null | undefined): string {
  if (!seconds) return "";
  const m = Math.max(1, Math.round(seconds / 60));
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
}

// Same limits the API enforces (clipforge/services/review.py), so the UI never sends a
// trim the server will refuse.
export const MIN_CLIP_S = 5;
export const MAX_CLIP_S = 180;

export interface Range {
  start: number;
  end: number;
}

/** Move one edge of the clip by delta seconds, clamped to the video and the length limits. */
export function nudge(range: Range, edge: "start" | "end", delta: number, videoDuration: number | null): Range {
  const round = (v: number) => Math.round(v * 10) / 10;
  const maxEnd = videoDuration ?? Number.POSITIVE_INFINITY;
  let { start, end } = range;
  if (edge === "start") {
    start = Math.min(Math.max(0, start + delta), end - MIN_CLIP_S);
    start = Math.max(start, end - MAX_CLIP_S);
  } else {
    end = Math.max(Math.min(maxEnd, end + delta), start + MIN_CLIP_S);
    end = Math.min(end, start + MAX_CLIP_S);
  }
  return { start: round(start), end: round(end) };
}

/** The signal bars on a candidate, in a fixed order, each with the colour it owns. */
export const SIGNALS = [
  { key: "chat_velocity", label: "Chat", color: "var(--chat)" },
  { key: "emote_burst", label: "Emotes", color: "var(--emote)" },
  { key: "audio_energy", label: "Audio", color: "var(--audio)" },
  { key: "speech_intensity", label: "Speech", color: "var(--speech)" },
  { key: "llm_moment_score", label: "Judge", color: "var(--judge)" },
] as const;

/** Weights that aren't a time-series signal but can still be tuned in a profile. */
export const OTHER_WEIGHT_LABELS: Record<string, string> = { keyword_boost: "Keywords" };

export interface SignalValue {
  key: string;
  label: string;
  color: string;
  value: number;
}

/** Only the signals this candidate actually has (chat may be unavailable, judge off). */
export function signalValues(c: Pick<Candidate, "components">): SignalValue[] {
  return SIGNALS.flatMap((s) => {
    const v = c.components[s.key];
    return typeof v === "number" && Number.isFinite(v)
      ? [{ ...s, value: Math.max(0, Math.min(1, v)) }]
      : [];
  });
}

export const STAGE_ORDER = ["fetch", "transcribe", "signals", "candidates", "judge", "render"] as const;

export interface StageStep {
  stage: string;
  status: string; // pending | running | succeeded | failed | skipped | waiting
  error: string | null;
}

/** Latest status per pipeline stage in pipeline order. Stages that haven't started are
 *  "waiting"; judge and render only appear once they've run (both are optional). */
export function stageTrack(stages: Stage[]): StageStep[] {
  const latest = new Map<string, Stage>();
  for (const s of stages) latest.set(s.stage, s); // API returns oldest first
  return STAGE_ORDER.filter((n) => (n !== "judge" && n !== "render") || latest.has(n)).map((n) => {
    const s = latest.get(n);
    return { stage: n, status: s?.status ?? "waiting", error: s?.error ?? null };
  });
}

/** Something is still happening, so the page should keep polling. */
export function isBusy(stages: Stage[]): boolean {
  if (stages.some((s) => s.status === "running" || s.status === "pending")) return true;
  const track = stageTrack(stages);
  const failed = track.some((s) => s.status === "failed");
  const done = track.find((s) => s.stage === "candidates")?.status === "succeeded";
  return !failed && !done;
}

export function profileLabel(p: ScoringProfile): string {
  return `${p.name} v${p.version}${p.is_default ? " (default)" : ""}`;
}

/** Newest version of each profile name, default first. */
export function latestProfiles(all: ScoringProfile[]): ScoringProfile[] {
  const byName = new Map<string, ScoringProfile>();
  for (const p of all) {
    const cur = byName.get(p.name);
    if (!cur || p.version > cur.version) byName.set(p.name, p);
  }
  return [...byName.values()].sort(
    (a, b) => Number(b.is_default) - Number(a.is_default) || a.name.localeCompare(b.name),
  );
}

export const POLICY_LABELS: Record<string, string> = {
  campaign: "Clipping campaign",
  written_permission: "Written permission",
  open_policy: "Open clip policy",
  own_channel: "My own channel",
  unknown: "Unknown (blocked)",
};

export const LAYOUT_LABELS: Record<string, string> = {
  auto: "Automatic",
  facecam_top_gameplay_bottom: "Facecam over gameplay",
  center_crop: "Centre crop",
  gameplay_blur_background: "Full frame, blurred fill",
};
