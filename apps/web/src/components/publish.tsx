import { useState, type FormEvent } from "react";
import { api } from "../api";
import { useAction, useLoad } from "../hooks";
import type { Clip, Privacy, Publication } from "../types";
import { ErrorLine } from "./bits";

const STATUS_TEXT: Record<Publication["status"], string> = {
  scheduled: "Scheduled",
  queued: "Waiting to upload",
  uploading: "Uploading…",
  published: "Published",
  failed: "Failed",
  cancelled: "Cancelled",
};

export function isActive(p: Publication): boolean {
  return p.status === "queued" || p.status === "uploading";
}

/** datetime-local value ("2026-11-21T18:30") in the device's zone -> ISO with offset. */
export function localToIso(local: string): string {
  return new Date(local).toISOString();
}

export function PublicationRow({ p, onChanged }: { p: Publication; onChanged: () => void }) {
  const act = useAction();
  const when = p.published_at ?? p.scheduled_at;
  return (
    <li className="pub">
      <div className="pub-main">
        <span className="row-title">{p.title}</span>
        <span className="row-meta">
          <span className={`pub-status pub-${p.status}`}>{STATUS_TEXT[p.status]}</span>
          <span>{p.account_label}</span>
          <span>{p.privacy}</span>
          {when && <span>{new Date(when).toLocaleString()}</span>}
          {p.stats?.views != null && <span className="mono">{p.stats.views.toLocaleString()} views</span>}
        </span>
        {p.error && <span className="error">{p.error}</span>}
      </div>
      {p.url && <a href={p.url} target="_blank" rel="noreferrer noopener">Open</a>}
      {(p.status === "scheduled" || p.status === "queued") && (
        <button type="button" disabled={act.busy} onClick={async () => { if (await act.run(() => api.cancelPublication(p.id))) onChanged(); }}>
          Cancel
        </button>
      )}
      <ErrorLine message={act.error} />
    </li>
  );
}

/** On the review screen for an approved clip with a final render. */
export function PublishPanel({ clip, suggestedTitle }: { clip: Clip; suggestedTitle: string }) {
  const targets = useLoad(() => api.listTargets(), []);
  const pubs = useLoad(() => api.listPublications(clip.id), [clip.id], {
    pollMs: 4000,
    poll: (ps) => ps.some(isActive),
  });
  const [title, setTitle] = useState(suggestedTitle);
  const [description, setDescription] = useState(clip.export_description);
  const [privacy, setPrivacy] = useState<Privacy>("private");
  const [mode, setMode] = useState<"now" | "later">("now");
  const [at, setAt] = useState("");
  const [targetId, setTargetId] = useState("");
  const act = useAction();
  const hasFinal = clip.renders.some((r) => r.kind === "final");
  const usable = (targets.data ?? []).filter((t) => t.enabled && t.connected);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const out = await act.run(() =>
      api.publish(clip.id, {
        target_id: targetId || undefined,
        title,
        description,
        privacy,
        scheduled_at: mode === "later" && at ? localToIso(at) : undefined,
      }),
    );
    if (out) await pubs.reload();
  }

  return (
    <details className="panel" open={(pubs.data?.length ?? 0) > 0 || undefined}>
      <summary>Publish</summary>
      {(pubs.data ?? []).length > 0 && (
        <ul className="rows pubs">
          {(pubs.data ?? []).map((p) => <PublicationRow key={p.id} p={p} onChanged={() => void pubs.reload()} />)}
        </ul>
      )}
      {targets.data && usable.length === 0 ? (
        <p className="muted">
          Connect a YouTube channel first. On the computer running ClipForge, run
          <code> clipforge publish connect-youtube client_secret.json</code>.
        </p>
      ) : !hasFinal ? (
        <p className="muted">The full-quality render is still on its way. Publishing opens when it's ready.</p>
      ) : (
        <form className="form" onSubmit={submit}>
          {usable.length > 1 && (
            <label className="field">
              <span>Channel</span>
              <select value={targetId} onChange={(e) => setTargetId(e.target.value)}>
                {usable.map((t) => <option key={t.id} value={t.id}>{t.account_label} ({t.platform})</option>)}
              </select>
            </label>
          )}
          <label className="field">
            <span>Title <span className="muted">({title.length}/100)</span></span>
            <input value={title} maxLength={100} onChange={(e) => setTitle(e.target.value)} required />
          </label>
          <label className="field">
            <span>Description <span className="muted">(the credit line is always kept)</span></span>
            <textarea rows={4} value={description} maxLength={5000} onChange={(e) => setDescription(e.target.value)} />
          </label>
          <div className="field-pair">
            <label className="field">
              <span>Visibility</span>
              <select value={privacy} onChange={(e) => setPrivacy(e.target.value as Privacy)}>
                <option value="private">Private</option>
                <option value="unlisted">Unlisted</option>
                <option value="public">Public</option>
              </select>
            </label>
            <label className="field">
              <span>When</span>
              <select value={mode} onChange={(e) => setMode(e.target.value as "now" | "later")}>
                <option value="now">Now</option>
                <option value="later">Schedule</option>
              </select>
            </label>
          </div>
          {mode === "later" && (
            <label className="field">
              <span>Publish at</span>
              <input type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} required />
            </label>
          )}
          {privacy !== "private" && (
            <p className="warn">
              YouTube keeps uploads private until your Google API project passes its audit, whatever
              you choose here.
            </p>
          )}
          <button type="submit" className="primary" disabled={act.busy}>
            {mode === "later" ? "Schedule post" : "Publish now"}
          </button>
          <ErrorLine message={act.error} />
        </form>
      )}
    </details>
  );
}
