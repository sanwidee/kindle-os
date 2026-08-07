"""Environment-driven configuration.

Everything the app needs comes from the process environment. Nothing is read
from a file inside the repo, and no secret has a default value -- if a secret
is missing the app refuses to start rather than falling back to something
guessable.

On the box the secrets arrive via systemd EnvironmentFile= (or the compose
`env_file:`) pointing at /etc/artifact-hub/secrets.env, root:artifacthub 0640.
See hub/.env.example for the full list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised at startup when the environment is incomplete or nonsensical."""


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise ConfigError(f"required environment variable {name} is not set")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    # --- paths -----------------------------------------------------------
    inbox_dir: Path
    library_dir: Path
    state_dir: Path

    # --- identity --------------------------------------------------------
    public_origin: str  # e.g. https://kindle.weedlabs.online -- no trailing slash
    site_title: str

    # --- web door (browser) ----------------------------------------------
    web_password_hash: str  # argon2id PHC string. NEVER the password itself.
    session_signing_key: str
    cookie_secure: bool
    session_idle_days: int
    session_absolute_days: int
    login_fail_delay_seconds: float

    # --- kindle door (OPDS) ----------------------------------------------
    # "name:sha256hex,name2:sha256hex". Plaintext tokens live on the Kindle
    # and in San's password manager, never here and never on the server.
    opds_token_seed: str

    # --- serving ---------------------------------------------------------
    xaccel_prefix: str  # nginx `internal` location that aliases library_dir
    use_xaccel: bool  # False for local dev, where there is no nginx in front
    opds_absolute_urls: bool
    opds_page_size: int

    # --- conversion ------------------------------------------------------
    code_columns: int
    code_max_lines: int
    table_columns: int
    eink_max_width: int
    ebooks_dir: Path
    strip_emoji: bool
    stable_filenames: bool
    renderer_version: str  # bump to force a full rebuild after converter changes

    # --- builder ---------------------------------------------------------
    build_interval_seconds: int
    build_on_start: bool
    gc_grace_days: int

    @property
    def db_path(self) -> Path:
        return self.state_dir / "hub.db"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "builder.lock"

    @property
    def cookie_name(self) -> str:
        # The __Host- prefix is only legal on a Secure cookie. Over plain HTTP
        # (local dev) browsers silently drop it, which looks like a broken
        # login, so fall back to a plain name when cookie_secure is off.
        return "__Host-session" if self.cookie_secure else "hub_session"

    @classmethod
    def from_env(cls) -> Config:
        cfg = cls(
            inbox_dir=Path(_env("HUB_INBOX_DIR", "/data/inbox")),
            library_dir=Path(_env("HUB_LIBRARY_DIR", "/data/library")),
            state_dir=Path(_env("HUB_STATE_DIR", "/data/state")),
            public_origin=_env("HUB_PUBLIC_ORIGIN").rstrip("/"),
            site_title=_env("HUB_SITE_TITLE", "kindle-os"),
            web_password_hash=_env("HUB_WEB_PASSWORD_HASH"),
            session_signing_key=_env("HUB_SESSION_SIGNING_KEY"),
            cookie_secure=_env_bool("HUB_COOKIE_SECURE", True),
            session_idle_days=_env_int("HUB_SESSION_IDLE_DAYS", 30),
            session_absolute_days=_env_int("HUB_SESSION_ABSOLUTE_DAYS", 90),
            login_fail_delay_seconds=float(os.environ.get("HUB_LOGIN_FAIL_DELAY", "1.0")),
            opds_token_seed=os.environ.get("HUB_OPDS_TOKENS", ""),
            xaccel_prefix=_env("HUB_XACCEL_PREFIX", "/_epub/"),
            use_xaccel=_env_bool("HUB_USE_XACCEL", True),
            opds_absolute_urls=_env_bool("HUB_OPDS_ABSOLUTE_URLS", True),
            opds_page_size=_env_int("HUB_OPDS_PAGE_SIZE", 25),
            code_columns=_env_int("HUB_CODE_COLUMNS", 64),
            code_max_lines=_env_int("HUB_CODE_MAX_LINES", 400),
            table_columns=_env_int("HUB_TABLE_COLUMNS", 64),
            eink_max_width=_env_int("HUB_EINK_MAX_WIDTH", 1236),
            ebooks_dir=Path(_env("HUB_EBOOKS_DIR", "/data/ebooks")),
            strip_emoji=_env_bool("HUB_STRIP_EMOJI", True),
            stable_filenames=_env_bool("HUB_STABLE_FILENAMES", False),
            renderer_version=_env("HUB_RENDERER_VERSION", "1"),
            build_interval_seconds=_env_int("HUB_BUILD_INTERVAL", 60),
            build_on_start=_env_bool("HUB_BUILD_ON_START", True),
            gc_grace_days=_env_int("HUB_GC_GRACE_DAYS", 7),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.public_origin.startswith(("http://", "https://")):
            raise ConfigError("HUB_PUBLIC_ORIGIN must include the scheme")
        if self.public_origin.startswith("http://") and self.cookie_secure:
            raise ConfigError(
                "HUB_COOKIE_SECURE=1 with an http:// origin will never work: "
                "the browser drops the cookie. Use https, or set "
                "HUB_COOKIE_SECURE=0 for local dev only."
            )
        if not self.web_password_hash.startswith("$argon2id$"):
            raise ConfigError(
                "HUB_WEB_PASSWORD_HASH must be an argon2id PHC string "
                "(generate with: python -m kindle_hub hash-password)"
            )
        if len(self.session_signing_key) < 32:
            raise ConfigError("HUB_SESSION_SIGNING_KEY must be at least 32 characters")
        if not self.xaccel_prefix.startswith("/") or not self.xaccel_prefix.endswith("/"):
            raise ConfigError("HUB_XACCEL_PREFIX must start and end with /")
        if self.session_idle_days > self.session_absolute_days:
            raise ConfigError("session idle timeout cannot exceed the absolute cap")
        for path in (self.inbox_dir, self.library_dir, self.state_dir):
            if not path.is_absolute():
                raise ConfigError(f"{path} must be an absolute path")

    def ensure_dirs(self) -> None:
        """library/ and state/ are ours to create. inbox/ is written by rsync
        from outside the container and is mounted read-only, so we only check
        that it exists."""
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if not self.inbox_dir.is_dir():
            raise ConfigError(f"inbox directory does not exist: {self.inbox_dir}")
