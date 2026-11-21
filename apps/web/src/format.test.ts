import { describe, expect, it } from "vitest";
import { createClient, errorMessage } from "./api";
import { duration, isBusy, latestProfiles, MAX_CLIP_S, MIN_CLIP_S, nudge, signalValues, stageTrack, timecode } from "./format";
import { diffDraft } from "./pages/Review";
import type { ScoringProfile, Stage } from "./types";

const stage = (name: string, status: string, error: string | null = null): Stage => ({
  stage: name, status, attempts: 1, error, started_at: null, finished_at: null,
});

describe("timecode / duration", () => {
  it("formats stream offsets", () => {
    expect(timecode(65)).toBe("1:05.0");
    expect(timecode(3725.44)).toBe("1:02:05.4");
    expect(timecode(59.96)).toBe("1:00.0"); // rounding carries into the minute
    expect(timecode(-3)).toBe("0:00.0");
  });
  it("formats video lengths", () => {
    expect(duration(null)).toBe("");
    expect(duration(20)).toBe("1 min");
    expect(duration(2950)).toBe("49 min");
    expect(duration(7500)).toBe("2 h 5 min");
  });
});

describe("nudge keeps trims the API will accept", () => {
  const r = { start: 100, end: 130 };
  it("moves one edge", () => {
    expect(nudge(r, "start", -2, 3600)).toEqual({ start: 98, end: 130 });
    expect(nudge(r, "end", 0.5, 3600)).toEqual({ start: 100, end: 130.5 });
  });
  it("never goes below the minimum length or outside the video", () => {
    expect(nudge({ start: 100, end: 106 }, "start", 2, null)).toEqual({ start: 100 + 6 - MIN_CLIP_S, end: 106 });
    expect(nudge({ start: 0.5, end: 30 }, "start", -2, null).start).toBe(0);
    expect(nudge({ start: 3590, end: 3599 }, "end", 2, 3600).end).toBe(3600);
  });
  it("never exceeds the maximum length", () => {
    const long = { start: 0, end: MAX_CLIP_S };
    expect(nudge(long, "end", 2, null).end).toBe(MAX_CLIP_S);
  });
});

describe("signals and stages", () => {
  it("keeps only numeric signals, clamped, in a fixed order", () => {
    const vals = signalValues({ components: { audio_energy: 1.4, chat_velocity: 0.5, peak_s: 120, speech_intensity: "x" } });
    expect(vals.map((v) => [v.key, v.value])).toEqual([["chat_velocity", 0.5], ["audio_energy", 1]]);
  });
  it("orders stages and fills in the ones not started", () => {
    const track = stageTrack([stage("transcribe", "running"), stage("fetch", "failed"), stage("fetch", "succeeded")]);
    expect(track.map((s) => `${s.stage}:${s.status}`)).toEqual([
      "fetch:succeeded", "transcribe:running", "signals:waiting", "candidates:waiting",
    ]);
  });
  it("polls until candidates exist or something fails", () => {
    expect(isBusy([])).toBe(true);
    expect(isBusy([stage("fetch", "succeeded")])).toBe(true);
    expect(isBusy([stage("fetch", "failed", "boom")])).toBe(false);
    expect(isBusy([stage("fetch", "succeeded"), stage("candidates", "succeeded")])).toBe(false);
  });
  it("lists the newest version of each profile, default first", () => {
    const p = (name: string, version: number, is_default = false) =>
      ({ id: `${name}${version}`, name, version, is_default, weights: {}, params: {}, created_at: "" }) as ScoringProfile;
    expect(latestProfiles([p("a", 1), p("loud", 1), p("loud", 2, true)]).map((x) => x.id)).toEqual(["loud2", "a1"]);
  });
});

describe("review draft diff", () => {
  const base = { range: { start: 1, end: 30 }, caption: "", intro: "", layout: "auto", styleId: "" };
  it("sends only what changed", () => {
    expect(diffDraft(base, base)).toEqual({});
    expect(diffDraft(base, { ...base, range: { start: 2, end: 30 }, intro: " hi " })).toEqual({ start_s: 2, commentary_text: "hi" });
    expect(diffDraft({ ...base, caption: "old" }, base)).toEqual({ caption_text: "" }); // clearing reverts to transcript
  });
});

describe("api client", () => {
  it("sends the bearer token and JSON body", async () => {
    const calls: [string, RequestInit | undefined][] = [];
    const client = createClient(async (url, init) => {
      calls.push([url, init]);
      return new Response(JSON.stringify({ id: "v1" }), { status: 201 });
    }, () => "tok");
    await client.addVideo("https://www.twitch.tv/videos/1", false, null);
    const [url, init] = calls[0]!;
    expect(url).toBe("/api/v1/videos");
    expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer tok");
    expect(JSON.parse(String(init?.body))).toEqual({ url: "https://www.twitch.tv/videos/1", override: false, scoring_profile_id: null });
  });
  it("turns FastAPI errors into one readable sentence", async () => {
    const client = createClient(async () => new Response(JSON.stringify({ detail: "clip length must be 5-180s" }), { status: 422 }), () => "t");
    await expect(client.getClip("x")).rejects.toThrow("clip length must be 5-180s");
    expect(errorMessage(422, { detail: [{ loc: ["body", "url"], msg: "too short" }] })).toBe("url: too short");
    expect(errorMessage(401, null)).toMatch(/token/);
    expect(errorMessage(500, null)).toBe("Request failed (500)");
  });
});
