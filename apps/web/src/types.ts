// Mirrors the /api/v1 response models (apps/api/routers/*.py).

export interface Video {
  id: string;
  url: string;
  platform: string;
  external_id: string;
  title: string;
  duration_s: number | null;
  status: string;
  override_used: boolean;
  source_id: string | null;
  scoring_profile_id: string | null;
  created_at: string;
}

export interface Stage {
  stage: string;
  status: string;
  attempts: number;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface VideoDetail extends Video {
  stages: Stage[];
}

export interface ClipSummary {
  clip_id: string;
  preview: string | null;
  final: string | null;
}

export type ReviewStatus = "pending" | "approved" | "rejected";

export interface Candidate {
  id: string;
  rank: number;
  start_s: number;
  end_s: number;
  score: number;
  components: Record<string, unknown>;
  reason: string;
  status: ReviewStatus;
  title_suggestions: string[];
  hook_text: string;
  judge: Record<string, unknown> | null;
  clip: ClipSummary | null;
}

export interface Render {
  id: string;
  kind: "preview" | "final";
  status: string;
  duration_s: number | null;
  render_seconds: number | null;
  download_url: string | null;
}

export interface Clip {
  id: string;
  candidate_id: string;
  video_id: string;
  start_s: number;
  end_s: number;
  layout: string;
  caption_style_id: string | null;
  caption_override: { text?: string } | null;
  crop_override: Record<string, unknown> | null;
  commentary_text: string;
  credit_text: string;
  renders: Render[];
  render_status: { preview: string | null; final: string | null };
  export_description: string;
  warnings: string[];
}

export interface ReviewResult {
  candidate_id: string;
  status: ReviewStatus;
  changes: string[];
  clip: Clip | null;
  final_render_queued: boolean;
}

export interface CandidatePatch {
  status?: ReviewStatus;
  start_s?: number;
  end_s?: number;
  caption_text?: string;
  layout?: string;
  caption_style_id?: string;
  commentary_text?: string;
}

export interface Source {
  id: string;
  platform: string;
  channel_handle: string;
  channel_id: string | null;
  clip_policy: string;
  policy_url: string | null;
  required_credit_text: string;
  notes: string;
  status: string;
  retired_at: string | null;
  created_at: string;
}

export interface ScoringProfile {
  id: string;
  name: string;
  version: number;
  weights: Record<string, number>;
  params: Record<string, number | boolean>;
  is_default: boolean;
  created_at: string;
}

export interface CaptionStyle {
  id: string;
  name: string;
  version: number;
  spec: Record<string, unknown>;
  created_at: string;
}
