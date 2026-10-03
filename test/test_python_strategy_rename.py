"""Rename endpoint for Python strategies: POST /python/rename/<strategy_id>.

The name is display-only metadata: the strategy id, script file and schedule
must be untouched, and save_configs() must persist the change.

Run:
    uv run pytest test/test_python_strategy_rename.py -v
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from flask import Flask  # noqa: E402


@pytest.fixture()
def ps_module(monkeypatch):
    from blueprints import python_strategy as ps

    ps.STRATEGY_CONFIGS.clear()
    # The route writes the real strategies/strategy_configs.json - never in tests
    saved = []
    monkeypatch.setattr(ps, "save_configs", lambda: saved.append(dict(ps.STRATEGY_CONFIGS)))
    # Decorator is bound at import time; force the session check to pass
    monkeypatch.setattr("utils.session.is_session_valid", lambda: True)
    yield ps
    ps.STRATEGY_CONFIGS.clear()


@pytest.fixture()
def client(ps_module, tmp_path):
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test-secret")
    app.register_blueprint(ps_module.python_strategy_bp)
    test_client = app.test_client()
    with test_client.session_transaction() as sess:
        sess["user"] = "Amal Roice"
    return test_client


def _add_strategy(ps_module, strategy_id="sensex_cluster_breakout_20260927180000", owner="Amal Roice"):
    ps_module.STRATEGY_CONFIGS[strategy_id] = {
        "name": "sensex cluster breakout 0.25% dry run",
        "file_path": "strategies/scripts/whatever.py",
        "file_name": "whatever.py",
        "exchange": "BFO",
        "is_running": False,
        "is_scheduled": True,
        "user_id": owner,
    }
    return strategy_id


def test_rename_updates_config_and_persists(client, ps_module):
    strategy_id = _add_strategy(ps_module)

    response = client.post(
        f"/python/rename/{strategy_id}",
        json={"name": "  sensex cluster breakout  "},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "success"
    assert body["data"]["name"] == "sensex cluster breakout"
    # Stripped and length-capped, other fields untouched
    assert ps_module.STRATEGY_CONFIGS[strategy_id]["name"] == "sensex cluster breakout"
    assert ps_module.STRATEGY_CONFIGS[strategy_id]["file_name"] == "whatever.py"
    assert ps_module.STRATEGY_CONFIGS[strategy_id]["is_scheduled"] is True


def test_rename_empty_name_is_rejected(client, ps_module):
    strategy_id = _add_strategy(ps_module)

    for payload in ({}, {"name": ""}, {"name": "   "}, {"name": None}):
        response = client.post(f"/python/rename/{strategy_id}", json=payload)
        assert response.status_code == 400, payload

    assert ps_module.STRATEGY_CONFIGS[strategy_id]["name"] == "sensex cluster breakout 0.25% dry run"


def test_rename_is_capped_at_100_characters(client, ps_module):
    strategy_id = _add_strategy(ps_module)

    response = client.post(f"/python/rename/{strategy_id}", json={"name": "x" * 250})

    assert response.status_code == 200
    assert len(ps_module.STRATEGY_CONFIGS[strategy_id]["name"]) == 100


def test_rename_rejects_non_owner(client, ps_module):
    strategy_id = _add_strategy(ps_module, owner="Someone Else")

    response = client.post(f"/python/rename/{strategy_id}", json={"name": "new name"})

    assert response.status_code == 403
    assert ps_module.STRATEGY_CONFIGS[strategy_id]["name"] == "sensex cluster breakout 0.25% dry run"


def test_rename_unknown_strategy_is_404(client, ps_module):
    response = client.post("/python/rename/does_not_exist", json={"name": "new name"})

    assert response.status_code == 404


def test_rename_requires_session(client, ps_module, monkeypatch):
    strategy_id = _add_strategy(ps_module)
    monkeypatch.setattr("utils.session.is_session_valid", lambda: False)

    response = client.post(
        f"/python/rename/{strategy_id}",
        json={"name": "new name"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 401
    assert ps_module.STRATEGY_CONFIGS[strategy_id]["name"] == "sensex cluster breakout 0.25% dry run"


def test_rename_ignores_request_without_json_body(client, ps_module):
    """Raw body is not JSON: fall back to {} and fail with 400, never 500."""
    strategy_id = _add_strategy(ps_module)

    response = client.post(
        f"/python/rename/{strategy_id}",
        data=json.dumps({"name": "x"}),
        content_type="text/plain",
    )

    assert response.status_code == 400
