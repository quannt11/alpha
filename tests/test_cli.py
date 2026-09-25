import json
import re
import time
from pathlib import Path

import pytest

from lab import cli
from lab.db import DB

ROOT = Path(__file__).resolve().parents[1]
H100 = "NVIDIA H100 80GB HBM3"


def run(*argv):
    cli.main(list(argv))


@pytest.fixture
def thread(labdir, cfg, tmp_path, monkeypatch):
    charter = tmp_path / "program.md"
    charter.write_text("# Charter: harness fidelity\nMetric: |our score - validator score|\n")
    run("thread", "start", "--title", "harness fidelity", "--metric", "abs_err_sd", "--text", str(charter))
    monkeypatch.setenv("LAB_THREAD", "t-001")
    return "t-001"


def test_thread_start_creates_workdir_and_wakes_it(labdir, cfg, thread):
    db = DB(cfg.db_path)
    t = db.one("SELECT * FROM threads WHERE id='t-001'")
    assert t["status"] == "active" and t["metric"] == "abs_err_sd"
    wd = Path(t["workdir"])
    assert "harness fidelity" in (wd / "TASK.md").read_text() and (wd / "results.tsv").exists()
    assert db.one("SELECT * FROM events WHERE topic='thread.start'")["key"] == "t-001"
    assert db.one("SELECT * FROM events WHERE topic='thread.task'")["key"] == "t-001"     # the wake
    idea = db.one("SELECT * FROM backlog")
    assert idea["status"] == "assigned" and idea["thread_id"] == "t-001" and t["task_id"] == idea["id"]


def test_max_threads_enforced(labdir, cfg, thread, monkeypatch):
    with pytest.raises(SystemExit):
        run("thread", "start", "--title", "second", "--text", "x")      # max_threads = 1
    run("thread", "retire", "t-001", "--text", "done")
    run("thread", "start", "--title", "second", "--text", "x")
    db = DB(cfg.db_path)
    assert [r["id"] for r in db.all("SELECT id FROM threads WHERE status='active'")] == ["t-002"]


def test_result_add_tracks_best_and_tsv(labdir, cfg, thread):
    run("result", "add", "--metric", "abs_err_sd", "--value", "0.31", "--kept", "yes", "--desc", "baseline",
        "--run", "r001", "--cost", "1.2")
    run("result", "add", "--metric", "abs_err_sd", "--value", "0.40", "--kept", "no", "--desc", "worse lr")
    run("result", "add", "--metric", "abs_err_sd", "--value", "0.12", "--kept", "yes", "--desc", "fix echo",
        "--best")
    db = DB(cfg.db_path)
    t = db.one("SELECT * FROM threads")
    assert t["best_value"] == 0.12 and t["best_desc"] == "fix echo"
    topics = [r["topic"] for r in db.all("SELECT topic FROM events WHERE key='t-001' ORDER BY id")]
    assert topics.count("thread.result") == 2 and "result.discarded" in topics
    tsv = (Path(t["workdir"]) / "results.tsv").read_text().splitlines()
    assert len(tsv) == 4 and "discard" in tsv[2]


def test_note_and_claim(labdir, cfg, thread):
    run("thread", "note", "t-001", "--text", "try the teacher at TP1", "--author", "Alice")
    run("thread", "claim", "--text", "margin +0.31 sd vs reign 21 on 1000-turn slice")
    db = DB(cfg.db_path)
    msg = db.one("SELECT * FROM events WHERE topic='thread.message'")
    assert msg["key"] == "t-001" and json.loads(msg["payload"])["from"] == "Alice"
    assert db.one("SELECT severity FROM events WHERE topic='thread.claim'")["severity"] == "major"


def test_thread_note_to_itself_does_not_wake_it(labdir, cfg, thread, monkeypatch):
    monkeypatch.setenv("LAB_ROLE", "thread")
    run("thread", "note", "t-001", "--text", "pass 1 summary")
    db = DB(cfg.db_path)
    assert not db.one("SELECT 1 FROM events WHERE topic='thread.message'")
    assert db.one("SELECT key FROM events WHERE topic='thread.log'")["key"] == "t-001"


def test_gpu_lease_by_thread_with_test_policy(labdir, cfg, thread):
    toml = labdir / "projects" / "affine" / "project.toml"
    toml.write_text(re.sub(r"(?m)^test_mode = .*$", "test_mode = true", toml.read_text()))
    run("gpu", "lease", "--gpu", H100, "--hours", "3", "--alt", "NVIDIA H100 NVL")
    db = DB(cfg.db_path)
    l = db.one("SELECT * FROM leases")
    assert l["holder"] == "t-001" and l["status"] == "requested" and l["gpu_count"] == 1
    assert json.loads(l["alternatives"]) == ["NVIDIA H100 NVL"] and l["max_hours"] == 3
    with pytest.raises(SystemExit):                                     # test mode: 1 GPU
        run("gpu", "lease", "--gpu", H100, "--count", "4")
    with pytest.raises(SystemExit):                                     # test mode: H100 only
        run("gpu", "lease", "--gpu", "NVIDIA H200")
    run("gpu", "release")
    assert db.one("SELECT status FROM leases")["status"] == "denied"    # a requested lease is cancelled


def test_gpu_release_stop_reaches_a_pod_already_released(labdir, cfg, thread):
    """m-6: `release` then `release --stop` left an 8×H200 VM idle 20 min: the second call matched no lease."""
    db = DB(cfg.db_path)
    lid = db.insert("leases", project="affine", holder="t-001", experiment_id="t-001", status="released",
                    gpu_type=H100, gpu_count=1, max_hours=2, pod_id="vm1", pool="research")
    db.insert("pods", id="vm1", project="affine", name="Pi_affine-01", cloud="SHADEFORM", state="RUNNING",
              last_experiment="t-001")
    with pytest.raises(SystemExit):                                     # plain release: nothing to do, says so
        run("gpu", "release")
    run("gpu", "release", "--lease", str(lid), "--stop")
    assert db.one("SELECT stop_requested FROM pods WHERE id='vm1'")["stop_requested"] == 1
    db.update("pods", "id='vm1'", (), stop_requested=0, lease_id=99)   # re-leased: not ours to stop any more
    with pytest.raises(SystemExit):
        run("gpu", "release", "--stop")


def test_gpu_lease_without_test_mode_takes_any_shape(labdir, cfg, thread):
    """affine runs for real since m-2 (2026-09-24): only the budget limits a lease."""
    run("gpu", "lease", "--gpu", "NVIDIA H200", "--count", "4", "--hours", "2")
    l = DB(cfg.db_path).one("SELECT * FROM leases")
    assert l["status"] == "requested" and l["gpu_count"] == 4 and l["gpu_type"] == "NVIDIA H200"


def test_gpu_lease_needs_a_thread(labdir, cfg, monkeypatch):
    monkeypatch.delenv("LAB_THREAD", raising=False)
    with pytest.raises(SystemExit):
        run("gpu", "lease", "--gpu", H100)


def test_gpu_extend_checks_budget(labdir, cfg, thread):
    db = DB(cfg.db_path)
    lid = db.insert("leases", project="affine", holder="t-001", experiment_id="t-001", status="granted",
                    gpu_type=H100, gpu_count=1, max_hours=2, price_hr=3.49, granted_at=time.time(),
                    expires_at=time.time() + 7200, pool="research")
    run("gpu", "extend", "--hours", "3")
    l = db.one("SELECT * FROM leases WHERE id=?", (lid,))
    assert l["max_hours"] == 5
    with pytest.raises(SystemExit):
        run("gpu", "extend", "--hours", "1000")                         # $3490 > $600


def test_say_ticket_idea_inject(labdir, cfg, capsys):
    run("say", "hello #120")
    run("ticket", "new", "--title", "check epoch coverage", "--body", "please", "--author", "alice")
    run("idea", "add", "--title", "idea", "--hypothesis", "because", "--gain", "+0.1 sd", "--cost", "40")
    run("inject", "what is the king?", "--author", "bob")
    db = DB(cfg.db_path)
    assert db.one("SELECT content FROM outbox")["content"] == "hello #120"
    topics = [r["topic"] for r in db.all("SELECT topic FROM events ORDER BY id")]
    assert topics == ["ticket.new", "idea.new", "discord.request"]
    assert json.loads(db.one("SELECT payload FROM events WHERE topic='discord.request'")["payload"])["simulated"]


def test_gpu_stock_reads_feed(labdir, cfg, capsys):
    db = DB(cfg.db_path)
    db.kv_set("affine", "gpu_prices", {H100: {"SECURE": 3.49}, "NVIDIA H200": {"SECURE": 4.59}})
    db.kv_set("affine", "stock", {"at": time.time(), "shapes": {
        f"{H100}|4|SECURE": "Low", f"{H100}|8|SECURE": None, "NVIDIA H200|8|SECURE": None}})
    run("gpu", "stock", "h100")
    out = capsys.readouterr().out
    assert "Low     4× NVIDIA H100 80GB HBM3" in out and "none    8× NVIDIA H100 80GB HBM3" in out
    assert "H200" not in out and "$3.49/gpu/h" in out


def test_gpu_pause_resume_humans_only(labdir, cfg, monkeypatch):
    run("gpu", "pause", "fixing bugs")
    db = DB(cfg.db_path)
    assert db.kv_get("affine", "gpu_paused")["reason"] == "fixing bugs"
    monkeypatch.setenv("LAB_RUN_ID", "12")          # an agent
    with pytest.raises(SystemExit):
        run("gpu", "resume")
    monkeypatch.delenv("LAB_RUN_ID")
    run("gpu", "resume")
    assert not db.kv_get("affine", "gpu_paused")


def test_gpu_resume_wakes_threads_with_a_task(labdir, cfg, thread, monkeypatch):
    monkeypatch.delenv("LAB_THREAD")
    run("gpu", "pause")
    run("gpu", "resume")
    db = DB(cfg.db_path)
    ev = db.one("SELECT * FROM events WHERE topic='thread.continue'")
    assert ev and ev["key"] == "t-001"


def test_daily_report_mirrored_only_for_analyst_report(labdir, cfg, monkeypatch, tmp_path):
    mirror = next(iter(cfg.project("affine").report_mirrors))
    rep = tmp_path / "daily-2026-09-25.md"
    rep.write_text("**affine daily**")
    run("say", "--file-text", str(rep))                                  # not the analyst: #120 only
    monkeypatch.setattr(cli, "ROLE", "analyst")
    run("say", "claim holds")                                            # analyst, not the report
    run("say", "--file-text", str(rep))
    db = DB(cfg.db_path)
    rows = [(r["channel_id"], r["content"]) for r in db.all("SELECT * FROM outbox ORDER BY id")]
    assert [ch for ch, _ in rows].count(mirror) == 1 and rows[-1] == (mirror, "**affine daily**")
    with pytest.raises(SystemExit):
        run("say", "sneaky", "--channel-id", mirror)


@pytest.fixture
def two_projects(labdir):
    """A second project next to affine: no plugin, its own code root."""
    pdir = labdir / "projects" / "beta"
    pdir.mkdir()
    (labdir / "beta-code").mkdir()
    (pdir / "project.toml").write_text(f'name = "beta"\nroot = "{labdir / "beta-code"}"\n[budget]\ndaily_usd = 50\n')
    return pdir


def test_status_covers_every_project_when_none_is_given(labdir, two_projects, capsys, monkeypatch):
    monkeypatch.chdir(labdir)
    run("status")
    out = capsys.readouterr().out
    assert "# affine — status" in out and "# beta — status" in out and out.count("labd heartbeat") == 1
    with pytest.raises(SystemExit):
        run("budget")                                   # everything else still needs a project
    assert "which project?" in capsys.readouterr().err


def test_project_is_taken_from_the_cwd(labdir, two_projects, capsys, monkeypatch):
    sub = labdir / "beta-code" / "src"
    sub.mkdir()
    monkeypatch.chdir(sub)
    run("status")
    out = capsys.readouterr().out
    assert "# beta — status" in out and "# affine" not in out
    monkeypatch.chdir(labdir / "projects" / "affine")
    run("status")
    assert "# affine — status" in capsys.readouterr().out


def test_world_facts_use_the_plugin_hook_or_show_every_field(cfg, two_projects, project):
    from lab import config as config_mod
    from lab.context import world_facts
    (project.world_dir).mkdir(parents=True, exist_ok=True)
    (project.world_dir / "world.json").write_text(json.dumps(
        {"world_version": "wvk24-e1-abc", "teacher": "Qwen/T", "contract": {"duel.n_turns": 1000}}))
    out = world_facts(project)
    assert "teacher: Qwen/T" in out and "duel.n_turns=1000" in out and "our crowns:" in out
    beta = config_mod.load(cfg.root / "lab.toml").project("beta")
    beta.world_dir.mkdir(parents=True)
    (beta.world_dir / "world.json").write_text(json.dumps({"world_version": "v1", "king": {"repo": "x/y"}}))
    out = world_facts(beta)
    assert out.startswith("world_version: v1") and 'king: {"repo": "x/y"}' in out


def test_maint_is_lab_wide_but_a_request_needs_a_project(labdir, two_projects, capsys, monkeypatch):
    monkeypatch.chdir(labdir)
    run("maint", "list")                                 # lab-deploy runs `lab maint mark` with no project
    with pytest.raises(SystemExit):
        run("maint", "request", "change X")
    assert "which project" in capsys.readouterr().err


def test_project_may_follow_the_subcommand():
    p = cli.build_parser()
    assert p.parse_args(["researcher", "show", "--project", "albedo"]).project == "albedo"
    assert p.parse_args(["--project", "affine", "researcher", "show"]).project == "affine"
    assert p.parse_args(["researcher", "show"]).project is None
