import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { ClipPlayer, downloadRender, ErrorLine, SignalStrip, StatusChip, TimeRange } from "../components/bits";
import { PublishPanel } from "../components/publish";
import { LAYOUT_LABELS, nudge, timecode, type Range } from "../format";
import { go, useAction, useLoad } from "../hooks";
import type { CandidatePatch, Clip, ReviewStatus } from "../types";

const NUDGES = [-2, -0.5, 0.5, 2];

interface Draft {
  range: Range;
  caption: string;
  intro: string;
  layout: string;
  styleId: string;
}

function draftFrom(clip: Clip | null, fallback: Range): Draft {
  return {
    range: clip ? { start: clip.start_s, end: clip.end_s } : fallback,
    caption: clip?.caption_override?.text ?? "",
    intro: clip?.commentary_text ?? "",
    layout: clip?.layout ?? "auto",
    styleId: clip?.caption_style_id ?? "",
  };
}

/** Only what changed, so each edit is recorded once as feedback (services/review.py). */
export function diffDraft(base: Draft, d: Draft): CandidatePatch {
  const p: CandidatePatch = {};
  if (d.range.start !== base.range.start) p.start_s = d.range.start;
  if (d.range.end !== base.range.end) p.end_s = d.range.end;
  if (d.caption.trim() !== base.caption.trim()) p.caption_text = d.caption.trim();
  if (d.intro.trim() !== base.intro.trim()) p.commentary_text = d.intro.trim();
  if (d.layout !== base.layout) p.layout = d.layout;
  if (d.styleId && d.styleId !== base.styleId) p.caption_style_id = d.styleId;
  return p;
}

export function ReviewPage({ videoId, candidateId }: { videoId: string; candidateId: string }) {
  const cands = useLoad(() => api.candidates(videoId), [videoId]);
  const video = useLoad(() => api.getVideo(videoId), [videoId]);
  const styles = useLoad(() => api.listStyles(), []);
  const cand = cands.data?.find((c) => c.id === candidateId);
  const clipId = cand?.clip?.clip_id;

  const [clip, setClip] = useState<Clip | null>(null);
  const [waitingFor, setWaitingFor] = useState<{ kind: "preview" | "final"; since: number; renderId?: string } | null>(null);
  const [view, setView] = useState<"preview" | "final" | null>(null); // null: best available
  const act = useAction();
  const [note, setNote] = useState<string | null>(null);

  // Load the clip once the candidate has one. Don't reset render tracking here: making a
  // preview creates the clip, and the candidate reload that follows must not stop polling.
  // (The page is keyed by candidate, so state never leaks between moments.)
  const loadedId = clip?.id;
  useEffect(() => {
    if (!clipId || clipId === loadedId) return;
    void api
      .getClip(clipId)
      .then((c) => {
        setClip(c);
        // a render started elsewhere (auto-preview, earlier visit): follow it too
        const running = (["final", "preview"] as const).find((k) => c.render_status[k] === "running");
        if (running) setWaitingFor({ kind: running, since: Date.now(), renderId: c.renders.find((r) => r.kind === running)?.id });
      })
      .catch(() => setClip(null));
  }, [clipId, loadedId]);

  // poll while a render we asked for is in flight (gives up after 10 minutes)
  useEffect(() => {
    if (!waitingFor || !clip) return;
    const id = window.setInterval(async () => {
      const fresh = await api.getClip(clip.id).catch(() => null);
      if (!fresh) return;
      const st = fresh.render_status[waitingFor.kind];
      const r = fresh.renders.find((x) => x.kind === waitingFor.kind);
      const changed = r && r.id !== waitingFor.renderId;
      const age = Date.now() - waitingFor.since;
      // a cached render (identical inputs) keeps its id: stop once the queue has had a moment
      const settled = st === "succeeded" && (changed || age > 15_000);
      if (st === "failed" || settled || age > 600_000) {
        setWaitingFor(null);
        setView(waitingFor.kind);
        if (st === "failed") setNote(`The ${waitingFor.kind} render failed. Try again or change the layout.`);
      }
      setClip(fresh);
    }, 3000);
    return () => window.clearInterval(id);
  }, [waitingFor, clip]);

  const fallback = useMemo<Range>(() => ({ start: cand?.start_s ?? 0, end: cand?.end_s ?? 0 }), [cand]);
  const base = useMemo(() => draftFrom(clip, fallback), [clip, fallback]);
  const [draft, setDraft] = useState<Draft>(base);
  useEffect(() => setDraft(base), [base]);
  const changes = diffDraft(base, draft);
  const dirty = Object.keys(changes).length > 0;

  if (cands.error) return <main className="page"><ErrorLine message={cands.error} /></main>;
  if (!cands.data) return <main className="page"><p className="muted">Loading…</p></main>;
  if (!cand) return <main className="page"><p className="empty">This moment no longer exists. It may have been replaced by a re-score.</p><a href={`#/videos/${videoId}`}>Back to the video</a></main>;

  const all = cands.data;
  const idx = all.findIndex((c) => c.id === cand.id);
  const nextPending = [...all.slice(idx + 1), ...all.slice(0, idx)].find((c) => c.status === "pending");
  const has = (k: "preview" | "final") => clip?.renders.some((r) => r.kind === k) ?? false;
  const shownKind = view && has(view) ? view : has("final") ? "final" : has("preview") ? "preview" : null;
  const shownRender = clip?.renders.find((r) => r.kind === shownKind);
  const title = cand.title_suggestions[0] ?? `Moment ${cand.rank}`;

  function track(kind: "preview" | "final", c: Clip | null) {
    setWaitingFor({ kind, since: Date.now(), renderId: c?.renders.find((r) => r.kind === kind)?.id });
  }

  async function makePreview() {
    const c = await act.run(() => api.preview(cand!.id));
    if (c) {
      setClip(c);
      track("preview", c);
      void cands.reload();
    }
  }

  async function save(status?: ReviewStatus) {
    const body: CandidatePatch = { ...changes, ...(status ? { status } : {}) };
    const out = await act.run(() => api.review(cand!.id, body));
    if (!out) return;
    setNote(null);
    if (out.clip) setClip(out.clip);
    if (out.final_render_queued) {
      track("final", out.clip);
      setNote("Approved. The full-quality render is on its way.");
    } else if (out.clip && out.status !== "rejected" && Object.keys(changes).length > 0) {
      // edits on an unapproved clip: refresh the preview so the change is visible
      await api.render(out.clip.id, "preview");
      track("preview", out.clip);
    }
    await cands.reload();
    if (status && status !== "pending" && nextPending) go(`/videos/${videoId}/c/${nextPending.id}`);
    else if (status === "rejected") go(`/videos/${videoId}`);
  }

  const setRange = (edge: "start" | "end", delta: number) =>
    setDraft((d) => ({ ...d, range: nudge(d.range, edge, delta, video.data?.duration_s ?? null) }));

  return (
    <main className="page review">
      <a className="back" href={`#/videos/${videoId}`}>Moments</a>
      <header className="review-head">
        <span className="rank">{cand.rank}</span>
        <h1>{title}</h1>
        <StatusChip status={cand.status} />
      </header>

      <div className="review-grid">
        <section className="stage-col">
          {clip && shownKind && shownRender ? (
            <>
              <ClipPlayer clipId={clip.id} kind={shownKind} version={shownRender.id} />
              {has("preview") && has("final") && (
                <div className="seg" role="group" aria-label="Which render">
                  {(["preview", "final"] as const).map((k) => (
                    <button key={k} type="button" aria-pressed={shownKind === k} className={shownKind === k ? "tab tab-on" : "tab"} onClick={() => setView(k)}>
                      {k === "preview" ? "Preview" : "Final"}
                    </button>
                  ))}
                </div>
              )}
            </>
          ) : (
            <div className="player player-empty">
              {waitingFor || clip?.render_status.preview === "running" ? (
                <span>Rendering preview…</span>
              ) : (
                <button type="button" className="primary" onClick={makePreview} disabled={act.busy}>Make a preview</button>
              )}
            </div>
          )}
          {waitingFor && shownRender && <p className="muted">Rendering {waitingFor.kind}… the video updates when it's done.</p>}
          {note && <p className="note">{note}</p>}
        </section>

        <section className="edit-col">
          <div className="why">
            <SignalStrip candidate={cand} large />
            <p>{cand.reason}</p>
            {cand.hook_text && <p className="hook">{cand.hook_text}</p>}
            {typeof cand.judge?.reason === "string" && <p className="muted">AI judge: {cand.judge.reason}</p>}
          </div>

          <fieldset className="trim">
            <legend>Trim <TimeRange start={draft.range.start} end={draft.range.end} /></legend>
            {(["start", "end"] as const).map((edge) => (
              <div className="trim-row" key={edge}>
                <span className="trim-label">{edge === "start" ? "Start" : "End"} <span className="mono">{timecode(draft.range[edge])}</span></span>
                <span className="nudges">
                  {NUDGES.map((n) => (
                    <button key={n} type="button" onClick={() => setRange(edge, n)} aria-label={`${edge} ${n > 0 ? "+" : ""}${n} seconds`}>
                      {n > 0 ? "+" : "−"}{Math.abs(n)}
                    </button>
                  ))}
                </span>
              </div>
            ))}
          </fieldset>

          <label className="field">
            <span>Intro text <span className="muted">(shown at the start; adds your own commentary)</span></span>
            <input maxLength={200} value={draft.intro} onChange={(e) => setDraft({ ...draft, intro: e.target.value })} placeholder="e.g. He did NOT see that coming" />
          </label>

          <label className="field">
            <span>Captions <span className="muted">(leave empty to use the transcript)</span></span>
            <textarea rows={3} maxLength={2000} value={draft.caption} onChange={(e) => setDraft({ ...draft, caption: e.target.value })} placeholder="Type corrected caption text" />
          </label>

          <div className="field-pair">
            <label className="field">
              <span>Layout</span>
              <select value={draft.layout} onChange={(e) => setDraft({ ...draft, layout: e.target.value })}>
                {Object.entries(LAYOUT_LABELS).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
              </select>
            </label>
            <label className="field">
              <span>Caption style</span>
              <select value={draft.styleId} onChange={(e) => setDraft({ ...draft, styleId: e.target.value })}>
                <option value="">Default</option>
                {(styles.data ?? []).map((s) => <option key={s.id} value={s.id}>{s.name} v{s.version}</option>)}
              </select>
            </label>
          </div>

          {clip?.warnings.map((w) => <p key={w} className="warn">{w}</p>)}

          {clip && (
            <details className="panel">
              <summary>Post text and downloads</summary>
              {clip.export_description ? <pre className="desc">{clip.export_description}</pre> : <p className="muted">No suggested title or credit. Titles come from the AI judge, which is off; credit comes from the source.</p>}
              <div className="add-row">
                <button type="button" disabled={!clip.export_description} onClick={() => void navigator.clipboard?.writeText(clip.export_description).then(() => setNote("Post text copied."))}>Copy post text</button>
                {has("final") && <button type="button" onClick={() => void act.run(() => downloadRender(clip.id, "final", title))}>Download final</button>}
                {has("preview") && <button type="button" onClick={() => void act.run(() => downloadRender(clip.id, "preview", title))}>Download preview</button>}
              </div>
              {clip.credit_text && <p className="muted">Credit included: {clip.credit_text}</p>}
            </details>
          )}

          {clip && cand.status === "approved" && <PublishPanel key={clip.id} clip={clip} suggestedTitle={title} />}

          <ErrorLine message={act.error} />
        </section>
      </div>

      <div className="actionbar">
        <button type="button" className="reject" onClick={() => void save("rejected")} disabled={act.busy}>Reject</button>
        {dirty && <button type="button" onClick={() => void save()} disabled={act.busy}>Save edits</button>}
        <button type="button" className="approve" onClick={() => void save("approved")} disabled={act.busy || (cand.status === "approved" && !dirty)}>
          {cand.status === "approved" ? (dirty ? "Save and re-render" : "Approved") : "Approve"}
        </button>
      </div>
    </main>
  );
}
