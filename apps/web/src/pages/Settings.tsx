import { useState, type FormEvent } from "react";
import { api, tokenStore } from "../api";
import { ErrorLine } from "../components/bits";
import { latestProfiles, OTHER_WEIGHT_LABELS, profileLabel, SIGNALS } from "../format";
import { useAction, useLoad } from "../hooks";
import type { ScoringProfile } from "../types";

export function TokenForm({ onSaved }: { onSaved: () => void }) {
  const [token, setToken] = useState(tokenStore.get());
  const check = useAction();
  async function submit(e: FormEvent) {
    e.preventDefault();
    tokenStore.set(token);
    if (await check.run(() => api.me())) onSaved();
  }
  return (
    <form className="panel form" onSubmit={submit}>
      <h2>API token</h2>
      <p className="muted">The value of CLIPFORGE_API_TOKEN from your .env. It stays on this device.</p>
      <input type="password" autoComplete="current-password" value={token} onChange={(e) => setToken(e.target.value)} required aria-label="API token" />
      <button type="submit" className="primary" disabled={check.busy}>{check.busy ? "Checking…" : "Save token"}</button>
      <ErrorLine message={check.error} />
    </form>
  );
}

function ProfileEditor({ base, onSaved }: { base: ScoringProfile | undefined; onSaved: () => void }) {
  const [name, setName] = useState(base?.name ?? "custom");
  const [weights, setWeights] = useState<Record<string, number>>(base?.weights ?? {});
  const [params, setParams] = useState(JSON.stringify(base?.params ?? {}, null, 2));
  const [isDefault, setIsDefault] = useState(false);
  const act = useAction();

  async function submit(e: FormEvent) {
    e.preventDefault();
    let parsed: Record<string, number | boolean>;
    try {
      parsed = JSON.parse(params) as Record<string, number | boolean>;
    } catch {
      void act.run(() => Promise.reject(new Error("Params must be valid JSON, e.g. {\"min_clip_s\": 20}")));
      return;
    }
    if (await act.run(() => api.addProfile({ name, weights, params: parsed, is_default: isDefault }))) onSaved();
  }

  const extra = Object.keys(weights).filter((k) => !SIGNALS.some((s) => s.key === k));
  return (
    <form className="form" onSubmit={submit}>
      <label className="field">
        <span>Name <span className="muted">(an existing name saves a new version)</span></span>
        <input value={name} onChange={(e) => setName(e.target.value)} required pattern="[A-Za-z0-9 _.\-]+" />
      </label>
      <fieldset className="weights">
        <legend>Signal weights <span className="muted">(only the ratios matter)</span></legend>
        {[...SIGNALS.map((s) => s.key), ...extra].map((k) => {
          const sig = SIGNALS.find((s) => s.key === k);
          const v = weights[k] ?? 0;
          return (
            <label key={k} className="weight">
              <span style={{ color: sig?.color }}>{sig?.label ?? OTHER_WEIGHT_LABELS[k] ?? k}</span>
              <input type="range" min={0} max={1} step={0.05} value={v}
                onChange={(e) => setWeights({ ...weights, [k]: Number(e.target.value) })} />
              <span className="mono">{v.toFixed(2)}</span>
            </label>
          );
        })}
      </fieldset>
      <label className="field">
        <span>Parameters (JSON)</span>
        <textarea rows={8} className="mono" value={params} onChange={(e) => setParams(e.target.value)} spellCheck={false} />
      </label>
      <label className="override">
        <input type="checkbox" checked={isDefault} onChange={(e) => setIsDefault(e.target.checked)} />
        <span>Use for new videos by default</span>
      </label>
      <button type="submit" className="primary" disabled={act.busy}>Save profile</button>
      <ErrorLine message={act.error} />
    </form>
  );
}

const STYLE_TEMPLATE = {
  font: "Montserrat ExtraBold",
  size: 72,
  primary: "#FFFFFF",
  highlight: "#FFE600",
  outline: "#000000",
  outline_px: 6,
  position: "lower_third",
  words_per_line: 3,
};

function StyleEditor({ onSaved }: { onSaved: () => void }) {
  const [name, setName] = useState("my style");
  const [spec, setSpec] = useState<Record<string, string | number>>(STYLE_TEMPLATE);
  const act = useAction();
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (await act.run(() => api.addStyle({ name, spec }))) onSaved();
  }
  const color = (k: string) => (
    <label className="field">
      <span>{{ primary: "Text", highlight: "Highlighted word", outline: "Outline" }[k]}</span>
      <input type="color" value={String(spec[k])} onChange={(e) => setSpec({ ...spec, [k]: e.target.value.toUpperCase() })} />
    </label>
  );
  return (
    <form className="form" onSubmit={submit}>
      <label className="field">
        <span>Name</span>
        <input value={name} onChange={(e) => setName(e.target.value)} required pattern="[A-Za-z0-9 _.\-]+" />
      </label>
      <div className="caption-sample" style={{ color: String(spec.primary), WebkitTextStroke: `1px ${spec.outline}` }}>
        THIS IS <span style={{ color: String(spec.highlight) }}>INSANE</span>
      </div>
      <div className="field-pair">{color("primary")}{color("highlight")}{color("outline")}</div>
      <div className="field-pair">
        <label className="field">
          <span>Size</span>
          <input type="number" min={20} max={200} value={spec.size} onChange={(e) => setSpec({ ...spec, size: Number(e.target.value) })} />
        </label>
        <label className="field">
          <span>Words per line</span>
          <input type="number" min={1} max={8} value={spec.words_per_line} onChange={(e) => setSpec({ ...spec, words_per_line: Number(e.target.value) })} />
        </label>
        <label className="field">
          <span>Position</span>
          <select value={spec.position} onChange={(e) => setSpec({ ...spec, position: e.target.value })}>
            <option value="lower_third">Lower third</option>
            <option value="middle">Middle</option>
            <option value="top">Top</option>
          </select>
        </label>
      </div>
      <button type="submit" className="primary" disabled={act.busy}>Save style</button>
      <ErrorLine message={act.error} />
    </form>
  );
}

export function SettingsPage({ onToken }: { onToken: () => void }) {
  const profiles = useLoad(() => api.listProfiles(), []);
  const styles = useLoad(() => api.listStyles(), []);
  const [baseId, setBaseId] = useState("");
  const latest = latestProfiles(profiles.data ?? []);
  const base = latest.find((p) => p.id === baseId) ?? latest[0];

  return (
    <main className="page">
      <h1>Settings</h1>
      <TokenForm onSaved={onToken} />

      <section className="panel">
        <h2>Scoring profiles</h2>
        <ul className="plain">
          {latest.map((p) => <li key={p.id}>{profileLabel(p)}</li>)}
        </ul>
        <ErrorLine message={profiles.error} />
        {base && (
          <>
            <label className="field">
              <span>Start from</span>
              <select value={base.id} onChange={(e) => setBaseId(e.target.value)}>
                {latest.map((p) => <option key={p.id} value={p.id}>{profileLabel(p)}</option>)}
              </select>
            </label>
            <ProfileEditor key={base.id} base={base} onSaved={() => void profiles.reload()} />
          </>
        )}
      </section>

      <section className="panel">
        <h2>Caption styles</h2>
        <ul className="plain">
          {(styles.data ?? []).map((s) => <li key={s.id}>{s.name} v{s.version}</li>)}
        </ul>
        <StyleEditor onSaved={() => void styles.reload()} />
      </section>
    </main>
  );
}
