from __future__ import annotations

import base64
import os
import textwrap
from pathlib import Path

import pytest

DEVICE_NAME = "kindle-test"
DEVICE_TOKEN = "not-a-real-token-only-used-by-the-test-suite"
WEB_PASSWORD = "correct horse battery staple correct"


@pytest.fixture(scope="session")
def fixture_inbox(tmp_path_factory) -> Path:
    inbox = tmp_path_factory.mktemp("inbox")
    (inbox / "notes").mkdir()
    (inbox / "notes" / "audit.md").write_text(
        textwrap.dedent(
            """\
            ---
            title: VPS2 capacity audit & review
            summary: Read-only survey. Contains an ampersand & a <bracket>.
            tags: [infra, vps]
            ---

            Leading prose before the first heading.

            # Findings

            | host | ram | disk | swap | vhosts | services | uptime | kernel | notes |
            |---|---|---|---|---|---|---|---|---|
            | vps2 | 2.2G | 26G | 2303M | 9 | 4 | 41d | 4.18 | shared semi-prod box |

            | a | b |
            |---|---|
            | 1 | 2 |

            ```python
            def a_really_long_function_name(first_argument, second_argument, third_argument, fourth=None):
                return {"key": first_argument, "other": second_argument, "third": third_argument}
            ```

            ![missing](https://example.com/remote.png)

            # Second section

            Done.
            """
        ),
        encoding="utf-8",
    )
    (inbox / "notes" / "plain.md").write_text("Just prose, no front matter.\n", "utf-8")
    return inbox


@pytest.fixture(scope="session")
def cfg(fixture_inbox, tmp_path_factory):
    from kindle_hub import auth
    from kindle_hub.config import Config

    root = tmp_path_factory.mktemp("hub")
    os.environ.update(
        HUB_INBOX_DIR=str(fixture_inbox),
        HUB_LIBRARY_DIR=str(root / "library"),
        HUB_STATE_DIR=str(root / "state"),
        HUB_PUBLIC_ORIGIN="https://kindle.example.invalid",
        HUB_COOKIE_SECURE="1",
        HUB_USE_XACCEL="1",
        HUB_SESSION_SIGNING_KEY="test-key-that-is-long-enough-to-pass-validation",
        HUB_WEB_PASSWORD_HASH=auth.hash_password(WEB_PASSWORD),
        HUB_OPDS_TOKENS=f"{DEVICE_NAME}:{auth.token_digest(DEVICE_TOKEN)}",
        HUB_OPDS_PAGE_SIZE="1",  # forces pagination with two fixtures
        HUB_LOG_LEVEL="WARNING",
    )
    config = Config.from_env()
    config.ensure_dirs()
    return config


@pytest.fixture(scope="session")
def app(cfg):
    from kindle_hub.app import create_app
    from kindle_hub.builder import sweep
    from kindle_hub.catalog.store import Store

    store = Store(cfg.db_path)
    store.migrate()
    sweep(cfg, store)
    return create_app(cfg=cfg, start_builder=False)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def basic_auth() -> dict[str, str]:
    raw = f"{DEVICE_NAME}:{DEVICE_TOKEN}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
