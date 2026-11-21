import { useState, type FormEvent } from "react";
import { api } from "../api";
import { ErrorLine } from "../components/bits";
import { POLICY_LABELS } from "../format";
import { useAction, useLoad } from "../hooks";
import type { Source } from "../types";

const EMPTY = { platform: "twitch", channel_handle: "", clip_policy: "campaign", required_credit_text: "", policy_url: "", notes: "" };

function SourceRow({ s, onChanged }: { s: Source; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [credit, setCredit] = useState(s.required_credit_text);
  const [policy, setPolicy] = useState(s.clip_policy);
  const act = useAction();
  const retired = s.status === "retired";

  async function save() {
    if (await act.run(() => api.updateSource(s.id, { required_credit_text: credit, clip_policy: policy }))) onChanged();
  }
  async function retire() {
    const reason = window.prompt(
      `Retire ${s.channel_handle}? This blocks the channel and hides all of its clips. Reason (optional):`,
    );
    if (reason === null) return;
    const out = await act.run(() => api.retireSource(s.id, reason));
    if (out) {
      window.alert(`Retired. ${out.clips_hidden} clip${out.clips_hidden === 1 ? "" : "s"} hidden.`);
      onChanged();
    }
  }

  return (
    <li className={retired ? "source retired" : "source"}>
      <button type="button" className="row" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="row-main">
          <span className="row-title">{s.channel_handle}</span>
          <span className="row-meta">
            <span className={`platform platform-${s.platform}`}>{s.platform}</span>
            <span>{retired ? "Retired" : (POLICY_LABELS[s.clip_policy] ?? s.clip_policy)}</span>
          </span>
        </span>
      </button>
      {open && (
        <div className="source-edit">
          {retired ? (
            <p className="muted">Retired {s.retired_at ? new Date(s.retired_at).toLocaleDateString() : ""}. Its clips are hidden and new videos are refused.</p>
          ) : (
            <>
              <label className="field">
                <span>Permission</span>
                <select value={policy} onChange={(e) => setPolicy(e.target.value)}>
                  {Object.entries(POLICY_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                </select>
              </label>
              <label className="field">
                <span>Credit line added to every clip</span>
                <input value={credit} onChange={(e) => setCredit(e.target.value)} placeholder="Clip: twitch.tv/name" />
              </label>
              {s.policy_url && <p><a href={s.policy_url} target="_blank" rel="noreferrer noopener">Policy page</a></p>}
              <div className="add-row">
                <button type="button" onClick={save} disabled={act.busy}>Save</button>
                <button type="button" className="reject" onClick={retire} disabled={act.busy}>Retire (takedown)</button>
              </div>
            </>
          )}
          <ErrorLine message={act.error} />
        </div>
      )}
    </li>
  );
}

export function SourcesPage() {
  const sources = useLoad(() => api.listSources(), []);
  const [form, setForm] = useState(EMPTY);
  const add = useAction();

  async function submit(e: FormEvent) {
    e.preventDefault();
    const s = await add.run(() => api.addSource({ ...form, policy_url: form.policy_url || null }));
    if (s) {
      setForm(EMPTY);
      await sources.reload();
    }
  }

  const set = (k: keyof typeof EMPTY) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });

  return (
    <main className="page">
      <h1>Sources</h1>
      <p className="muted">
        Only channels listed here, with a known permission, can be clipped. Retiring a channel
        hides its clips and refuses its videos.
      </p>
      <ErrorLine message={sources.error} />
      <ul className="rows">
        {(sources.data ?? []).map((s) => <SourceRow key={s.id} s={s} onChanged={() => void sources.reload()} />)}
      </ul>
      {sources.data?.length === 0 && <p className="empty">No channels yet. Add the first one below.</p>}

      <form className="panel form" onSubmit={submit}>
        <h2>Add a channel</h2>
        <div className="field-pair">
          <label className="field">
            <span>Platform</span>
            <select value={form.platform} onChange={set("platform")}>
              <option value="twitch">Twitch</option>
              <option value="youtube">YouTube</option>
              <option value="kick">Kick</option>
            </select>
          </label>
          <label className="field">
            <span>Channel handle</span>
            <input value={form.channel_handle} onChange={set("channel_handle")} required placeholder="streamer" autoCapitalize="none" />
          </label>
        </div>
        <label className="field">
          <span>Permission</span>
          <select value={form.clip_policy} onChange={set("clip_policy")}>
            {Object.entries(POLICY_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label className="field">
          <span>Credit line</span>
          <input value={form.required_credit_text} onChange={set("required_credit_text")} placeholder="Clip: twitch.tv/streamer" />
        </label>
        <label className="field">
          <span>Link to the policy or permission</span>
          <input type="url" value={form.policy_url} onChange={set("policy_url")} placeholder="https://" />
        </label>
        <button type="submit" className="primary" disabled={add.busy}>Add channel</button>
        <ErrorLine message={add.error} />
      </form>
    </main>
  );
}
