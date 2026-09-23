import asyncio
import json

import pytest

import lab.fleet as fleet_mod
from lab.db import now
from lab.fleet import Fleet, parse_status
from lab.runpod import Pod, RunpodError

H100 = "NVIDIA H100 80GB HBM3"


class FakeRunpod:
    def __init__(self):
        self.pods: dict[str, Pod] = {}
        self.calls: list[tuple] = []
        self.n = 0
        self.fail_community = False
        # a teammate's pod the lab must never touch
        self.pods["teammate1"] = Pod("teammate1", "an_cacheon", "RUNNING", H100, 1, 3.49, "1.2.3.4", 22, "m0",
                                     "SECURE", {})

    async def gpu_prices(self):
        return {H100: {"COMMUNITY": 2.0, "SECURE": 3.0}}

    async def create_pod(self, *, name, gpu_type, gpu_count, cloud, image, container_disk_gb, volume_gb, env):
        self.calls.append(("create", name, cloud))
        if cloud == "COMMUNITY" and self.fail_community:
            raise RunpodError("no instances available", 400, capacity=True)
        self.n += 1
        pid = f"pod{self.n}"
        self.pods[pid] = Pod(pid, name, "RUNNING", gpu_type, gpu_count, 2.0 * gpu_count, "10.0.0.1", 40000 + self.n,
                             f"m{self.n}", cloud, {})
        return self.pods[pid]

    async def get_pod(self, pid):
        return self.pods.get(pid)

    async def list_pods(self):
        return list(self.pods.values())

    async def start(self, pid):
        self.calls.append(("start", pid))
        self.pods[pid].status = "RUNNING"

    async def stop(self, pid):
        self.calls.append(("stop", pid))
        self.pods[pid].status = "EXITED"


@pytest.fixture
def rp():
    return FakeRunpod()


@pytest.fixture
def fleet(db, cfg, project, rp, monkeypatch):
    async def fake_ssh(key, host, port, cmd, timeout=45):
        if "status.json" in cmd:
            return 0, json.dumps(fake_ssh.status, indent=1)      # labrun writes indented JSON
        return 0, "NVIDIA H100 80GB HBM3\n/dev/sda 200G"
    fake_ssh.status = {}
    monkeypatch.setattr(fleet_mod, "ssh_run", fake_ssh)
    f = Fleet(db, cfg, project, rp)
    f.max_per_lease, f.allowed_types = 0, []     # mechanics tests run without the test-mode policy
    f.ssh = fake_ssh
    return f


def add_thread(db, tid="t-001", status="active"):
    db.insert("threads", id=tid, project="affine", title="t " + tid, status=status, created_at=now(), passes=0,
              workdir="/tmp")


def lease(db, holder="t-001", gpu=H100, count=1, hours=2.0, alts=(), status="requested"):
    return db.insert("leases", project="affine", holder=holder, experiment_id=holder, status=status, gpu_type=gpu,
                     gpu_count=count, max_hours=hours, alternatives=json.dumps(list(alts)), pool="research",
                     requested_at=now())


def topics(db):
    return [r["topic"] for r in db.all("SELECT topic FROM events ORDER BY id")]


def L(db, lid):
    return db.one("SELECT * FROM leases WHERE id=?", (lid,))


async def test_lease_lifecycle_and_watchdog(db, fleet, rp):
    add_thread(db)
    lid = lease(db)
    await fleet.process_leases()          # requested → provisioning → (pod already up) granted
    l = L(db, lid)
    assert l["status"] == "granted" and l["pod_name"] == "Pi_affine-01" and l["ssh_port"] == 40001
    assert ("create", "Pi_affine-01", "COMMUNITY") in rp.calls
    assert l["est_usd"] == pytest.approx(4.0)
    db.update("leases", "id=?", (lid,), job_name="r001-base")
    fleet.ssh.status = {"state": "running", "heartbeat_at": now()}
    await fleet.watchdog()
    fleet.ssh.status = {"state": "done", "exit_code": 0, "heartbeat_at": now()}
    await fleet.watchdog()
    fin = db.one("SELECT * FROM events WHERE topic='job.finished'")
    assert fin["key"] == "t-001" and "r001-base" in fin["summary"]
    db.update("leases", "id=?", (lid,), status="release_requested", job_status=json.dumps({"stop_now": True}))
    await fleet.process_leases()
    assert ("stop", "pod1") in rp.calls
    assert db.one("SELECT lease_id FROM pods WHERE id='pod1'")["lease_id"] is None


async def test_only_active_threads_get_gpus(db, fleet, rp):
    add_thread(db, "t-001", status="retired")
    lid = lease(db, "t-001")
    lid2 = lease(db, "t-009")
    await fleet.process_leases()
    assert L(db, lid)["status"] == "denied" and "retired" in L(db, lid)["reason"]
    assert L(db, lid2)["status"] == "denied" and "unknown thread" in L(db, lid2)["reason"]


async def test_capacity_fallback_to_secure(db, fleet, rp):
    rp.fail_community = True
    add_thread(db)
    lease(db)
    await fleet.process_leases()
    assert [c for c in rp.calls if c[0] == "create"][-1][2] == "SECURE"


async def test_daily_budget_is_the_only_limit(db, fleet, rp):
    assert fleet.max_gpus == 0 and fleet.p.per_experiment_usd == 0
    add_thread(db)
    big = lease(db, hours=250)                     # $500 on one lease: fine, no approval step
    await fleet.process_leases()
    assert L(db, big)["status"] == "granted"
    over = lease(db, hours=60)                     # $120 more → $620 > $600
    await fleet.process_leases()
    assert L(db, over)["status"] == "denied" and "daily budget" in L(db, over)["reason"]


async def test_pods_stop_at_100_percent(db, fleet, rp):
    add_thread(db)
    lease(db, hours=1)
    await fleet.process_leases()
    fleet.budget.record(600, "research", experiment_id=None, pod_id=None)
    await fleet.reconcile()
    assert ("stop", "pod1") in rp.calls
    assert "daily budget reached" in db.one(
        "SELECT summary FROM events WHERE topic='budget.alert' ORDER BY id DESC")["summary"]


async def test_never_touches_foreign_pods(db, fleet, rp):
    await fleet.reconcile()
    assert not any(c[1] == "teammate1" for c in rp.calls if c[0] in ("stop", "start"))
    add_thread(db)
    lease(db)
    await fleet.process_leases()
    db.x("UPDATE pods SET lease_id=NULL, idle_since=?", (now() - 3600,))
    await fleet.reconcile()
    assert [c[1] for c in rp.calls if c[0] == "stop"] == ["pod1"]


async def test_billing_per_thread_and_overtime(db, fleet, rp):
    add_thread(db)
    lid = lease(db, hours=1)
    await fleet.process_leases()
    db.x("UPDATE pods SET last_billed_at=?", (now() - 1800,))
    await fleet.reconcile()
    assert db.one("SELECT SUM(usd) s FROM ledger")["s"] == pytest.approx(1.0, rel=0.05)   # $2/h × 0.5h
    assert db.one("SELECT spent_usd FROM threads")["spent_usd"] == pytest.approx(1.0, rel=0.05)
    db.update("leases", "id=?", (lid,), expires_at=now() - 0.2 * 3600)
    await fleet.reconcile()
    assert L(db, lid)["status"] == "released" and ("stop", "pod1") in rp.calls


async def test_no_runpod_key_denies(db, cfg, project):
    f = Fleet(db, cfg, project, None)
    add_thread(db)
    lid = lease(db)
    await f.process_leases()
    assert L(db, lid)["status"] == "denied" and "RUNPOD_API_KEY" in L(db, lid)["reason"]


async def test_stall_failure_and_idle_gpu_detection(db, fleet, rp):
    add_thread(db)
    lid = lease(db, hours=5)
    await fleet.process_leases()
    db.update("leases", "id=?", (lid,), job_name="r002")
    fleet.ssh.status = {"state": "running", "heartbeat_at": now() - 3 * 3600}
    await fleet.watchdog()
    assert any("no heartbeat" in r["summary"] for r in db.all("SELECT summary FROM events WHERE topic='job.anomaly'"))
    fleet.ssh.status = {"state": "failed", "exit_code": 1, "message": "Traceback: boom", "heartbeat_at": now()}
    await fleet.watchdog()
    assert any("failed (exit 1)" in r["summary"] for r in db.all("SELECT summary FROM events WHERE topic='job.anomaly'"))
    fleet.ssh.status = {"state": "failed", "exit_code": 1, "heartbeat_at": now() - 3600}
    await fleet.watchdog()
    assert db.one("SELECT 1 FROM events WHERE topic='job.idle' AND key='t-001'")


async def test_capacity_wait_retry_then_give_up(db, fleet, rp):
    async def no_stock(**kw):
        rp.calls.append(("create", kw["name"], kw["cloud"]))
        raise RunpodError("create pod: There are no instances currently available", 500, capacity=True)
    rp.create_pod = no_stock
    add_thread(db)
    lid = lease(db)
    await fleet.process_leases()
    assert L(db, lid)["status"] == "requested" and "waiting for capacity" in L(db, lid)["reason"]
    assert "gpu.waiting" in topics(db)
    n = len(rp.calls)
    await fleet.process_leases()
    assert len(rp.calls) == n
    db.kv_set("affine", f"capnext:{lid}", 0)
    db.kv_set("affine", f"capfirst:{lid}", now() - 7 * 3600)
    await fleet.process_leases()
    assert L(db, lid)["status"] == "failed" and "no Runpod capacity" in L(db, lid)["reason"]


async def test_alternative_gpu_used_when_primary_out_of_stock(db, fleet, rp):
    real = rp.create_pod

    async def only_h200(**kw):
        if kw["gpu_type"] != "NVIDIA H200":
            rp.calls.append(("create", kw["name"], kw["cloud"]))
            raise RunpodError("no instances currently available", 500, capacity=True)
        return await real(**kw)
    rp.create_pod = only_h200
    prices = await rp.gpu_prices()
    prices["NVIDIA H200"] = {"COMMUNITY": 3.0, "SECURE": 4.0}
    rp.gpu_prices = lambda: asyncio.sleep(0, result=prices)
    add_thread(db)
    lid = lease(db, alts=["NVIDIA H200"])
    await fleet.process_leases()
    assert L(db, lid)["status"] == "granted" and L(db, lid)["gpu_type"] == "NVIDIA H200"
    assert L(db, lid)["est_usd"] == pytest.approx(6.0)        # priced on the more expensive candidate


async def test_clouds_that_cannot_host_the_count_are_skipped(db, fleet, rp):
    prices = {H100: {"COMMUNITY": 2.69, "SECURE": 3.49, "max": {"COMMUNITY": 1, "SECURE": 8}}}
    rp.gpu_prices = lambda: asyncio.sleep(0, result=prices)
    est, cloud = await fleet.estimate(H100, 8, 1.0)
    assert cloud == "SECURE" and est == pytest.approx(3.49 * 8)
    add_thread(db)
    lease(db, count=8, hours=1)
    await fleet.process_leases()
    assert [c[2] for c in rp.calls if c[0] == "create"] == ["SECURE"]


async def test_test_mode_policy(db, fleet, rp):
    fleet.max_per_lease = 1
    fleet.allowed_types = [H100, "NVIDIA H100 PCIe"]
    add_thread(db)
    four = lease(db, count=4)
    wrong = lease(db, gpu="NVIDIA H200")
    ok = lease(db, alts=["NVIDIA H200", "NVIDIA H100 PCIe"])
    await fleet.process_leases()
    assert L(db, four)["status"] == "denied" and "test mode" in L(db, four)["reason"]
    assert L(db, wrong)["status"] == "denied"
    assert L(db, ok)["status"] == "granted"
    assert fleet.candidates(L(db, ok)) == [H100, "NVIDIA H100 PCIe"]


async def test_optional_gpu_concurrency_cap(db, fleet, rp):
    fleet.max_gpus = 8
    add_thread(db)
    lease(db, count=8, status="granted")
    lid = lease(db, count=4)
    await fleet.process_leases()
    assert L(db, lid)["status"] == "requested"


async def test_stock_feed_orders_skips_and_wakes(db, fleet, rp):
    stock = {}

    async def fake_stock(shapes):
        return {s: stock.get(s) for s in shapes}
    rp.stock = fake_stock
    fleet.stock_watch = {"types": [H100], "counts": [1]}
    await fleet.refresh_stock(force=True)
    assert fleet.stock_of(H100, 1, "COMMUNITY") == "none"
    assert fleet.stock_of(H100, 2, "COMMUNITY") == "unknown"
    add_thread(db)
    lid = lease(db)
    db.kv_set("affine", f"blind:{lid}", now())
    await fleet.process_leases()
    assert not [c for c in rp.calls if c[0] == "create"] and L(db, lid)["status"] == "requested"
    stock[(H100, 1, "SECURE")] = "Low"
    await fleet.refresh_stock(force=True)
    assert db.kv_get("affine", f"capnext:{lid}") == 0
    await fleet.process_leases()
    assert [c[2] for c in rp.calls if c[0] == "create"][0] == "SECURE"
    assert L(db, lid)["status"] == "granted"


async def test_stock_requests_from_cli_are_fetched(db, fleet, rp):
    async def fake_stock(shapes):
        return {s: "High" for s in shapes}
    rp.stock = fake_stock
    db.kv_set("affine", "stock_requests", [[H100, 4]])
    await fleet.refresh_stock()
    assert fleet.stock_of(H100, 4, "SECURE") == "High" and db.kv_get("affine", "stock_requests") == []


async def test_pause_stops_pods_and_blocks_new_work(db, fleet, rp):
    add_thread(db)
    lid = lease(db)
    await fleet.process_leases()
    assert L(db, lid)["status"] == "granted"
    db.kv_set("affine", "gpu_paused", {"at": now(), "reason": "test"})
    await fleet.enforce_pause()
    assert ("stop", "pod1") in rp.calls and L(db, lid)["status"] == "released"
    lid2 = lease(db)
    n = len(rp.calls)
    await fleet.process_leases()
    assert len(rp.calls) == n and L(db, lid2)["status"] == "requested"
    db.kv_set("affine", "gpu_paused", None)
    await fleet.process_leases()
    assert L(db, lid2)["status"] == "granted"


async def test_thread_gets_its_own_pod_back(db, fleet, rp):
    for i, tid in enumerate(("t-001", "t-002"), 1):
        add_thread(db, tid)
        rp.pods[f"old{i}"] = Pod(f"old{i}", f"Pi_affine-0{i}", "EXITED", H100, 1, 3.49, "h", 1, "m", "SECURE", {})
        db.insert("pods", id=f"old{i}", project="affine", name=f"Pi_affine-0{i}", gpu_type=H100, gpu_count=1,
                  cloud="SECURE", price_hr=3.49, created_at=now(), state="EXITED", last_seen=now() - i,
                  last_experiment=tid)
    lease(db, "t-002")
    await fleet.process_leases()
    assert ("start", "old2") in rp.calls and ("start", "old1") not in rp.calls


def test_parse_status_handles_real_labrun_output():
    real = '{\n "exp": "r001",\n "pid": 1322,\n "exit_code": 1,\n "state": "failed",\n "message": "Traceback {x}"\n}\n'
    assert parse_status(real)["state"] == "failed"
    assert parse_status("Warning: banner\n" + real)["exit_code"] == 1
    assert parse_status("{}") == {} and parse_status("") == {} and parse_status("garbage") == {}
