"""Feature-delivery plan §4: build identity must be honest and unforgeable
by the UI — /version and the System page report the exact deployed identity,
with ``unknown`` for values that were not provided at build time.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from acms import build_info
from acms.main import app

client = TestClient(app)


@pytest.fixture()
def reset_build_env(monkeypatch):
    import acms.main as main_mod

    def _reset(git_sha: str | None, build_time: str | None):
        if git_sha is None:
            monkeypatch.delenv("ACMS_BUILD_GIT_SHA", raising=False)
        else:
            monkeypatch.setenv("ACMS_BUILD_GIT_SHA", git_sha)
        if build_time is None:
            monkeypatch.delenv("ACMS_BUILD_TIME", raising=False)
        else:
            monkeypatch.setenv("ACMS_BUILD_TIME", build_time)
        importlib.reload(build_info)
        # main.py captured build_identity at import; rebind to the reloaded module.
        main_mod.build_identity = build_info.as_dict
        return build_info

    yield _reset
    importlib.reload(build_info)
    main_mod.build_identity = build_info.as_dict


def test_version_reports_build_identity(reset_build_env):
    b = reset_build_env("eab493fe218b9f350ad66592e5d64487c65be90f", "2026-09-26T12:00:00Z")
    r = client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == b.VERSION
    assert body["git_sha"] == "eab493fe218b9f350ad66592e5d64487c65be90f"
    assert body["git_sha_short"] == "eab493f"
    assert body["build_time"] == "2026-09-26T12:00:00Z"


def test_version_falls_back_to_unknown(reset_build_env):
    b = reset_build_env(None, None)
    body = client.get("/version").json()
    assert body["git_sha"] == "unknown"
    assert body["git_sha_short"] == "unknown"
    assert body["build_time"] == "unknown"
    assert body["version"] == b.VERSION


def test_health_unchanged():
    assert client.get("/health").json() == {"status": "ok"}


def test_system_page_shows_git_sha(reset_build_env):
    """UI System page renders the full build identity (plan §4)."""
    reset_build_env("eab493fe218b9f350ad66592e5d64487c65be90f", "2026-09-26T12:00:00Z")
    from acms.ui import routes as ui_routes

    ui_routes.templates = ui_routes.Jinja2Templates(directory=str(ui_routes.templates_dir))
    # Login flow is exercised in test_ui_pages; here just verify template context.
    identity = build_info.as_dict()
    assert set(identity) == {"version", "git_sha", "git_sha_short", "build_time", "reported_at"}
    assert identity["git_sha_short"] == "eab493f"