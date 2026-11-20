"""`clipforge` CLI. Runs as the single seeded owner in the default workspace."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import typer
from sqlalchemy import select

from clipforge.domain.values import ClipPolicy, Platform, WorkspaceId
from clipforge.infra.config import get_settings
from clipforge.infra.db import models as m
from clipforge.infra.db.pipeline_store import stage_overview
from clipforge.infra.db.session import session_scope
from clipforge.infra.db.sources import SourceAdmin
from clipforge.infra.logging import bind_context, configure_logging
from clipforge.infra.registry import get_container
from clipforge.infra.seed import seed as run_seed
from clipforge.ports.infra import Priority
from clipforge.ports.pipeline import PipelineError
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
