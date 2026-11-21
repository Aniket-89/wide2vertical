// Thin typed client for /api/v1. The bearer token lives in localStorage: this is a
// single-owner MVP behind the API's static token (apps/api/deps.py current_principal).
import type {
  Candidate,
  CandidatePatch,
  CaptionStyle,
  Clip,
  Publication,
  PublishInput,
  PublishTarget,
  ReviewResult,
  ScoringProfile,
  Source,
  Video,
  VideoDetail,
} from "./types";

const TOKEN_KEY = "clipforge.token";

export const tokenStore = {
  get: (): string => localStorage.getItem(TOKEN_KEY) ?? "",
  set: (t: string): void => localStorage.setItem(TOKEN_KEY, t.trim()),
  clear: (): void => localStorage.removeItem(TOKEN_KEY),
};

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** FastAPI errors are {"detail": string | [{msg, loc}]}: turn either into one sentence. */
export function errorMessage(status: number, body: unknown): string {
  if (status === 401) return "The API token is missing or wrong. Enter it in Settings.";
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: string; loc?: unknown[] };
    const field = Array.isArray(first.loc) ? first.loc.slice(1).join(".") : "";
    const msg = first.msg ?? "invalid value";
    return field ? `${field}: ${msg}` : msg;
  }
  return `Request failed (${status})`;
}

type Fetch = (input: string, init?: RequestInit) => Promise<Response>;

export function createClient(
  fetchImpl: Fetch = (input, init) => fetch(input, init),
  token: () => string = tokenStore.get,
) {
  async function raw(path: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${token()}`);
    if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    const res = await fetchImpl(`/api/v1${path}`, { ...init, headers });
    if (!res.ok) {
      let body: unknown = null;
      try {
        body = await res.json();
      } catch {
        // non-JSON error body: fall back to the status code
      }
      throw new ApiError(res.status, errorMessage(res.status, body));
    }
    return res;
  }

  async function json<T>(path: string, init: RequestInit = {}): Promise<T> {
    return (await raw(path, init)).json() as Promise<T>;
  }

  const post = <T>(path: string, body: unknown = {}) =>
    json<T>(path, { method: "POST", body: JSON.stringify(body) });
  const patch = <T>(path: string, body: unknown) =>
    json<T>(path, { method: "PATCH", body: JSON.stringify(body) });

  return {
    me: () => json<{ user_id: string; workspace_id: string; role: string }>("/me"),

    listVideos: () => json<Video[]>("/videos"),
    getVideo: (id: string) => json<VideoDetail>(`/videos/${id}`),
    addVideo: (url: string, override: boolean, scoringProfileId: string | null) =>
      post<Video>("/videos", { url, override, scoring_profile_id: scoringProfileId }),
    reanalyze: (id: string, scoringProfileId: string) =>
      post<Video>(`/videos/${id}/analyze`, { scoring_profile_id: scoringProfileId }),
    candidates: (videoId: string) => json<Candidate[]>(`/videos/${videoId}/candidates`),

    review: (candidateId: string, body: CandidatePatch) =>
      patch<ReviewResult>(`/candidates/${candidateId}`, body),
    preview: (candidateId: string) => post<Clip>(`/candidates/${candidateId}/preview`),
    getClip: (clipId: string) => json<Clip>(`/clips/${clipId}`),
    render: (clipId: string, kind: "preview" | "final") =>
      post<Clip>(`/clips/${clipId}/render`, { kind }),
    /** Media needs the bearer header, which a video element's src cannot send: fetch a blob. */
    renderBlob: async (clipId: string, kind: "preview" | "final") =>
      (await raw(`/clips/${clipId}/download?kind=${kind}`)).blob(),

    listSources: () => json<Source[]>("/sources"),
    addSource: (s: Partial<Source>) => post<Source>("/sources", s),
    updateSource: (id: string, body: Partial<Source>) => patch<Source>(`/sources/${id}`, body),
    retireSource: (id: string, reason: string) =>
      post<{ clips_hidden: number }>(`/sources/${id}/retire`, { reason }),

    listProfiles: () => json<ScoringProfile[]>("/scoring-profiles"),
    addProfile: (p: Pick<ScoringProfile, "name" | "weights" | "params" | "is_default">) =>
      post<ScoringProfile>("/scoring-profiles", p),
    listStyles: () => json<CaptionStyle[]>("/caption-styles"),
    addStyle: (s: Pick<CaptionStyle, "name" | "spec">) => post<CaptionStyle>("/caption-styles", s),

    listTargets: () => json<PublishTarget[]>("/publish-targets"),
    publish: (clipId: string, body: PublishInput) => post<Publication>(`/clips/${clipId}/publish`, body),
    listPublications: (clipId?: string) =>
      json<Publication[]>(clipId ? `/publications?clip_id=${clipId}` : "/publications"),
    cancelPublication: (id: string) => post<Publication>(`/publications/${id}/cancel`),
  };
}

export type Api = ReturnType<typeof createClient>;
export const api = createClient();
