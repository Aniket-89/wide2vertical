import { api } from "../api";
import { ErrorLine } from "../components/bits";
import { isActive, PublicationRow } from "../components/publish";
import { useLoad } from "../hooks";

export function PostsPage() {
  const pubs = useLoad(() => api.listPublications(), [], { pollMs: 5000, poll: (ps) => ps.some(isActive) });
  const targets = useLoad(() => api.listTargets(), []);
  const all = pubs.data ?? [];
  const upcoming = all.filter((p) => p.status === "scheduled" || isActive(p));
  const done = all.filter((p) => !upcoming.includes(p));

  return (
    <main className="page">
      <h1>Posts</h1>
      <p className="muted">
        {targets.data?.length
          ? `Connected: ${targets.data.map((t) => t.account_label).join(", ")}. View counts refresh every few hours.`
          : "No channel connected yet. Run clipforge publish connect-youtube on the ClipForge computer."}
      </p>
      <ErrorLine message={pubs.error} />
      {pubs.data && all.length === 0 && (
        <p className="empty">Nothing posted yet. Approve a clip, then publish it from its review screen.</p>
      )}
      {upcoming.length > 0 && (
        <>
          <h2>Coming up</h2>
          <ul className="rows pubs">{upcoming.map((p) => <PublicationRow key={p.id} p={p} onChanged={() => void pubs.reload()} />)}</ul>
        </>
      )}
      {done.length > 0 && (
        <>
          <h2>History</h2>
          <ul className="rows pubs">{done.map((p) => <PublicationRow key={p.id} p={p} onChanged={() => void pubs.reload()} />)}</ul>
        </>
      )}
    </main>
  );
}
