import { useState } from "react";
import { tokenStore } from "./api";
import { useRoute } from "./hooks";
import { ReviewPage } from "./pages/Review";
import { SettingsPage, TokenForm } from "./pages/Settings";
import { SourcesPage } from "./pages/Sources";
import { VideoDetailPage } from "./pages/VideoDetail";
import { VideosPage } from "./pages/Videos";

const NAV = [
  { path: "", label: "Videos" },
  { path: "sources", label: "Sources" },
  { path: "settings", label: "Settings" },
];

export function App() {
  const route = useRoute();
  const [hasToken, setHasToken] = useState(Boolean(tokenStore.get()));
  const [section = "", id, sub, subId] = route;

  let page;
  if (!hasToken) {
    page = (
      <main className="page">
        <h1>Welcome to ClipForge</h1>
        <TokenForm onSaved={() => setHasToken(true)} />
      </main>
    );
  } else if (section === "videos" && id && sub === "c" && subId) {
    page = <ReviewPage key={subId} videoId={id} candidateId={subId} />;
  } else if (section === "videos" && id) {
    page = <VideoDetailPage key={id} videoId={id} />;
  } else if (section === "sources") {
    page = <SourcesPage />;
  } else if (section === "settings") {
    page = <SettingsPage onToken={() => setHasToken(true)} />;
  } else {
    page = <VideosPage />;
  }

  const active = section === "videos" ? "" : section;
  return (
    <>
      <header className="topbar">
        <a className="brand" href="#/">ClipForge</a>
        <nav aria-label="Main">
          {NAV.map((n) => (
            <a key={n.path} href={`#/${n.path}`} aria-current={active === n.path ? "page" : undefined}>
              {n.label}
            </a>
          ))}
        </nav>
      </header>
      {page}
    </>
  );
}
