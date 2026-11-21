import { useEffect, useState } from "react";
import { api } from "../api";
import { ErrorLine, SignalStrip, StageBar, StatusChip, TimeRange } from "../components/bits";
import { duration, isBusy, latestProfiles, profileLabel } from "../format";
import { useAction, useLoad } from "../hooks";
import type { Candidate, ReviewStatus } from "../types";

type Filter = ReviewStatus | "all";
const FILTERS: { key: Filter; label: string }[] = [
  { key: "pending", label: "To review" },
  { key: "approved", label: "Approved" },
  { key: "rejected", label: "Rejected" },
  { key: "all", label: "All" },
];

function renderNote(c: Candidate): string {
  const s = c.clip;
  if (!s) return "";
  if (s.final === "succeeded") return "Final ready";
  if (s.final === "running") return "Rendering final…";
  if (s.final === "failed") return "Final render failed";
  if (s.preview === "succeeded") return "Preview ready";
  if (s.preview === "running") return "Rendering preview…";
  if (s.preview === "failed") return "Preview failed";
  return "";
}

export function VideoDetailPage({ videoId }: { videoId: string }) {
  const video = useLoad(() => api.getVideo(videoId), [videoId], {
    pollMs: 4000,
    poll: (v) => isBusy(v.stages),
  });
  const busy = video.data ? isBusy(video.data.stages) : true;
  const cands = useLoad(() => api.candidates(videoId), [videoId], {
    pollMs: 5000,
    poll: (cs) => busy || cs.some((c) => c.clip && (c.clip.preview === "running" || c.clip.final === "running")),
  });
  const profiles = useLoad(() => api.listProfiles(), []);
  const reloadCands = cands.reload;
  useEffect(() => {
    if (!busy) void reloadCands(); // analysis just finished: fetch the final list once
  }, [busy, reloadCands]);
  const [filter, setFilter] = useState<Filter>("pending");
  const [profileId, setProfileId] = useState("");
  const reanalyze = useAction();

  const v = video.data;
  const all = cands.data ?? [];
  const shown = filter === "all" ? all : all.filter((c) => c.status === filter);
  const counts = Object.fromEntries(FILTERS.map((f) => [f.key, f.key === "all" ? all.length : all.filter((c) => c.status === f.key).length]));
  const current = profiles.data?.find((p) => p.id === v?.scoring_profile_id);

  async function rerun() {
    if (!profileId) return;
    const out = await reanalyze.run(() => api.reanalyze(videoId, profileId));
    if (out) {
      setProfileId("");
      await video.reload();
      await cands.reload();
    }
  }

  return (
    <main className="page">
      <a className="back" href="#/">All videos</a>
      <ErrorLine message={video.error} />
      {v && (
        <header className="video-head">
          <h1>{v.title || v.url}</h1>
          <p className="row-meta">
            <span className={`platform platform-${v.platform}`}>{v.platform}</span>
            {duration(v.duration_s) && <span>{duration(v.duration_s)}</span>}
            <a href={v.url} target="_blank" rel="noreferrer noopener">Open original</a>
          </p>
          <StageBar stages={v.stages} />
        </header>
      )}

      {v && !busy && (
        <details className="panel">
          <summary>Scoring: {current ? profileLabel(current) : "default profile"}</summary>
          <p className="muted">
            Re-score this video with another profile. Signals are cached, so this takes
            seconds. Clips you have already reviewed keep your decision.
          </p>
          <div className="add-row">
            <select value={profileId} onChange={(e) => setProfileId(e.target.value)} aria-label="Scoring profile">
              <option value="">Choose a profile</option>
              {latestProfiles(profiles.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>{profileLabel(p)}</option>
              ))}
            </select>
            <button type="button" onClick={rerun} disabled={!profileId || reanalyze.busy}>
              {reanalyze.busy ? "Starting…" : "Re-score"}
            </button>
          </div>
          <ErrorLine message={reanalyze.error} />
        </details>
      )}

      <nav className="tabs" aria-label="Filter moments">
        {FILTERS.map((f) => (
          <button key={f.key} type="button" className={filter === f.key ? "tab tab-on" : "tab"}
            aria-pressed={filter === f.key} onClick={() => setFilter(f.key)}>
            {f.label} <span className="count">{counts[f.key]}</span>
          </button>
        ))}
      </nav>

      <ErrorLine message={cands.error} />
      {cands.data && shown.length === 0 && (
        <p className="empty">
          {busy
            ? "Still working on this video. Moments appear here when analysis finishes."
            : filter === "pending"
              ? "Nothing left to review. Check Approved for your clips."
              : "No moments here."}
        </p>
      )}
      <ol className="rows">
        {shown.map((c) => (
          <li key={c.id}>
            <a className="row moment" href={`#/videos/${videoId}/c/${c.id}`}>
              <span className="rank" aria-label={`Rank ${c.rank}`}>{c.rank}</span>
              <span className="row-main">
                <span className="row-title">{c.title_suggestions[0] ?? c.reason}</span>
                <span className="row-meta">
                  <TimeRange start={c.start_s} end={c.end_s} />
                  {renderNote(c) && <span>{renderNote(c)}</span>}
                </span>
              </span>
              <SignalStrip candidate={c} />
              {filter === "all" && <StatusChip status={c.status} />}
            </a>
          </li>
        ))}
      </ol>
    </main>
  );
}
