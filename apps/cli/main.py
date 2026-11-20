"""`clipforge` CLI."""

from __future__ import annotations

import typer

from clipforge.infra.config import get_settings
from clipforge.infra.db.session import session_scope
from clipforge.infra.logging import configure_logging
from clipforge.infra.seed import seed as run_seed

app = typer.Typer(no_args_is_help=True, help="ClipForge: stream VODs in, vertical clips out.")


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


if __name__ == "__main__":
    app()
