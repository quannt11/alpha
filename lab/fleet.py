"""GPU fleet: leases held by research threads, pod reconcile, billing, stock and watchdog.

Each research thread (one Claude mind) manages its own GPUs: it asks for a lease with
`lab gpu lease`, runs jobs on the pod under labrun, extends or releases the lease. This
module grants those requests within the rules the operator set — the daily budget,
test mode, the pause switch — and keeps the pods honest: billing, idle reaping, overtime
stops and a watchdog that wakes the thread when its job finishes or goes wrong.

Pattern ported from affine/ops/teacher-swarm/manager.py (rent -> bootstrap -> probe ->
heal, with a spend cap and a blacklist of machines that burned us).

Hard rule: the fleet only ever touches pods recorded in its own `pods` table. The Runpod
account is shared with the whole team; a pod the lab did not create is invisible here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from pathlib import Path

from .budget import Budget, day_start
from .config import LabConfig, Project, expand
from .db import DB, now
from .runpod import Pod, Runpod, RunpodError

log = logging.getLogger("lab.fleet")

ACTIVE_LEASE = ("provisioning", "granted")


def ssh_base(key: Path, host: str, port: int) -> list[str]:
    # Pods are disposable and host:port pairs get reused by different pods, so
    # host keys are not pinned (teacher-swarm hit the same key churn).
    return ["ssh", "-i", str(key), "-p", str(port), "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=12",
            "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", f"root@{host}"]


async def ssh_run(key: Path, host: str, port: int, cmd: str, timeout: float = 45) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*ssh_base(key, host, port), cmd,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timeout"
    return proc.returncode or 0, out.decode(errors="replace")


def parse_status(out: str) -> dict:
    """labrun writes status.json indented over many lines; ssh may prepend banner noise.
    Parse the whole outermost JSON object in the output."""
    text = out.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        v = json.loads(text[start:end + 1])
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def holder_of(l) -> str:
    return l["holder"] or l["experiment_id"]


class Fleet:
    def __init__(self, db: DB, cfg: LabConfig, project: Project, runpod: Runpod | None, notify=None):
        self.db = db
        self.cfg = cfg
        self.p = project
        self.rp = runpod
        self.budget = Budget(db, project, cfg.timezone)
        f = project.fleet
        self.max_gpus = int(f.get("max_gpus", 0))                      # 0 = no concurrency cap
        self.idle_stop_s = float(f.get("idle_stop_minutes", 20)) * 60
        self.stall_s = float(f.get("stall_minutes", 30)) * 60
        self.check_s = float(f.get("check_hours", 1)) * 3600
        self.provision_timeout_s = float(f.get("provision_timeout_minutes", 25)) * 60
        self.capacity_retry_s = float(f.get("capacity_retry_minutes", 10)) * 60
        self.capacity_wait_s = float(f.get("capacity_wait_hours", 6)) * 3600
        self.stock_refresh_s = float(f.get("stock_refresh_minutes", 5)) * 60
        self.stock_watch = f.get("stock_watch", {})
        # [fleet.test_policy] applies only while the project is in test_mode.
        tp = f.get("test_policy", {}) if project.test_mode else {}
        self.max_per_lease = int(tp.get("max_gpus_per_experiment", 0))   # 0 = no limit
        self.allowed_types = list(tp.get("allowed_gpu_types", []))       # [] = any
        self.cloud_order = f.get("cloud_order", ["COMMUNITY", "SECURE"])
        self.image = f.get("image", cfg.runpod.get("image", "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"))
        self.disk_gb = int(f.get("container_disk_gb", 80))
        self.volume_gb = int(f.get("volume_gb", 200))
        self.ssh_key = expand(cfg.runpod.get("ssh_key", "~/.ssh/runpod_pic"))
        self.notify = notify
        self._prices: dict[str, dict] = {}
        self._prices_at = 0.0

    # ------------------------------------------------------------ pricing
    async def prices(self) -> dict[str, dict]:
        if self.rp and now() - self._prices_at > 3600:
            try:
                self._prices = await self.rp.gpu_prices()
                self._prices_at = now()
                self.db.kv_set(self.p.name, "gpu_prices", self._prices)
            except Exception as e:
                log.warning("gpu price refresh failed: %r", e)
        return self._prices

    def clouds_for(self, gpu_type: str, count: int) -> list[str]:
        """Clouds from cloud_order that allow `count` GPUs of this type in one pod (unknown = allowed)."""
        mx = (self._prices.get(gpu_type) or {}).get("max") or {}
        return [c for c in self.cloud_order if not mx.get(c) or count <= mx[c]]

    async def estimate(self, gpu_type: str | list[str], count: int, hours: float) -> tuple[float | None, str | None]:
        """Worst case over the candidate GPU types that have a known price, each priced on the first
        cloud that can host it. Unpriced alternatives are ignored; None only if nothing is priced."""
        hints = self.p.fleet.get("price_hints", {})
        prices = await self.prices()
        worst: tuple[float, str] | None = None
        for t in ([gpu_type] if isinstance(gpu_type, str) else gpu_type):
            got = None
            for cloud in self.clouds_for(t, count):
                pr = (prices.get(t) or {}).get(cloud)
                if pr:
                    got = (pr * count * hours, cloud)
                    break
            if got is None and t in hints:
                got = (float(hints[t]) * count * hours, self.cloud_order[0])
            if got is not None and (worst is None or got[0] > worst[0]):
                worst = got
        return worst if worst else (None, None)

    def candidates(self, l) -> list[str]:
        """The lease's GPU type followed by its alternatives, filtered by the test policy."""
        types = [l["gpu_type"]]
        try:
            alts = json.loads(l["alternatives"] or "[]")
        except ValueError:
            alts = []
        out = types + [a for a in alts if isinstance(a, str) and a not in types]
        return [t for t in out if not self.allowed_types or t in self.allowed_types]

    def policy_violation(self, l) -> str | None:
        """Test-mode GPU policy (project.toml [fleet.test_policy])."""
        if self.max_per_lease and l["gpu_count"] > self.max_per_lease:
            return (f"{l['gpu_count']} GPUs requested; test mode allows at most {self.max_per_lease} "
                    f"per lease")
        if self.allowed_types and not self.candidates(l):
            return f"GPU type {l['gpu_type']} not allowed in test mode; allowed: {', '.join(self.allowed_types)}"
        return None

    # ------------------------------------------------------------ stock
    STOCK_RANK = {"High": 0, "Medium": 1, "Low": 2}
    STOCK_FRESH_S = 900

    def _watch_shapes(self) -> set[tuple[str, int]]:
        shapes = {(t, int(c)) for t in self.stock_watch.get("types", []) for c in self.stock_watch.get("counts", [])}
        for l in self.db.all("SELECT * FROM leases WHERE project=? AND status='requested'", (self.p.name,)):
            shapes |= {(t, l["gpu_count"]) for t in self.candidates(l)}
        for t, c in self.db.kv_get(self.p.name, "stock_requests", []) or []:
            shapes.add((t, int(c)))
        return shapes

    async def refresh_stock(self, force: bool = False) -> None:
        """Every stock_refresh_minutes (or at once when the CLI asked for a shape): record stock for
        every watched shape on every cloud that can host it; wake capacity-waiting leases whose
        shape is now in stock."""
        if not self.rp:
            return
        cache = self.db.kv_get(self.p.name, "stock", {}) or {}
        pending = self.db.kv_get(self.p.name, "stock_requests", []) or []
        if not force and not pending and now() - float(cache.get("at", 0)) < self.stock_refresh_s:
            return
        await self.prices()
        shapes = [(t, c, cl) for t, c in sorted(self._watch_shapes()) for cl in self.clouds_for(t, c)]
        if not shapes:
            return
        try:
            got = await self.rp.stock(shapes)
        except Exception as e:
            log.warning("stock refresh failed: %r", e)
            return
        self.db.kv_set(self.p.name, "stock", {"at": now(), "shapes": {f"{t}|{c}|{cl}": v for (t, c, cl), v in got.items()}})
        self.db.kv_set(self.p.name, "stock_requests", [])
        self.db.kv_set(self.p.name, "gpu_prices", self._prices)
        for l in self.db.all("SELECT * FROM leases WHERE project=? AND status='requested'", (self.p.name,)):
            if any(got.get((t, l["gpu_count"], cl)) for t in self.candidates(l)
                   for cl in self.clouds_for(t, l["gpu_count"])):
                self.db.kv_set(self.p.name, f"capnext:{l['id']}", 0)

    def stock_of(self, gpu_type: str, count: int, cloud: str) -> str:
        """'High'|'Medium'|'Low' in stock, 'none' known out of stock, 'unknown' no fresh data."""
        cache = self.db.kv_get(self.p.name, "stock", {}) or {}
        key = f"{gpu_type}|{count}|{cloud}"
        if now() - float(cache.get("at", 0)) > self.STOCK_FRESH_S or key not in cache.get("shapes", {}):
            return "unknown"
        return cache["shapes"][key] or "none"

    # ------------------------------------------------------------ operator pause
    def paused(self) -> dict | None:
        """Set by a human with `lab gpu pause`; only `lab gpu resume` (human) clears it."""
        return self.db.kv_get(self.p.name, "gpu_paused") or None

    async def enforce_pause(self) -> None:
        """While paused: release every lease and stop every lab pod that is still running."""
        if not self.paused():
            return
        for l in self.db.all("SELECT * FROM leases WHERE project=? AND status IN ('provisioning','granted')",
                             (self.p.name,)):
            await self._release(l, reason="GPUs paused by operator", stop_now=True)
        if self.rp:
            for pod in self.db.all("SELECT * FROM pods WHERE project=? AND terminated=0 AND state='RUNNING'",
                                   (self.p.name,)):
                try:
                    await self.rp.stop(pod["id"])
                    self.db.update("pods", "id=?", (pod["id"],), state="EXITED", lease_id=None)
                except RunpodError as e:
                    log.warning("pause: stop %s failed: %r", pod["name"], e)

    # ------------------------------------------------------------ helpers
    def active_gpus(self) -> int:
        """GPUs on pods the lab actually holds (provisioning or granted leases)."""
        r = self.db.one("SELECT COALESCE(SUM(gpu_count),0) n FROM leases WHERE project=? AND status IN "
                        "('provisioning','granted','release_requested')", (self.p.name,))
        return int(r["n"])

    def _once(self, key: str, tag: str, topic: str, msg: str, sev: str):
        k = f"once:{key}:{tag}"
        if not self.db.kv_get(self.p.name, k):
            self.db.kv_set(self.p.name, k, now())
            self.db.emit(self.p.name, topic, msg, severity=sev, key=key)

    def _next_pod_name(self) -> str:
        names = {r["name"] for r in self.db.all("SELECT name FROM pods WHERE project=? AND terminated=0",
                                                  (self.p.name,))}
        pat = re.compile(re.escape(self.p.pod_prefix) + r"-(\d+)$")
        used = {int(m.group(1)) for n in names if (m := pat.match(n))}
        n = 1
        while n in used:
            n += 1
        return f"{self.p.pod_prefix}-{n:02d}"

    def _blacklist(self) -> set[str]:
        return set(self.db.kv_get(self.p.name, "machine_blacklist", []) or [])

    def _lease_event(self, lease, status: str, msg: str, sev: str = "normal", **extra):
        h = holder_of(lease)
        self.db.emit(self.p.name, "gpu.lease", f"{h}: lease {lease['id']} {status}: {msg}", severity=sev,
                     key=h, payload={"lease_id": lease["id"], "status": status, **extra})

    def _holder_ok(self, h: str) -> str | None:
        """Why this holder may not hold GPUs right now, or None."""
        t = self.db.one("SELECT status FROM threads WHERE id=? AND project=?", (h, self.p.name))
        if t is None:
            return f"unknown thread {h!r}"
        if t["status"] != "active":
            return f"thread {h} is {t['status']}"
        return None

    # ------------------------------------------------------------ leases
    async def process_leases(self) -> None:
        for l in self.db.all("SELECT * FROM leases WHERE project=? AND status='requested'", (self.p.name,)):
            await self._grant(l)
        for l in self.db.all("SELECT * FROM leases WHERE project=? AND status='provisioning'", (self.p.name,)):
            await self._check_provisioning(l)
        for l in self.db.all("SELECT * FROM leases WHERE project=? AND status='release_requested'", (self.p.name,)):
            await self._release(l, reason=l["reason"] or "released by holder",
                                stop_now=bool(json.loads(l["job_status"] or "{}").get("stop_now")))

    async def _grant(self, l) -> None:
        h = holder_of(l)
        if self.paused():
            self._once(h, f"paused-lease{l['id']}", "gpu.waiting",
                       f"{h}: GPUs are paused by the operator; lease {l['id']} waits", "normal")
            return
        if float(self.db.kv_get(self.p.name, f"capnext:{l['id']}", 0) or 0) > now():
            return  # waiting for Runpod capacity; retry later
        deny = None
        if not self.rp:
            deny = "RUNPOD_API_KEY is not configured on the daemon"
        else:
            deny = self._holder_ok(h) or self.policy_violation(l)
        est = None
        if deny is None:
            est, _ = await self.estimate(self.candidates(l), l["gpu_count"], l["max_hours"])
            if est is None:
                deny = f"no price for GPU type(s) {self.candidates(l)!r}"
            else:
                self.db.update("leases", "id=?", (l["id"],), est_usd=est)
                # this 'requested' row now reserves `est` itself, so credit it back before checking
                ok, why = self.budget.check(l["pool"] or next(iter(self.p.pools)), est, credit=est)
                if not ok:
                    deny = why
        if deny:
            self.db.update("leases", "id=?", (l["id"],), status="denied", reason=deny)
            self._lease_event(l, "denied", deny)
            return
        if self.max_gpus and self.active_gpus() + l["gpu_count"] > self.max_gpus:
            self._once(h, f"gpucap-lease{l['id']}", "gpu.waiting",
                       f"{h}: lease {l['id']} waits for GPUs ({self.active_gpus()}/{self.max_gpus} in use)", "info")
            return
        # prefer the holder's own previous pod: its /workspace still holds the code and downloads
        pod_row = None
        for t in self.candidates(l):
            pod_row = self.db.one(
                "SELECT * FROM pods WHERE project=? AND terminated=0 AND lease_id IS NULL AND gpu_type=? AND "
                "gpu_count=? ORDER BY (last_experiment=?) DESC, (state='RUNNING') DESC, last_seen DESC",
                (self.p.name, t, l["gpu_count"], h))
            if pod_row:
                break
        pool = l["pool"] or next(iter(self.p.pools))
        try:
            if pod_row:
                if pod_row["state"] != "RUNNING":
                    await self.rp.start(pod_row["id"])
                pod_id, name, cloud, gtype = pod_row["id"], pod_row["name"], pod_row["cloud"], pod_row["gpu_type"]
                self.db.update("pods", "id=?", (pod_id,), lease_id=l["id"], idle_since=None, last_pool=pool,
                               last_experiment=h)
            else:
                pod, gtype = await self._create(l, self.candidates(l))
                pod_id, name, cloud = pod.id, pod.name, pod.cloud
                self.db.insert("pods", id=pod.id, project=self.p.name, name=pod.name, gpu_type=gtype,
                               gpu_count=l["gpu_count"], cloud=pod.cloud, price_hr=pod.price_hr, created_at=now(),
                               state=pod.status, last_seen=now(), lease_id=l["id"], machine_id=pod.machine_id,
                               last_pool=pool, last_experiment=h, last_billed_at=now())
        except RunpodError as err:
            if err.capacity:
                await self._wait_for_capacity(l, err)
                return
            self.db.update("leases", "id=?", (l["id"],), status="failed", reason=str(err)[:500])
            self._lease_event(l, "failed", str(err)[:300], "major")
            return
        self.db.kv_set(self.p.name, f"capnext:{l['id']}", 0)
        self.db.update("leases", "id=?", (l["id"],), status="provisioning", pod_id=pod_id, pod_name=name,
                       cloud=cloud, gpu_type=gtype, granted_at=None)
        self._lease_event(l, "provisioning", f"{name} ({l['gpu_count']}× {gtype}, {cloud})", "info")

    async def _create(self, l, types: list[str]) -> tuple[Pod, str]:
        """Try each candidate GPU type on each cloud that can host the count, in-stock shapes first.
        Raises a capacity RunpodError only if every combination is out of stock."""
        env = {"PUBLIC_KEY": Path(str(self.ssh_key) + ".pub").read_text().strip()}
        combos = [(t, cl) for t in types for cl in self.clouds_for(t, l["gpu_count"])]
        known = {c: self.stock_of(c[0], l["gpu_count"], c[1]) for c in combos}
        combos.sort(key=lambda c: self.STOCK_RANK.get(known[c], 3 if known[c] == "unknown" else 9))
        # Trust "none" from the stock feed, but still try blind every 30 min in case the feed is wrong.
        blind_key = f"blind:{l['id']}"
        if combos and all(known[c] == "none" for c in combos):
            if now() - float(self.db.kv_get(self.p.name, blind_key, 0) or 0) < 1800:
                raise RunpodError(f"stock feed shows none of {', '.join(f'{t}/{cl}' for t, cl in combos)} "
                                  f"for {l['gpu_count']}×", 503, capacity=True)
            self.db.kv_set(self.p.name, blind_key, now())
        tried = []
        for gtype, cloud in combos:
            tried.append(f"{gtype}/{cloud}")
            try:
                pod = await self.rp.create_pod(name=self._next_pod_name(), gpu_type=gtype, gpu_count=l["gpu_count"],
                                               cloud=cloud, image=self.image, container_disk_gb=self.disk_gb,
                                               volume_gb=self.volume_gb, env=env)
                pod.cloud = pod.cloud or cloud        # v1 responses omit the cloud type
                return pod, gtype
            except RunpodError as err:
                if not err.capacity:
                    raise
                log.info("no capacity for %s× %s on %s", l["gpu_count"], gtype, cloud)
        if not tried:
            raise RunpodError(f"no cloud allows {l['gpu_count']}× of {types} in one pod", 400)
        raise RunpodError(f"no capacity for {l['gpu_count']}× on any of: {', '.join(tried)}", 503, capacity=True)

    async def _wait_for_capacity(self, l, err: RunpodError) -> None:
        """Keep the lease requested and retry; give up after capacity_wait_hours."""
        t = now()
        h = holder_of(l)
        first = float(self.db.kv_get(self.p.name, f"capfirst:{l['id']}", 0) or 0)
        if not first:
            self.db.kv_set(self.p.name, f"capfirst:{l['id']}", t)
            self.db.emit(self.p.name, "gpu.waiting",
                         f"{h}: lease {l['id']}: {err} — retrying every {self.capacity_retry_s / 60:.0f} min for up to "
                         f"{self.capacity_wait_s / 3600:.0f}h", severity="normal", key=h)
            first = t
        if t - first > self.capacity_wait_s:
            reason = f"no Runpod capacity for {(t - first) / 3600:.1f}h: {err}"
            self.db.update("leases", "id=?", (l["id"],), status="failed", reason=reason[:500])
            self._lease_event(l, "failed", reason[:300] + " — try other GPU types/counts", "major")
            return
        self.db.update("leases", "id=?", (l["id"],), reason=f"waiting for capacity since {first:.0f}")
        self.db.kv_set(self.p.name, f"capnext:{l['id']}", t + self.capacity_retry_s)

    async def _check_provisioning(self, l) -> None:
        pod = await self.rp.get_pod(l["pod_id"]) if self.rp else None
        age = now() - (l["requested_at"] or now())
        if pod and pod.status == "RUNNING" and pod.ssh_host and pod.ssh_port:
            rc, out = await ssh_run(self.ssh_key, pod.ssh_host, pod.ssh_port,
                                    "nvidia-smi --query-gpu=name --format=csv,noheader | head -8; df -h /workspace | tail -1",
                                    timeout=40)
            if rc == 0:
                t = now()
                self.db.update("leases", "id=?", (l["id"],), status="granted", granted_at=t, ssh_host=pod.ssh_host,
                               ssh_port=pod.ssh_port, price_hr=pod.price_hr, expires_at=t + l["max_hours"] * 3600,
                               heartbeat_at=None, job_state=None, job_name=None)
                self.db.update("pods", "id=?", (pod.id,), state="RUNNING", price_hr=pod.price_hr, last_seen=t,
                               machine_id=pod.machine_id, ssh_host=pod.ssh_host, ssh_port=pod.ssh_port)
                self._lease_event(l, "granted", f"{l['pod_name']} ready at ${pod.price_hr:.2f}/h for up to "
                                                f"{l['max_hours']}h", "normal",
                                  host=pod.ssh_host, port=pod.ssh_port, price_hr=pod.price_hr, probe=out.strip()[:400])
                return
        if age > self.provision_timeout_s:
            reason = f"pod not reachable after {age / 60:.0f} min"
            if pod and pod.machine_id:
                self.db.kv_set(self.p.name, "machine_blacklist", sorted(self._blacklist() | {pod.machine_id}))
            if pod and self.rp:
                try:
                    await self.rp.stop(pod.id)
                except RunpodError:
                    pass
            self.db.update("pods", "id=?", (l["pod_id"],), lease_id=None, idle_since=now())
            self.db.update("leases", "id=?", (l["id"],), status="failed", reason=reason)
            self._lease_event(l, "failed", reason + " (pod stopped; request a new lease to retry)", "major")

    async def _release(self, l, *, reason: str, stop_now: bool = False) -> None:
        t = now()
        self.db.update("leases", "id=?", (l["id"],), status="released", released_at=t, reason=reason)
        if l["pod_id"]:
            self.db.update("pods", "id=?", (l["pod_id"],), lease_id=None, idle_since=t)
            if stop_now and self.rp:
                try:
                    await self.rp.stop(l["pod_id"])
                    self.db.update("pods", "id=?", (l["pod_id"],), state="EXITED")
                except RunpodError as e:
                    log.warning("stop on release failed: %r", e)
        self._lease_event(l, "released", reason, "info")

    # ------------------------------------------------------------ reconcile + billing
    async def reconcile(self) -> None:
        if not self.rp:
            return
        ours = {r["id"]: r for r in self.db.all("SELECT * FROM pods WHERE project=? AND terminated=0", (self.p.name,))}
        if ours:
            live = {p.id: p for p in await self.rp.list_pods() if p.id in ours}
            t = now()
            for pid, row in ours.items():
                pod = live.get(pid)
                if pod is None:
                    self.db.update("pods", "id=?", (pid,), terminated=1, state="TERMINATED", last_seen=t)
                    if row["lease_id"]:
                        l = self.db.one("SELECT * FROM leases WHERE id=?", (row["lease_id"],))
                        if l and l["status"] in ("provisioning", "granted"):
                            self.db.update("leases", "id=?", (l["id"],), status="failed", reason="pod disappeared")
                            self._lease_event(l, "failed", "pod disappeared (terminated outside the lab?)", "major")
                    continue
                last = row["last_billed_at"] or t
                if row["state"] == "RUNNING" or pod.status == "RUNNING":
                    dt = min(t - last, 6 * 3600)
                    price = pod.price_hr or row["price_hr"] or 0
                    usd = price * dt / 3600
                    holder = row["last_experiment"] if row["lease_id"] else None
                    self.budget.record(usd, row["last_pool"] or next(iter(self.p.pools)), experiment_id=holder,
                                       pod_id=pid, note="idle" if not row["lease_id"] else "")
                    if holder and usd > 0:
                        self.db.x("UPDATE threads SET spent_usd=COALESCE(spent_usd,0)+? WHERE id=?", (usd, holder))
                self.db.update("pods", "id=?", (pid,), state=pod.status, last_seen=t, last_billed_at=t,
                               price_hr=pod.price_hr or row["price_hr"],
                               ssh_host=pod.ssh_host or row["ssh_host"], ssh_port=pod.ssh_port or row["ssh_port"])
                if pod.status == "RUNNING" and not row["lease_id"]:
                    idle_since = row["idle_since"] or t
                    if not row["idle_since"]:
                        self.db.update("pods", "id=?", (pid,), idle_since=t)
                    if t - idle_since > self.idle_stop_s:
                        try:
                            await self.rp.stop(pid)
                            self.db.update("pods", "id=?", (pid,), state="EXITED")
                            self.db.emit(self.p.name, "fleet.stopped", f"stopped idle pod {row['name']} "
                                         f"(idle {(t - idle_since) / 60:.0f} min)", severity="info")
                        except RunpodError as e:
                            log.warning("idle stop failed: %r", e)
        await self._enforce_limits()

    async def _enforce_limits(self) -> None:
        t = now()
        # overtime leases: warn at max_hours, stop at 1.1 × max_hours (the holder can `lab gpu extend`)
        for l in self.db.all("SELECT * FROM leases WHERE project=? AND status='granted' AND expires_at IS NOT NULL",
                             (self.p.name,)):
            h = holder_of(l)
            over = t - l["expires_at"]
            if over > 0.1 * l["max_hours"] * 3600:
                await self._release(l, reason=f"hard stop: exceeded max_hours={l['max_hours']} by 10%", stop_now=True)
                self.db.emit(self.p.name, "job.anomaly", f"{h}: lease {l['id']} hard-stopped for overtime",
                             severity="major", key=h)
            elif over > 0:
                self._once(h, f"overtime{l['id']}", "job.anomaly",
                           f"{h}: lease {l['id']} past max_hours={l['max_hours']}; hard stop at +10% "
                           f"(extend with `lab gpu extend`)", "major")
        # daily budget: alert at 80%, stop every lab pod at 100% (new leases are already refused)
        spent = self.budget.spent()
        cap = self.p.daily_usd
        day = int(day_start(self.cfg.timezone, t))
        if cap and spent >= 0.8 * cap and not self.db.kv_get(self.p.name, f"alert80:{day}"):
            self.db.kv_set(self.p.name, f"alert80:{day}", t)
            self.db.emit(self.p.name, "budget.alert", f"80% of today's ${cap:.0f} GPU budget spent (${spent:.0f})",
                         severity="major")
        if cap and spent >= cap and not self.db.kv_get(self.p.name, f"ceiling:{day}"):
            self.db.kv_set(self.p.name, f"ceiling:{day}", t)
            for l in self.db.all("SELECT * FROM leases WHERE project=? AND status='granted'", (self.p.name,)):
                await self._release(l, reason="daily budget reached", stop_now=True)
            self.db.emit(self.p.name, "budget.alert", f"daily budget reached (${spent:.0f} of ${cap:.0f}); "
                         "all lab pods stopped", severity="major")

    # ------------------------------------------------------------ watchdog
    async def watchdog(self) -> None:
        """Read the current job's labrun status on every granted pod; wake the holder on change."""
        t = now()
        for l in self.db.all("SELECT * FROM leases WHERE project=? AND status='granted'", (self.p.name,)):
            h = holder_of(l)
            job = l["job_name"] or l["experiment_id"] or h
            path = f"/workspace/lab/{job}/status.json"
            rc, out = await ssh_run(self.ssh_key, l["ssh_host"], l["ssh_port"],
                                    f"cat {shlex.quote(path)} 2>/dev/null || echo '{{}}'", timeout=30)
            if rc != 0:
                n = int(self.db.kv_get(self.p.name, f"unreach:{l['id']}", 0)) + 1
                self.db.kv_set(self.p.name, f"unreach:{l['id']}", n)
                if n == 3:
                    self.db.emit(self.p.name, "job.anomaly", f"{h}: pod {l['pod_name']} unreachable over ssh "
                                 f"(3 checks): {out.strip()[:200]}", severity="major", key=h)
                continue
            self.db.kv_set(self.p.name, f"unreach:{l['id']}", 0)
            st = parse_status(out)
            state, prev, hb = st.get("state"), l["job_state"], st.get("heartbeat_at")
            self.db.update("leases", "id=?", (l["id"],), job_state=state, job_status=json.dumps(st)[:4000],
                           job_status_at=t, heartbeat_at=hb)
            if state != prev:
                if state == "done":
                    self.db.emit(self.p.name, "job.finished", f"{h}: job {job} finished (exit {st.get('exit_code')})",
                                 severity="normal", key=h, payload=st)
                elif state == "failed":
                    self.db.emit(self.p.name, "job.anomaly", f"{h}: job {job} failed (exit {st.get('exit_code')}): "
                                 f"{str(st.get('message', ''))[-300:]}", severity="major", key=h, payload=st)
                elif state == "running" and prev is None:
                    self.db.emit(self.p.name, "job.started", f"{h}: job {job} running on {l['pod_name']}",
                                 severity="info", key=h)
            if state == "running" and hb and t - float(hb) > self.stall_s:
                self._once(h, f"stall{job}{int(float(hb))}", "job.anomaly",
                           f"{h}: job {job} has no heartbeat for {(t - float(hb)) / 60:.0f} min", "major")
            if state == "running":
                last = float(self.db.kv_get(self.p.name, f"check:{h}", 0) or 0)
                if t - last > self.check_s:
                    self.db.kv_set(self.p.name, f"check:{h}", t)
                    if last:
                        self.db.emit(self.p.name, "job.check", f"{h}: periodic progress check on {job}", key=h,
                                     payload=st)
            # a held GPU with nothing running is money burning: tell the holder after 20 min
            if state in (None, "done", "failed") and l["granted_at"]:
                since = float(st.get("heartbeat_at") or l["granted_at"])
                if t - since > self.idle_stop_s:
                    self._once(h, f"idlegpu{l['id']}{int(since)}", "job.idle",
                               f"{h}: lease {l['id']} holds a GPU with no job running for "
                               f"{(t - since) / 60:.0f} min — launch the next run or release it", "normal")
