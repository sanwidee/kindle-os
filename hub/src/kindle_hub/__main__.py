"""CLI: python -m kindle_hub <command>

  serve          run the dev server (Flask's, not for production)
  build          one inbox sweep, then exit
  doctor         print what the process can see, and refuse to lie about it
  hash-password  produce the argon2id hash for HUB_WEB_PASSWORD_HASH
  mint-token     produce a device token and its sha256 digest
  token          add / remove / list device tokens in the database
  logout-all     wipe every server-side session

hash-password and mint-token are meant to be run on your own machine. Neither
needs the server, and neither should put a plaintext secret in the server's
shell history or process table.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from . import auth
from .app import configure_logging, create_app, seed_device_tokens
from .builder import BuildLock, sweep
from .catalog.store import Store
from .config import Config, ConfigError


def _load() -> tuple[Config, Store]:
    cfg = Config.from_env()
    cfg.ensure_dirs()
    store = Store(cfg.db_path)
    store.migrate()
    return cfg, store


@click.group()
def cli() -> None:
    configure_logging()


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8090, show_default=True)
def serve(host: str, port: int) -> None:
    """Development server. Production runs gunicorn behind nginx."""
    app = create_app()
    click.echo(f"dev server on http://{host}:{port} (not for production)")
    app.run(host=host, port=port, debug=False, threaded=True)


@cli.command()
@click.option("--force", is_flag=True, help="rebuild every document, ignoring hashes")
def build(force: bool) -> None:
    """One inbox sweep, then exit."""
    cfg, store = _load()
    seed_device_tokens(cfg, store)
    lock = BuildLock(cfg.lock_path)
    if not lock.acquire():
        click.echo("another process owns the build lock (the service is running)",
                   err=True)
        sys.exit(1)
    try:
        result = sweep(cfg, store, force=force)
    finally:
        lock.release()
    click.echo(
        f"scanned={result.scanned} built={result.built} deleted={result.deleted} "
        f"errors={result.errors} in {result.seconds:.2f}s"
    )
    sys.exit(1 if result.errors else 0)


@cli.command()
def doctor() -> None:
    """Print what this process can actually see. No guessing, no defaults
    presented as facts."""
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        click.echo(f"config error: {exc}", err=True)
        sys.exit(2)

    checks: list[tuple[str, str, bool]] = []

    def check(label: str, value: object, ok: bool) -> None:
        checks.append((label, str(value), ok))

    check("python", sys.version.split()[0], sys.version_info >= (3, 10))
    check("public origin", cfg.public_origin, cfg.public_origin.startswith("https://"))
    check("cookie", cfg.cookie_name, cfg.cookie_secure)
    check("inbox", cfg.inbox_dir, os.access(cfg.inbox_dir, os.R_OK))
    check("library", cfg.library_dir, os.access(cfg.library_dir, os.W_OK))
    check("state", cfg.state_dir, os.access(cfg.state_dir, os.W_OK))
    check("x-accel", f"{cfg.use_xaccel} -> {cfg.xaccel_prefix}", True)

    # SELinux is Disabled on the target box today. If that ever changes,
    # proxy_pass, the bind mounts, and X-Accel-Redirect all break at once, so
    # doctor refuses to report healthy rather than letting it surprise you.
    selinux = "unknown"
    try:
        import subprocess

        selinux = subprocess.run(
            ["getenforce"], capture_output=True, text=True, timeout=2
        ).stdout.strip() or "unknown"
    except Exception:
        pass
    check("selinux", selinux, selinux in ("Disabled", "unknown"))

    try:
        store = Store(cfg.db_path)
        store.migrate()
        check("database", f"{cfg.db_path} ({store.count()} documents)", True)
        check("devices", ", ".join(r["name"] for r in store.device_tokens()) or "none",
              bool(store.device_tokens()))
    except Exception as exc:
        check("database", f"FAILED: {exc}", False)

    width = max(len(label) for label, _, _ in checks)
    failures = 0
    for label, value, ok in checks:
        mark = "ok  " if ok else "FAIL"
        failures += 0 if ok else 1
        click.echo(f"[{mark}] {label.ljust(width)}  {value}")
    sys.exit(1 if failures else 0)


@cli.command("hash-password")
def hash_password() -> None:
    """Produce the argon2id hash for HUB_WEB_PASSWORD_HASH.

    Use a 25+ character passphrase from a password manager. Password strength
    is the actual control here; the rate limiter only buys time.
    """
    password = click.prompt("password", hide_input=True, confirmation_prompt=True)
    if len(password) < 16:
        click.echo(
            "warning: shorter than 16 characters. This is the only human "
            "credential on the system; use a passphrase from a manager.",
            err=True,
        )
    click.echo("")
    click.echo("HUB_WEB_PASSWORD_HASH='" + auth.hash_password(password) + "'")


@cli.command("mint-token")
@click.argument("name")
def mint_token(name: str) -> None:
    """Generate a device token for the Kindle door.

    Prints the plaintext ONCE. Type it into KOReader, save it in the password
    manager, and put only the digest in HUB_OPDS_TOKENS. The server never needs
    the plaintext.

    Treat loss of physical control of the Kindle as compromise of this token:
    KOReader stores OPDS credentials in plaintext on a device that mounts as
    unencrypted USB storage. Rotation is one row.
    """
    token = auth.new_device_token()
    click.echo("")
    click.echo(f"  username (in KOReader):  {name}")
    click.echo(f"  password (in KOReader):  {token}")
    click.echo("")
    click.echo(f"HUB_OPDS_TOKENS='{name}:{auth.token_digest(token)}'")
    click.echo("")
    click.echo("The plaintext above is not stored anywhere. Copy it now.")


@cli.group()
def token() -> None:
    """Manage device tokens in the database."""


@token.command("list")
def token_list() -> None:
    _, store = _load()
    rows = [dict(r) for r in store.device_tokens()]
    click.echo(json.dumps(rows, indent=2))


@token.command("add")
@click.argument("name")
@click.argument("digest")
def token_add(name: str, digest: str) -> None:
    """Register a device by NAME and sha256 DIGEST (not the token itself)."""
    if len(digest) != 64:
        raise click.BadParameter("expected a 64-character sha256 hex digest")
    _, store = _load()
    store.upsert_device_token(name, digest.lower())
    click.echo(f"registered {name}")


@token.command("rm")
@click.argument("name")
@click.option("--reason", default=None, help="Recorded with the tombstone.")
def token_rm(name: str, reason: str | None) -> None:
    """Revoke a device. Takes effect on the next request, and survives restarts.

    Revocation writes a tombstone as well as deleting the row, so the digest
    still sitting in HUB_OPDS_TOKENS cannot re-register it at the next
    startup. Clean the entry out of secrets.env anyway.
    """
    _, store = _load()
    if not store.revoke_device_token(name, reason):
        click.echo(f"no such device token: {name}", err=True)
        raise SystemExit(1)
    click.echo(f"revoked {name} (tombstoned; safe across restarts)")
    click.echo("next: remove this entry from HUB_OPDS_TOKENS in secrets.env")


@token.command("unrevoke")
@click.argument("name")
def token_unrevoke(name: str) -> None:
    """Clear tombstones for a name so it can be registered again.

    Deliberately manual. Reusing a revoked device name should be something you
    chose, never something a restart did for you.
    """
    _, store = _load()
    n = store.unrevoke_device_name(name)
    click.echo(f"cleared {n} tombstone(s) for {name}")


@token.command("log")
@click.option("--name", default=None, help="Limit to one device.")
@click.option("--limit", default=50, show_default=True)
def token_log(name: str | None, limit: int) -> None:
    """Append-only access log for device tokens.

    This is the compensating control for the two Kindle risks the design
    accepts (MITM on an unvalidated TLS connection, token theft off the USB
    partition). Look here for requests you do not recognise.
    """
    _, store = _load()
    rows = store.device_access_log(name, limit)
    if not rows:
        click.echo("no access recorded")
        return
    for r in rows:
        click.echo(
            f"{r['at']}  {r['outcome']:6}  {r['name']:16}  "
            f"{r['ip']:>15}  {r['path'] or ''}"
        )


@cli.command("logout-all")
def logout_all() -> None:
    """Wipe every server-side session. The kill switch for a lost device."""
    _, store = _load()
    click.echo(f"dropped {store.drop_all_sessions()} session(s)")


@cli.command("render-one")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--out", type=click.Path(path_type=Path), default=None,
              help="write the EPUB here instead of the library")
def render_one(path: Path, out: Path | None) -> None:
    """Convert a single markdown file. Useful for eyeballing an EPUB on the
    Kindle over USB before any server exists."""
    from .convert import pipeline
    from .ingest import scanner

    cfg = Config.from_env()
    source = scanner.SourceFile(
        relpath=path.name,
        path=path,
        source_sha=__import__("hashlib").sha256(path.read_bytes()).hexdigest(),
        size=path.stat().st_size,
    )
    result = pipeline.build(cfg, source)
    built = cfg.library_dir / result.document.doc_id / result.document.epub_name
    if out:
        out.write_bytes(built.read_bytes())
        click.echo(str(out))
    else:
        click.echo(str(built))


if __name__ == "__main__":
    cli()
