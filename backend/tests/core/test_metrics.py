"""GET /api/v1/metrics: Prometheus text from the database, behind the bearer token."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app.core.config import Settings
from backend.app.credentials.memory_store import InMemoryCredentialStore
from backend.app.main import create_app

LINE = re.compile(r'^[a-z_]+(\{([a-z_]+="(?:[^"\\]|\\.)*",?)*\})? -?[0-9.e+-]+$')


def test_metrics_need_the_token_and_reflect_the_records(tmp_path: Path) -> None:
    app = create_app(settings=Settings(database_path=tmp_path / "s" / "db.sqlite3"),
                     credential_store=InMemoryCredentialStore())
    with TestClient(app) as client:
        assert client.get("/api/v1/metrics").status_code == 401
        client.headers["Authorization"] = f"Bearer {app.state.api_token}"
        empty = client.get("/api/v1/metrics")
        assert empty.status_code == 200
        assert empty.headers["content-type"].startswith("text/plain; version=0.0.4")
        assert "sentinel_changes 0" in empty.text

        database = app.state.database
        from backend.tests.passport.test_builder import _seed_change
        change = _seed_change(database)
        now = datetime.now(UTC).isoformat()
        with database.connection() as connection:
            for status, payload in (("PASSED", {}), ("ERROR", {"limitations": [
                    "The agent did not start (LINUX_SANDBOX_VERIFICATION_FAILED): mismatch"]}),
                    ("ERROR", {"limitations": ["The agent did not start: other"]})):
                connection.execute(
                    "INSERT INTO agent_runs (id, change_id, status, payload_json, started_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (str(uuid4()), str(change.id), status, json.dumps(payload), now))
        document = {"schema": "sentinel-eval-run/1", "id": str(uuid4()), "suite": "s",
                    "config": {"name": 'quote"d', "agent": "mock"}, "k": 1,
                    "started_at": now, "completed_at": now,
                    "results": [{"task_id": "a", "attempt": 1, "status": "PASSED",
                                 "wall_seconds": 1.0, "agent": None, "hidden": None}]}
        assert client.post("/api/v1/evals/runs", json=document).status_code == 201
        text = client.get("/api/v1/metrics").text
    assert "sentinel_changes 1" in text
    assert 'sentinel_agent_runs_total{status="ERROR"} 2' in text
    assert 'sentinel_boundary_failures_total{code="LINUX_SANDBOX_VERIFICATION_FAILED"} 1' in text
    assert "sentinel_eval_runs_total 1" in text
    assert re.search(r'sentinel_eval_pass_rate\{agent="mock",config="quote\\"d",run_id="[^"]+"\} 1\.0',
                     text)
    for line in text.splitlines():
        assert line.startswith("# ") or LINE.match(line), line
