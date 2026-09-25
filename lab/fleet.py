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

Shadeform (lab/shadeform.py) is a second source, addressed as the pseudo-cloud "SHADEFORM" in
cloud_order; the cheapest offer wins, whichever provider it is. Its VMs cannot be stopped:
where a Runpod pod is stopped (keeping /workspace), a Shadeform VM is deleted. They also boot
slower and less predictably, so they get their own provisioning timeout.

Vast.ai (lab/vast.py) is a third, "VAST": Docker containers on marketplace hosts. They stop and start
like Runpod pods; their probe waits for the onstart script, re-attaches the lab key through the API if
root is refused, and gives up at once on an instance Vast reports dead (its machine is blacklisted).
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
from .shadeform import CLOUD as SHADEFORM, READY_CHECK as SF_READY, Shadeform
from .vast import CLOUD as VAST, READY_CHECK as VAST_READY, Vast

log = logging.getLogger("lab.fleet")

ACTIVE_LEASE = ("provisioning", "granted")


def ssh_base(key: Path, host: str, port: int, user: str = "root") -> list[str]:
    # Pods are disposable and host:port pairs get reused by different pods, so
    # host keys are not pinned (teacher-swarm hit the same key churn).
    return ["ssh", "-i", str(key), "-p", str(port), "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=12",
            "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", f"{user}@{host}"]


async def ssh_run(key: Path, host: str, port: int, cmd: str, timeout: float = 45,
                  user: str = "root") -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*ssh_base(key, host, port, user), cmd,
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
    def __init__(self, db: DB, cfg: LabConfig, project: Project, runpod: Runpod | None, notify=None,
                 shadeform: Shadeform | None = None, vast: Vast | None = None):
        self.db = db
        self.cfg = cfg
        self.p = project
        self.rp = runpod
        self.sf = shadeform
        self.vast = vast
        self.budget = Budget(db, project, cfg.timezone)
        f = project.fleet
        self.max_gpus = int(f.get("max_gpus", 0))                      # 0 = no concurrency cap
        self.idle_stop_s = float(f.get("idle_stop_minutes", 20)) * 60
        self.stall_s = float(f.get("stall_minutes", 30)) * 60
        self.check_s = float(f.get("check_hours", 1)) * 3600
        self.provision_timeout_s = float(f.get("provision_timeout_minutes", 25)) * 60
        self.sf_provision_timeout_s = float(f.get("shadeform_provision_timeout_minutes", 60)) * 60
        self.vast_provision_timeout_s = float(f.get("vast_provision_timeout_minutes", 40)) * 60   # image pulls
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
    def api(self, cloud: str | None) -> Runpod | Shadeform | Vast | None:
        """The client that rents this cloud, or None if its key is not configured."""
        return self.sf if cloud == SHADEFORM else self.vast if cloud == VAST else self.rp

    def any_api(self) -> bool:
        return bool(self.rp or self.sf or self.vast)

    def provision_timeout(self, cloud: str | None) -> float:
        return (self.sf_provision_timeout_s if cloud == SHADEFORM else
                self.vast_provision_timeout_s if cloud == VAST else self.provision_timeout_s)

    async def prices(self) -> dict[str, dict]:
        """Runpod's price table with Shadeform's and Vast's merged in as the SHADEFORM and VAST clouds."""
        if self.any_api() and now() - self._prices_at > 3600:
            merged, ok = {k: dict(v) for k, v in self._prices.items()}, False
            for api in (self.rp, self.sf, self.vast):
                if not api:
                    continue
                try:
                    got = await api.gpu_prices()
                except Exception as e:
                    log.warning("gpu price refresh failed (%s): %r", type(api).__name__, e)
                    continue
                ok = True
                for t, v in got.items():
                    row = merged.setdefault(t, {})
                    row.update({k: x for k, x in v.items() if k != "max"})
                    row["max"] = {**(row.get("max") or {}), **(v.get("max") or {})}
            if ok:
                self._prices, self._prices_at = merged, now()
                self.db.kv_set(self.p.name, "gpu_prices", self._prices)
        return self._prices

    def clouds_for(self, gpu_type: str, count: int) -> list[str]:
        """Clouds from cloud_order that we hold a key for and that allow `count` GPUs of this type in one
        pod (unknown = allowed; a max of 0 means the cloud does not offer it, whatever its listed price).
        Shadeform and Vast only if they have an equivalent of the Runpod GPU id."""
        mx = (self._prices.get(gpu_type) or {}).get("max") or {}
        return [c for c in self.cloud_order if self.api(c) and (mx.get(c) is None or count <= mx[c])
                and (c not in (SHADEFORM, VAST) or gpu_type in self.api(c).gpu_map)]

    async def estimate(self, gpu_type: str | list[str], count: int, hours: float) -> tuple[float | None, str | None]:
        """Worst case over every (GPU type, cloud) the lease could land on: _create takes the cheapest
        with capacity, which may be the dearest one. Unpriced alternatives are ignored; None only if
        nothing is priced. Once granted, the reservation follows the pod's real price (Budget.committed)."""
        hints = self.p.fleet.get("price_hints", {})
        prices = await self.prices()
        worst: tuple[float, str] | None = None
        for t in ([gpu_type] if isinstance(gpu_type, str) else gpu_type):
            got = None
            for cloud in self.clouds_for(t, count):
                pr = (prices.get(t) or {}).get(cloud)
                if pr and (got is None or pr * count * hours > got[0]):
                    got = (pr * count * hours, cloud)
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
        if not self.any_api():
            return
        cache = self.db.kv_get(self.p.name, "stock", {}) or {}
        pending = self.db.kv_get(self.p.name, "stock_requests", []) or []
        if not force and not pending and now() - float(cache.get("at", 0)) < self.stock_refresh_s:
            return
        await self.prices()
        shapes = [(t, c, cl) for t, c in sorted(self._watch_shapes()) for cl in self.clouds_for(t, c)]
        if not shapes:
            return
        got: dict = {}
        for api in {self.api(cl) for _, _, cl in shapes}:
            try:
                got.update(await api.stock([s for s in shapes if self.api(s[2]) is api]))
            except Exception as e:
                log.warning("stock refresh failed (%s): %r", type(api).__name__, e)
        if not got:
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
        for pod in self.db.all("SELECT * FROM pods WHERE project=? AND terminated=0 AND state='RUNNING'",
                               (self.p.name,)):
            try:
                await self.stop_pod(pod["id"])
                self.db.update("pods", "id=?", (pod["id"],), lease_id=None)
            except RunpodError as e:
                log.warning("pause: stop %s failed: %r", pod["name"], e)

    # ------------------------------------------------------------ helpers
    async def stop_pod(self, pod_id: str) -> str:
        """Stop a lab pod. Shadeform VMs cannot stop, so they are deleted (and their disk with them).
        Returns what happened: "stopped" | "deleted"."""
        row = self.db.one("SELECT cloud FROM pods WHERE id=?", (pod_id,))
        cloud = row["cloud"] if row else None
        api = self.api(cloud)
        if api is None:
            raise RunpodError(f"no API key for {cloud or 'Runpod'} on the daemon")
        if cloud == SHADEFORM:
            await api.delete(pod_id)
            self.db.update("pods", "id=?", (pod_id,), state="TERMINATED", terminated=1)
            return "deleted"
        await api.stop(pod_id)
        self.db.update("pods", "id=?", (pod_id,), state="EXITED")
        return "stopped"

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
        await self.process_stops()

    async def process_stops(self) -> None:
        """Stop unleased pods flagged by `lab gpu release --stop` or by a stop that failed; retried until it works."""
        for pod in self.db.all("SELECT * FROM pods WHERE project=? AND stop_requested=1 AND terminated=0",
                               (self.p.name,)):
            if pod["lease_id"] or pod["state"] == "EXITED":
                self.db.update("pods", "id=?", (pod["id"],), stop_requested=0)
                continue
            try:
                how = await self.stop_pod(pod["id"])
            except RunpodError as e:
                self._once(pod["id"], f"stopfail{pod['idle_since']}", "fleet.stop_failed",
                           f"could not stop {pod['name']} (retrying): {e}"[:300], "major")
                continue
            self.db.update("pods", "id=?", (pod["id"],), stop_requested=0)
            self.db.emit(self.p.name, "fleet.stopped", f"{how} pod {pod['name']} on request", severity="info",
                         key=pod["last_experiment"])

    async def _grant(self, l) -> None:
        h = holder_of(l)
        if self.paused():
            self._once(h, f"paused-lease{l['id']}", "gpu.waiting",
                       f"{h}: GPUs are paused by the operator; lease {l['id']} waits", "normal")
            return
        if float(self.db.kv_get(self.p.name, f"capnext:{l['id']}", 0) or 0) > now():
            return  # waiting for Runpod capacity; retry later
        deny = None
        if not self.any_api():
            deny = "no GPU provider key (RUNPOD_API_KEY / SHADEFORM_API_KEY / VAST_API_KEY) is configured on the daemon"
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
                "gpu_count=? AND (state='RUNNING' OR COALESCE(cloud,'')!='SHADEFORM') "   # a deleted VM can't start
                "ORDER BY (last_experiment=?) DESC, (state='RUNNING') DESC, last_seen DESC",
                (self.p.name, t, l["gpu_count"], h))
            # a machine that failed to come up is not restarted (a stopped Vast instance stays on its host)
            if pod_row and self.api(pod_row["cloud"]) and pod_row["machine_id"] not in self._blacklist():
                break
            pod_row = None
        pool = l["pool"] or next(iter(self.p.pools))
        try:
            if pod_row:
                if pod_row["state"] != "RUNNING":
                    await self.api(pod_row["cloud"]).start(pod_row["id"])
                pod_id, name, cloud, gtype = pod_row["id"], pod_row["name"], pod_row["cloud"], pod_row["gpu_type"]
                self.db.update("pods", "id=?", (pod_id,), lease_id=l["id"], idle_since=None, last_pool=pool,
                               last_experiment=h, stop_requested=0)
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
                       cloud=cloud, gpu_type=gtype, granted_at=None, provisioning_at=now())
        note = (f" — a Shadeform VM: may take up to {self.sf_provision_timeout_s / 60:.0f} min to boot (wait, don't "
                "release); deleted, not stopped, when released or idle") if cloud == SHADEFORM else ""
        self._lease_event(l, "provisioning", f"{name} ({l['gpu_count']}× {gtype}, {cloud}){note}", "info")

    async def _create(self, l, types: list[str]) -> tuple[Pod, str]:
        """Try each candidate GPU type on each cloud that can host the count, cheapest first.
        Raises a capacity RunpodError only if every combination is out of stock."""
        env = {"PUBLIC_KEY": Path(str(self.ssh_key) + ".pub").read_text().strip()}
        combos = [(t, cl) for t in types for cl in self.clouds_for(t, l["gpu_count"])]
        known = {c: self.stock_of(c[0], l["gpu_count"], c[1]) for c in combos}
        # cheapest first, whichever provider; shapes the stock feed shows as sold out go last
        combos.sort(key=lambda c: (known[c] == "none", (self._prices.get(c[0]) or {}).get(c[1]) or float("inf")))
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
            # on a marketplace the same bad host keeps offering the cheapest price: skip blacklisted machines
            extra = {"avoid": self._blacklist()} if cloud == VAST else {}
            try:
                pod = await self.api(cloud).create_pod(
                    name=self._next_pod_name(), gpu_type=gtype, gpu_count=l["gpu_count"], cloud=cloud,
                    image=self.image, container_disk_gb=self.disk_gb, volume_gb=self.volume_gb, env=env, **extra)
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
            reason = f"no {'/'.join(n for n, a in (('Runpod', self.rp), ('Shadeform', self.sf), ('Vast', self.vast)) if a)} capacity for {(t - first) / 3600:.1f}h: {err}"
            self.db.update("leases", "id=?", (l["id"],), status="failed", reason=reason[:500])
            self._lease_event(l, "failed", reason[:300] + " — try other GPU types/counts", "major")
            return
        self.db.update("leases", "id=?", (l["id"],), reason=f"waiting for capacity since {first:.0f}")
        self.db.kv_set(self.p.name, f"capnext:{l['id']}", t + self.capacity_retry_s)

    async def _check_provisioning(self, l) -> None:
        api = self.api(l["cloud"])
        pod = await api.get_pod(l["pod_id"]) if api else None
        # timed from the pod's creation, not the request (which may have waited hours for capacity)
        age = now() - (l["provisioning_at"] or l["requested_at"] or now())
        vast = l["cloud"] == VAST
        row = self.db.one("SELECT created_at FROM pods WHERE id=?", (l["pod_id"],))
        fresh = bool(row) and (row["created_at"] or 0) >= (l["requested_at"] or 0)    # created for this lease
        if pod is None or pod.status != "RUNNING" or not (pod.ssh_host and pod.ssh_port):
            why = ("pod not found" if pod is None else f"pod {pod.status}" if pod.status != "RUNNING"
                   else "no ssh address yet")
            if vast and pod and (msg := str((pod.raw or {}).get("status_msg") or "").strip()):
                why += f" ({msg[:200]})"          # Vast says what it is doing: pulling the image, or why it failed
            if vast and fresh and pod and pod.status == "ERROR":
                # a new Vast container that exited/went offline never comes up (a restarted one still
                # reads "exited" until its host schedules it, so it gets the normal timeout)
                age = float("inf")
        else:
            sf = l["cloud"] == SHADEFORM
            rc, out = await ssh_run(self.ssh_key, pod.ssh_host, pod.ssh_port,
                                    (SF_READY if sf else VAST_READY if vast else "") +
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
            # a failed probe used to be silent: the lease sat in 'provisioning' with no clue why
            last = (out.strip().splitlines() or [""])[-1][:200]
            why = f"ssh probe root@{pod.ssh_host}:{pod.ssh_port} rc={rc}: {last or 'no output'}"
            if sf and rc == 255 and (user := (pod.raw or {}).get("ssh_user")):
                # root refused: the VM's own user has the lab key too (ssh_key_id), so ask it why
                urc, uout = await ssh_run(self.ssh_key, pod.ssh_host, pod.ssh_port,
                                          "sudo -n journalctl -u init-script --no-pager -n 2 -o cat 2>&1 | tail -2",
                                          timeout=30, user=user)
                if urc == 0 and uout.strip():
                    why += f"; startup script: {' | '.join(uout.strip().splitlines())[:300]}"
            if vast and rc == 255 and "denied" in out.lower():
                # root refused our key: put it on the instance again through Vast's API (needs no ssh)
                try:
                    await api.attach_key(pod.id, Path(str(self.ssh_key) + ".pub").read_text().strip())
                    why += "; lab key re-attached through the Vast API"
                except RunpodError as e:
                    why += f"; re-attaching the lab key failed: {str(e)[:150]}"
            log.info("lease %s: %s", l["id"], why)
        self.db.update("leases", "id=?", (l["id"],), reason=why)
        if age > self.provision_timeout(l["cloud"]):
            reason = (f"pod failed to start (last check: {why})" if age == float("inf") else
                      f"pod not reachable after {age / 60:.0f} min (last check: {why})")
            if pod and pod.machine_id:
                self.db.kv_set(self.p.name, "machine_blacklist", sorted(self._blacklist() | {pod.machine_id}))
            self.db.update("pods", "id=?", (l["pod_id"],), lease_id=None, idle_since=now())
            how = "stopped"
            if pod:
                try:
                    if vast and fresh:
                        # it never came up, so it holds nothing; stopped, it would bill disk on a blacklisted host
                        await api.delete(pod.id)
                        self.db.update("pods", "id=?", (pod.id,), state="TERMINATED", terminated=1)
                        how = "deleted"
                    else:
                        how = await self.stop_pod(pod.id)
                except RunpodError:
                    pass
            self.db.update("leases", "id=?", (l["id"],), status="failed", reason=reason)
            self._lease_event(l, "failed", reason + f" (pod {how}; request a new lease to retry)", "major")

    async def _release(self, l, *, reason: str, stop_now: bool = False) -> None:
        t = now()
        self.db.update("leases", "id=?", (l["id"],), status="released", released_at=t, reason=reason)
        if l["pod_id"]:
            self.db.update("pods", "id=?", (l["pod_id"],), lease_id=None, idle_since=t)
            if stop_now:
                try:
                    await self.stop_pod(l["pod_id"])
                except RunpodError as e:
                    log.warning("stop on release failed: %r", e)
                    self.db.update("pods", "id=?", (l["pod_id"],), stop_requested=1)   # process_stops retries
        self._lease_event(l, "released", reason, "info")

    # ------------------------------------------------------------ reconcile + billing
    async def reconcile(self) -> None:
        # pods whose provider has no key on this daemon are left alone (never marked gone)
        ours = {r["id"]: r for r in self.db.all("SELECT * FROM pods WHERE project=? AND terminated=0", (self.p.name,))
                if self.api(r["cloud"])}
        if ours:
            live: dict[str, Pod] = {}
            down = set()           # a provider whose API failed: its pods wait for the next pass
            for api in {self.api(r["cloud"]) for r in ours.values()}:
                try:
                    live.update({p.id: p for p in await api.list_pods() if p.id in ours})
                except Exception as e:
                    log.warning("list pods failed (%s): %r", type(api).__name__, e)
                    down.add(api)
            t = now()
            for pid, row in ours.items():
                if self.api(row["cloud"]) in down:
                    continue
                pod = live.get(pid)
                if pod is None or pod.status == "TERMINATED":
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
                            how = await self.stop_pod(pid)
                            self.db.emit(self.p.name, "fleet.stopped", f"{how} idle pod {row['name']} "
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
