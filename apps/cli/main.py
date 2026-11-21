"""`clipforge` CLI. Runs as the single seeded owner in the default workspace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from sqlalchemy import select

from clipforge.domain.urls import parse_video_url
from clipforge.domain.values import ClipPolicy, Platform, WorkspaceId
from clipforge.infra.config import get_settings
from clipforge.infra.db import models as m
from clipforge.infra.db.pipeline_store import SqlPipelineStore, stage_overview
from clipforge.infra.db.session import session_scope
from clipforge.infra.db.sources import SourceAdmin
from clipforge.infra.logging import bind_context, configure_logging
from clipforge.infra.registry import get_container
from clipforge.infra.seed import seed as run_seed
from clipforge.ports.infra import Priority
from clipforge.ports.pipeline import PipelineError
from clipforge.scoring.evaluate import EvalResult, evaluate_at_k
from clipforge.services.analyze import AnalyzeResult
from clipforge.services.ingest import SourceNotAllowed

app = typer.Typer(no_args_is_help=True, help="ClipForge: stream VODs in, vertical clips out.")
sources_app = typer.Typer(no_args_is_help=True, help="Manage the source allowlist.")
app.add_typer(sources_app, name="sources")


def _ws() -> WorkspaceId:
    return WorkspaceId(UUID(get_settings().default_workspace_id))


def _actor() -> UUID:
    return UUID(get_settings().default_user_id)


@app.callback()
def _main() -> None:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)


@app.command()
def seed() -> None:
    """Create the single MVP workspace, owner user, and default config (idempotent)."""
    with session_scope() as session:
        ws = run_seed(session, get_settings())
    typer.echo(f"seeded workspace {ws}")


@app.command()
def version() -> None:
    typer.echo("clipforge 0.1.0")


# ------------------------------------------------------------------ ingest
@app.command()
def ingest(
    url: str,
    override: Annotated[
        bool, typer.Option("--override", help="Process even if the channel isn't allowlisted.")
    ] = False,
    queue: Annotated[
        bool, typer.Option("--queue", help="Enqueue fetch+transcribe on the worker.")
    ] = False,
) -> None:
    """Resolve URL, enforce the allowlist, download audio, transcribe (word timestamps)."""
    c = get_container()
    ws = _ws()
    try:
        # Ingest commits on its own so a refusal's audit row and the video row persist even
        # if a later stage fails.
        with session_scope(commit_on_error=True) as session:
            res = c.ingest_pipeline(session, ws, _actor()).ingest(url, override=override)
        video_id = res.video.id
        with bind_context(video_id=video_id, workspace_id=ws):
            typer.echo(f"video {video_id}  [{res.verdict.value}] {res.reason}")
            if queue:
                c.queue.enqueue(
                    "pipeline.fetch",
                    {"workspace_id": str(ws), "video_id": str(video_id)},
                    priority=Priority.NORMAL,
                )
                typer.echo("queued fetch → transcribe on the worker")
                return
            with session_scope(commit_on_error=True) as session:
                a = c.ingest_pipeline(session, ws, _actor()).fetch_audio(video_id)
            typer.echo(f"audio  {a.bytes / 1e6:.1f} MB  {a.blob_key.value}")
            typer.echo("transcribing… (CPU can take ~0.3-1x realtime)")
            with session_scope(commit_on_error=True) as session:
                new = c.ingest_pipeline(session, ws, _actor()).transcribe(video_id)
            typer.echo("transcript " + ("created" if new else "reused from cache"))
            _run_analysis(video_id, None, top=10)
    except SourceNotAllowed as exc:
        typer.secho(f"refused: {exc.reason}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    except PipelineError as exc:
        typer.secho(f"failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


@app.command()
def status(video_id: UUID) -> None:
    """Show per-stage status for a video."""
    with session_scope() as session:
        rows = stage_overview(session, _ws(), video_id)
        t = session.scalars(
            select(m.Transcript).where(
                m.Transcript.workspace_id == _ws(), m.Transcript.video_id == video_id
            )
        ).first()
    if not rows:
        typer.echo("no such video (in this workspace)")
        raise typer.Exit(1)
    for r in rows:
        err = f"  error={r['error'][:120]}" if r["error"] else ""
        typer.echo(f"{r['stage']:<12} {r['status']:<10} attempts={r['attempts']}{err}")
    if t is not None:
        text = " ".join(w["w"] for w in t.words)
        typer.echo(f"\ntranscript: {len(t.words)} words, lang={t.language}, model={t.model}")
        typer.echo(text[:400] + ("…" if len(text) > 400 else ""))


# ------------------------------------------------------------------ analysis (M2)
def _fmt_t(s: float) -> str:
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:04.1f}"


def _print_candidates(res: AnalyzeResult, top: int) -> None:
    typer.echo(f"profile {res.profile.name} v{res.profile.version}  chat: {res.chat_status}")
    typer.echo("signals: " + ", ".join(f"{k}={v}" for k, v in res.signals.items()))
    typer.echo("weights used: " + ", ".join(f"{k}={v}" for k, v in res.weights_used.items()))
    typer.echo(f"combined: {res.combined}   candidates: {len(res.candidates)}")
    cost = f"  est. cost ${res.judge_cost_usd:.4f}" if res.judge_cost_usd else ""
    typer.echo(f"judge: {res.judge_status}{cost}\n")
    names = list(res.weights_used)
    header = f"{'#':>2}  {'start':>10} {'end':>10} {'len':>4}  {'score':>5}  " + "  ".join(
        f"{n[:8]:>8}" for n in names
    )
    typer.echo(header)
    for i, c in enumerate(res.candidates[:top], start=1):
        comps = "  ".join(f"{c.components.get(n, 0.0):>8.2f}" for n in names)
        typer.echo(
            f"{i:>2}  {_fmt_t(c.start_s):>10} {_fmt_t(c.end_s):>10} {c.end_s - c.start_s:>4.0f}"
            f"  {c.score:>5.2f}  {comps}   {c.reason}"
        )


def _parse_kv(items: list[str] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in items or []:
        k, _, v = item.partition("=")
        if not k or not v:
            raise typer.BadParameter(f"expected name=value, got {item!r}")
        out[k.strip()] = float(v)
    return out


def _run_analysis(
    video_id: UUID,
    profile: str | None,
    top: int,
    *,
    persist: bool = True,
    weights: dict[str, float] | None = None,
    params: dict[str, float] | None = None,
    with_judge: bool | None = None,
) -> AnalyzeResult:
    with session_scope(commit_on_error=True) as session:
        pipeline = get_container().analyze_pipeline(session, _ws(), _actor())
        res = pipeline.analyze(
            video_id,
            profile,
            persist=persist,
            weight_overrides=weights,
            param_overrides=params,
            with_judge=with_judge,
        )
    _print_candidates(res, top)
    return res


@app.command()
def analyze(
    video_id: UUID,
    profile: Annotated[str | None, typer.Option(help="Scoring profile name")] = None,
    top: int = 15,
    weight: Annotated[list[str] | None, typer.Option(help="Override: name=value")] = None,
    param: Annotated[list[str] | None, typer.Option(help="Override: name=value")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Don't save candidates")] = False,
    no_judge: Annotated[
        bool, typer.Option("--no-judge", help="Skip the LLM judge even if enabled")
    ] = False,
) -> None:
    """Compute signals, ranked candidate windows, and (if enabled) LLM-judge the top ones."""
    try:
        _run_analysis(
            video_id,
            profile,
            top,
            persist=not dry_run,
            weights=_parse_kv(weight),
            params=_parse_kv(param),
            with_judge=False if no_judge else None,
        )
    except PipelineError as exc:
        typer.secho(f"failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


@app.command("candidates")
def list_candidates_cmd(video_id: UUID, top: int = 20) -> None:
    """Show stored candidates for a video, best first."""
    with session_scope() as session:
        store = SqlPipelineStore(session, _ws())
        rows = store.list_candidates(video_id, limit=top)
    if not rows:
        typer.echo("no candidates yet; run `clipforge analyze`")
        raise typer.Exit(1)
    for c in rows:
        comps = ", ".join(
            f"{k}={v:.2f}"
            for k, v in c.components.items()
            if isinstance(v, float) and k != "peak_s"
        )
        typer.echo(
            f"{_fmt_t(c.start_s)} → {_fmt_t(c.end_s)}  score={c.score:.2f}  "
            f"[{c.status}]  {c.reason}  ({comps})"
        )


@app.command("eval")
def eval_cmd(
    labels: Annotated[Path, typer.Option(help="labels.json with hand-labelled good moments")],
    profile: Annotated[str | None, typer.Option(help="Scoring profile name")] = None,
    k: Annotated[list[int] | None, typer.Option(help="K values (repeatable)")] = None,
    weight: Annotated[list[str] | None, typer.Option(help="Override: name=value")] = None,
    param: Annotated[list[str] | None, typer.Option(help="Override: name=value")] = None,
    with_judge: Annotated[
        bool, typer.Option("--with-judge", help="Include the LLM judge (costs money)")
    ] = False,
) -> None:
    """Replay harness: precision@K / recall@K of the current scoring vs. labels.json.
    Nothing is written to candidates, so profiles and overrides can be A/B-compared."""
    spec = json.loads(labels.read_text(encoding="utf-8"))
    ks = k or [5, 10, 20]
    totals: dict[int, list[EvalResult]] = {kk: [] for kk in ks}
    with session_scope() as session:
        store = SqlPipelineStore(session, _ws())
        for item in spec.get("videos", []):
            parsed = parse_video_url(item["url"])
            if parsed is None:
                typer.secho(f"skip unsupported url {item['url']}", fg=typer.colors.YELLOW)
                continue
            video = store.video_by_external(parsed.platform, parsed.external_id)
            if video is None:
                typer.secho(f"skip (not ingested yet): {item['url']}", fg=typer.colors.YELLOW)
                continue
            res = (
                get_container()
                .analyze_pipeline(session, _ws())
                .analyze(
                    video.id,
                    profile,
                    persist=False,
                    weight_overrides=_parse_kv(weight),
                    param_overrides=_parse_kv(param),
                    with_judge=with_judge,
                )
            )
            ranked = [(c.start_s, c.end_s) for c in res.candidates]
            line = []
            for kk in ks:
                r = evaluate_at_k(ranked, item["good_moments_s"], kk, item.get("tolerance_s", 5))
                totals[kk].append(r)
                line.append(f"P@{kk}={r.precision:.2f} R@{kk}={r.recall:.2f}")
            typer.echo(f"{item['url']}  ({len(ranked)} candidates)  " + "  ".join(line))
    typer.echo("")
    for kk, rs in totals.items():
        if rs:
            p = sum(r.precision for r in rs) / len(rs)
            rc = sum(r.recall for r in rs) / len(rs)
            typer.echo(f"mean over {len(rs)} video(s): P@{kk}={p:.3f}  R@{kk}={rc:.3f}")


# ------------------------------------------------------------------ sources
@sources_app.command("add")
def sources_add(
    platform: Platform,
    handle: str,
    policy: Annotated[ClipPolicy, typer.Option(help="Clip policy")] = ClipPolicy.UNKNOWN,
    credit: Annotated[str, typer.Option(help="Required credit text")] = "",
    policy_url: Annotated[str | None, typer.Option(help="Link to the policy/permission")] = None,
    notes: str = "",
) -> None:
    """Allowlist a channel."""
    with session_scope() as session:
        try:
            s = SourceAdmin(session, _ws(), _actor()).add(
                platform,
                handle,
                policy,
                required_credit_text=credit,
                policy_url=policy_url,
                notes=notes,
            )
        except ValueError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"added {s.id}  {s.platform}/{s.channel_handle}  policy={s.clip_policy}")


@sources_app.command("list")
def sources_list() -> None:
    with session_scope() as session:
        for s in SourceAdmin(session, _ws()).list():
            credit = f'  credit="{s.required_credit_text}"' if s.required_credit_text else ""
            typer.echo(
                f"{s.id}  {s.platform:<8} {s.channel_handle:<24} "
                f"{s.clip_policy:<18} {s.status}{credit}"
            )


@sources_app.command("update")
def sources_update(
    source_id: UUID,
    policy: Annotated[ClipPolicy | None, typer.Option()] = None,
    credit: Annotated[str | None, typer.Option()] = None,
    policy_url: Annotated[str | None, typer.Option()] = None,
    notes: Annotated[str | None, typer.Option()] = None,
) -> None:
    fields = {
        k: v
        for k, v in {
            "clip_policy": policy,
            "required_credit_text": credit,
            "policy_url": policy_url,
            "notes": notes,
        }.items()
        if v is not None
    }
    with session_scope() as session:
        s = SourceAdmin(session, _ws(), _actor()).update(source_id, **fields)
        typer.echo(f"updated {s.id}  policy={s.clip_policy}")


@sources_app.command("retire")
def sources_retire(source_id: UUID, reason: str = "") -> None:
    """Takedown: block the source and hide all of its clips."""
    with session_scope() as session:
        n = SourceAdmin(session, _ws(), _actor()).retire(source_id, reason)
    typer.echo(f"retired; {n} clip(s) hidden")


if __name__ == "__main__":
    app()
