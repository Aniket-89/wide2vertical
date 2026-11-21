import { useState, type FormEvent } from "react";
import { api } from "../api";
import { ErrorLine } from "../components/bits";
import { duration, latestProfiles, profileLabel } from "../format";
import { go, useAction, useLoad } from "../hooks";

/** "transcribe_done" -> "Transcribed"; keeps the list readable without the stage detail. */
export function videoStatus(status: string): string {
  const map: Record<string, string> = {
    pending: "Queued",
    ingest_done: "Queued",
    fetch_done: "Downloaded",
    transcribe_done: "Transcribed",
    signals_done: "Analysing",
    candidates_done: "Ready to review",
    judge_done: "Ready to review",
    render_done: "Ready to review",
    source_retired: "Source retired",
  };
  if (map[status]) return map[status];
  if (status.endsWith("_failed")) return `Failed at ${status.replace("_failed", "")}`;
  return status.replace(/_/g, " ");
}

export function VideosPage() {
  const videos = useLoad(() => api.listVideos(), [], {
    pollMs: 5000,
    poll: (vs) => vs.some((v) => !videoStatus(v.status).startsWith("Ready") && !v.status.endsWith("_failed") && v.status !== "source_retired"),
  });
  const profiles = useLoad(() => api.listProfiles(), []);
  const [url, setUrl] = useState("");
  const [profileId, setProfileId] = useState("");
  const [override, setOverride] = useState(false);
  const add = useAction();

  async function submit(e: FormEvent) {
    e.preventDefault();
    const v = await add.run(() => api.addVideo(url.trim(), override, profileId || null));
    if (v) {
      setUrl("");
      setOverride(false);
      go(`/videos/${v.id}`);
    }
  }

  // Allowlist refusals that an override can lift (not takedowns): compliance/allowlist.py
  const blocked = !!add.error && /add it to sources|set it on the source/.test(add.error);

  return (
    <main className="page">
      <form className="add" onSubmit={submit}>
        <label htmlFor="url" className="visually-hidden">Stream or VOD link</label>
        <input
          id="url"
          type="url"
          inputMode="url"
          placeholder="Paste a YouTube, Twitch or Kick VOD link"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          required
        />
        <div className="add-row">
          <label className="field-inline">
            <span>Scoring</span>
            <select value={profileId} onChange={(e) => setProfileId(e.target.value)}>
              <option value="">Default profile</option>
              {latestProfiles(profiles.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>{profileLabel(p)}</option>
              ))}
            </select>
          </label>
          <button type="submit" className="primary" disabled={add.busy || !url.trim()}>
            {add.busy ? "Checking…" : "Find clips"}
          </button>
        </div>
        <ErrorLine message={add.error} />
        {(blocked || override) && (
          <label className="override">
            <input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} />
            <span>
              Process anyway. I have permission for this channel. The override is logged; add
              the channel under Sources to stop seeing this.
            </span>
          </label>
        )}
      </form>

      <ErrorLine message={videos.error} />
      {videos.data && videos.data.length === 0 && (
        <p className="empty">No videos yet. Paste a VOD link above to find its best moments.</p>
      )}
      <ul className="rows">
        {(videos.data ?? []).map((v) => (
          <li key={v.id}>
            <a className="row" href={`#/videos/${v.id}`}>
              <span className="row-main">
                <span className="row-title">{v.title || v.url}</span>
                <span className="row-meta">
                  <span className={`platform platform-${v.platform}`}>{v.platform}</span>
                  {duration(v.duration_s) && <span>{duration(v.duration_s)}</span>}
                  {v.override_used && <span className="warn-text">override</span>}
                </span>
              </span>
              <span className={`row-status ${v.status.endsWith("_failed") ? "error-text" : ""}`}>
                {videoStatus(v.status)}
              </span>
            </a>
          </li>
        ))}
      </ul>
    </main>
  );
}
