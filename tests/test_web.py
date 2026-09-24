import json
import threading
import urllib.error
import urllib.request

import pytest

from lab.db import now
from lab.web import action_argv, make_server


@pytest.fixture
def web(cfg, db, labdir):
    srv = make_server(cfg, str(labdir / "lab.toml"), port=0)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def req(path, body=None, headers=None):
        h = {"X-Lab-Web": "1", "Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        r = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    yield req
    srv.shutdown()


def _seed(db):
    t = now()
    db.insert("threads", id="t-001", project="affine", title="try GRPO", status="active", created_at=t, passes=3,
              metric="margin", workdir="/nonexistent")
    db.insert("results", project="affine", thread_id="t-001", ts=t, run="r1", description="first", metric="margin",
              value=0.1, kept=1, cost_usd=2.0)
    db.insert("leases", project="affine", holder="t-001", experiment_id="t-001", status="granted",
              gpu_type="NVIDIA H100 80GB HBM3", gpu_count=1, cloud="SECURE", max_hours=4, price_hr=3.0,
              requested_at=t - 600, granted_at=t - 600, job_state=None, pool="research")
    db.insert("agent_runs", project="affine", role="thread", key="t-001", status="error", queued_at=t - 60,
              started_at=t - 50, ended_at=t - 10, error="boom", cost_usd=0.5)
    db.insert("ledger", project="affine", ts=t, pool="research", experiment_id="t-001", usd=1.5)
    db.emit("affine", "thread.stalled", "t-001 may be stuck", severity="normal", key="t-001")


def test_views_render_with_alerts(web, db):
    _seed(db)
    code, body = web("/")
    assert code == 200 and b"Lab console" in body
    o = json.loads(web("/api/overview")[1])
    texts = " | ".join(a["text"] for a in o["alerts"])
    assert o["alerts"][0]["level"] == "critical" and "heartbeat" in texts   # no labd in tests
    assert "no running job" in texts and "failed or timed out" in texts and "Stall alarm" in texts
    assert o["burn_hr"] == 3.0 and o["budget"]["spent_today"] == 1.5
    for path in ("threads", "threads/t-001", "gpus", "runs", "events?sev=normal", "discord", "inbox", "world"):
        code, body = web("/api/" + path)
        assert code == 200, (path, body)
    g = json.loads(web("/api/gpus")[1])
    assert g["daily"][-1]["gpu"] == 1.5 and g["by_holder"][0]["holder"] == "t-001"
    run_id = json.loads(web("/api/runs")[1])["runs"][0]["id"]
    assert json.loads(web(f"/api/runs/{run_id}")[1])["run"]["error"] == "boom"
    assert web("/api/threads/t-999")[0] == 404


def test_actions_go_through_the_cli(web, db):
    _seed(db)
    code, body = web("/api/action", {"action": "gpu_pause", "reason": "checking"})
    r = json.loads(body)
    assert code == 200 and r["ok"], r
    assert db.kv_get("affine", "gpu_paused")["reason"] == "checking"
    assert json.loads(web("/api/action", {"action": "gpu_resume"})[1])["ok"]
    assert db.kv_get("affine", "gpu_paused") is None
    r = json.loads(web("/api/action", {"action": "thread_note", "id": "t-001", "text": "look at X"})[1])
    assert r["ok"], r
    e = db.one("SELECT * FROM events WHERE topic='thread.message' ORDER BY id DESC")
    assert e["key"] == "t-001" and json.loads(e["payload"])["from"] == "operator (web)"


def test_actions_are_guarded(web, db):
    # no custom header (what a cross-site form post looks like), foreign Host, foreign Origin
    assert web("/api/action", {"action": "gpu_pause"}, {"X-Lab-Web": "0"})[0] == 403
    assert web("/api/action", {"action": "gpu_pause"}, {"Host": "evil.example"})[0] == 403
    assert web("/api/action", {"action": "gpu_pause"}, {"Origin": "http://evil.example"})[0] == 403
    assert web("/api/overview", headers={"Host": "evil.example:8765"})[0] == 403
    assert db.kv_get("affine", "gpu_paused") is None
    assert web("/api/action", {"action": "rm_rf"})[0] == 400
    with pytest.raises(ValueError):
        action_argv({"action": "thread_note", "id": "t-1; rm -rf /", "text": "x"})
    assert action_argv({"action": "maint_approve", "id": "m-3"}) == ["maint", "approve", "m-3"]
