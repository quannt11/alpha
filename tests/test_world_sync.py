"""Every agent must act on the live world: STATE.md lag is detected, flagged and healed, and ideas and
threads written under older rules are marked."""
import json

from lab import cli
from lab.agents import Agents
from lab.context import backlog_table, status_text, threads_table, world_lag
from lab.daemon import Daemon
from lab.db import DB, now

OLD, LIVE = "wvk24-e77-cdc56f848", "wvk24-e78-cdc56f848"


def world(project, live=LIVE, state=OLD):
    project.world_dir.mkdir(parents=True, exist_ok=True)
    (project.world_dir / "world.json").write_text(json.dumps({"world_version": live}))
    (project.world_dir / "STATE.md").write_text(f"# World State\n\nWorld version: `{state}` (as of …)\n")


def test_lag_lists_the_changes_since_state_versions_went_stale(db, cfg, project):
    world(project)
    db.emit("affine", "world.change.contract", "long ago: wvk 23 -> 24", severity="major")
    db.x("UPDATE events SET ts=?", (now() - 7200,))
    db.kv_set("affine", "world_versions", [[OLD, now() - 7000], [LIVE, now() - 600]])
    db.emit("affine", "world.change.corpus", "corpus epoch 77 → 78", severity="normal")
    lag = world_lag(db, project)
    assert "STATE.md is behind" in lag and OLD in lag and LIVE in lag
    assert "77 → 78" in lag and "wvk 23 -> 24" not in lag
    assert "STATE.md is behind" in status_text(db, cfg, project)
    world(project, state=LIVE)
    assert world_lag(db, project) == ""


def test_scout_is_woken_again_while_state_stays_behind(cfg):
    d = Daemon(cfg)
    p = cfg.project("affine")
    world(p)
    resyncs = lambda: d.db.all("SELECT * FROM events WHERE topic='world.change.resync'")
    t = now()
    d._resync_world(p, t)
    d._resync_world(p, t + 300)
    assert not resyncs()                                  # give the Scout's own run time to land
    d._resync_world(p, t + 601)
    assert len(resyncs()) == 1 and json.loads(resyncs()[0]["payload"])["live_version"] == LIVE
    d._resync_world(p, t + 1200)
    assert len(resyncs()) == 1                            # at most every 30 min
    d.db.insert("agent_runs", project="affine", role="scout", key="", status="running", queued_at=t)
    d._resync_world(p, t + 2500)
    assert len(resyncs()) == 1                            # a Scout run is already on it
    world(p, state=LIVE)
    d._resync_world(p, t + 2600)
    assert d.db.kv_get("affine", "world_lag_since") == 0


def test_researcher_waits_for_a_pending_scout_update_but_not_forever(db, cfg):
    a = Agents(db, cfg)
    a._cursor("affine", "scout", "")
    a._cursor("affine", "researcher", "")
    db.emit("affine", "world.change.contract", "wvk 24 → 25", severity="major")
    db.emit("affine", "research.suggestion", "test grpo on the king", severity="normal", key="1")
    db.x("UPDATE events SET ts=?", (now() - 120,))       # past both debounces
    a.dispatch()
    queued = lambda role: db.one("SELECT * FROM agent_runs WHERE role=? AND status IN ('queued','running')", (role,))
    assert queued("scout") and not queued("researcher")
    db.x("UPDATE agent_runs SET status='ok' WHERE role='scout'")
    a.dispatch()
    assert queued("researcher")
    # a Scout that never finishes cannot hold the Researcher up
    db.x("UPDATE agent_runs SET status='ok'")
    db.emit("affine", "world.change.contract", "wvk 25 → 26", severity="major")
    db.emit("affine", "research.suggestion", "another request", severity="normal", key="2")
    db.x("UPDATE events SET ts=? WHERE topic IN ('world.change.contract','research.suggestion') AND id>2", (now() - 1300,))
    db.insert("agent_runs", project="affine", role="scout", key="", status="running", queued_at=now())
    a.dispatch()
    assert db.one("SELECT COUNT(*) n FROM agent_runs WHERE role='researcher'")["n"] == 2


def test_ideas_and_threads_written_under_older_rules_are_flagged(labdir, cfg, tmp_path):
    p = cfg.project("affine")
    world(p, live="wvk23-e70-cold", state="wvk23-e70-cold")
    cli.main(["idea", "add", "--title", "GRPO with the wvk-23 reward", "--hypothesis", "h"])
    charter = tmp_path / "program.md"
    charter.write_text("# Charter\n")
    cli.main(["thread", "start", "--title", "grpo", "--metric", "margin", "--text", str(charter)])
    db = DB(cfg.db_path)
    assert db.one("SELECT world_version FROM backlog")["world_version"] == "wvk23-e70-cold"
    assert db.one("SELECT world_version FROM threads")["world_version"] == "wvk23-e70-cold"
    db.kv_set("affine", "world_version", "wvk23-e71-cold")    # a new corpus epoch is not a rule change
    assert "⚠" not in backlog_table(db, "affine") and "⚠" not in threads_table(db, "affine")
    db.kv_set("affine", "world_version", "wvk24-e71-cnew")
    assert "⚠ written under wvk23 rules" in backlog_table(db, "affine")
    assert "⚠ chartered under wvk23 rules" in threads_table(db, "affine")
