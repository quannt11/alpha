"""Vast.ai REST client: a GPU marketplace (Docker containers on hosts' machines), the lab's third GPU source.

Only the daemon constructs this: it is the one process that holds VAST_API_KEY. To the fleet, Vast is one
more cloud ("VAST") next to Runpod's COMMUNITY/SECURE and SHADEFORM, addressed with the same Runpod GPU ids,
and it answers with the same `Pod` shape. Unlike Shadeform, an instance can be stopped and started again
(its disk is kept and billed at a small storage rate), so the fleet treats it like a Runpod pod.

Getting root ssh right the first time (Shadeform locked the lab out of leases 10-12):
- Vast copies the *account's* ssh keys into an instance when it is created and never afterwards, so the lab
  key is registered on the account before every create (matched on the key itself, not a name);
- it is also attached to the instance explicitly after the create, and appended by the onstart script;
- if the readiness probe is still refused, the fleet re-attaches it through the API (`attach_key`) — the
  instance's own API is the one way in that does not need ssh.
"""
from __future__ import annotations

import json
import logging
import shlex
import time

import httpx

from .runpod import CAPACITY_HINTS, Pod, RunpodError

log = logging.getLogger("lab.vast")

HOST = "https://console.vast.ai"
CLOUD = "VAST"

# Runpod GPU ids (what leases name) → Vast (gpu_name, minimum GPU memory in GB or None).
GPU_MAP: dict[str, tuple[str, int | None]] = {
    "NVIDIA H100 80GB HBM3": ("H100 SXM", None),
    "NVIDIA H100 PCIe": ("H100 PCIE", None),
    "NVIDIA H100 NVL": ("H100 NVL", None),
    "NVIDIA H200": ("H200", None),
    "NVIDIA H200 NVL": ("H200 NVL", None),
    "NVIDIA B200": ("B200", None),
    "NVIDIA A100-SXM4-80GB": ("A100 SXM4", 70),
    "NVIDIA A100 80GB PCIe": ("A100 PCIE", 70),
    "NVIDIA L40S": ("L40S", None),
    "NVIDIA L40": ("L40", None),
    "NVIDIA RTX A6000": ("RTX A6000", None),
    "NVIDIA RTX 6000 Ada Generation": ("RTX 6000Ada", None),
    "NVIDIA GeForce RTX 4090": ("RTX 4090", None),
    "NVIDIA GeForce RTX 5090": ("RTX 5090", None),
    "NVIDIA RTX PRO 6000 Blackwell Server Edition": ("RTX PRO 6000 S", None),
}

# an offer taken between search and create answers with one of these
CAPACITY = CAPACITY_HINTS + ("no_such_ask", "not available", "already rented", "no longer available", "is rented")

LOG, DONE = "/var/log/lab-bootstrap.log", "/var/lib/lab-bootstrap.done"
# Vast runs this as root at every container start, after its own ssh setup. Key first, so nothing slow can
# lock the lab out; .no_auto_tmux stops Vast's ssh login from wrapping sessions in tmux.
ONSTART = """exec >>{log} 2>&1
set -x
touch /root/.no_auto_tmux
mkdir -p /root/.ssh && chmod 700 /root/.ssh
grep -qxF {key} /root/.ssh/authorized_keys 2>/dev/null || echo {key} >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
mkdir -p /workspace
command -v rsync >/dev/null || { timeout 300 apt-get -o DPkg::Lock::Timeout=120 update -qq
  DEBIAN_FRONTEND=noninteractive timeout 300 apt-get -o DPkg::Lock::Timeout=120 install -y -qq rsync; }
touch {done}
""".replace("{log}", LOG).replace("{done}", DONE)

# Prefixed to the fleet's readiness probe on Vast instances.
READY_CHECK = (f"test -e {DONE} || {{ echo \"bootstrap still running: $(tail -n1 {LOG} 2>/dev/null)\"; exit 3; }}; ")


class VastError(RunpodError):
    """The fleet catches RunpodError for every provider."""


def ssh_address(i: dict) -> tuple[str | None, int | None]:
    """Direct ssh (the host's public IP and its port for 22/tcp) when mapped, else Vast's ssh proxy."""
    ports = i.get("ports") if isinstance(i.get("ports"), dict) else {}
    direct = ports.get("22/tcp") or []
    if i.get("public_ipaddr") and direct and direct[0].get("HostPort"):
        return str(i["public_ipaddr"]).strip(), int(direct[0]["HostPort"])
    if i.get("ssh_host") and i.get("ssh_port"):
        return i["ssh_host"], int(i["ssh_port"])
    return None, None


def status(i: dict) -> str:
    """Runpod-style status. A fresh container that exited or went offline never reaches running."""
    actual = str(i.get("actual_status") or "").lower()
    if actual == "running":
        return "RUNNING"
    if str(i.get("intended_status") or "").lower() == "stopped":
        return "EXITED"
    if actual in ("exited", "offline", "unknown"):
        return "ERROR"
    return "CREATED"     # None / created / loading / scheduling


def normalise(i: dict) -> Pod:
    host, port = ssh_address(i)
    return Pod(id=str(i["id"]), name=i.get("label") or "", status=status(i), gpu_type=i.get("gpu_name"),
               gpu_count=int(i.get("num_gpus") or 0), price_hr=float(i.get("dph_total") or 0), ssh_host=host,
               ssh_port=port, machine_id=str(i["machine_id"]) if i.get("machine_id") else None, cloud=CLOUD, raw=i)


class Vast:
    OFFERS_TTL_S = 60

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None, cfg: dict | None = None):
        if not api_key:
            raise VastError("VAST_API_KEY is not configured")
        cfg = cfg or {}
        self.key = api_key
        self.client = client or httpx.AsyncClient(timeout=60)
        self.gpu_map = {**GPU_MAP, **{k: tuple(v) for k, v in (cfg.get("gpu_map") or {}).items()}}
        # which hosts are good enough to rent from ([vast] in lab.toml)
        self.min_reliability = float(cfg.get("min_reliability", 0.97))
        self.min_inet_down = float(cfg.get("min_inet_down_mbps", 200))
        self.max_inet_usd_per_tb = float(cfg.get("max_inet_usd_per_tb", 20))
        self.min_cuda = float(cfg.get("min_cuda", 12.8))
        self.min_hours = float(cfg.get("min_offer_hours", 24))
        self.datacenter_only = bool(cfg.get("datacenter_only", False))
        self.image = cfg.get("image")        # None = the fleet's image
        self._offers: list[dict] = []
        self._offers_at = 0.0
        self._offers_disk = 0

    async def _req(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
        r = await self.client.request(method, HOST + path, json=body, params=params,
                                      headers={"Authorization": f"Bearer {self.key}"})
        text = r.text[:500]
        if r.status_code >= 400:
            raise VastError(f"vast {method} {path}: {r.status_code} {text}", r.status_code,
                            capacity=any(h in text.lower() for h in CAPACITY))
        d = r.json() if r.content else None
        if isinstance(d, dict) and d.get("success") is False:     # Vast reports some failures with a 200
            raise VastError(f"vast {method} {path}: {text}", 400, capacity=any(h in text.lower() for h in CAPACITY))
        return d

    # ------------------------------------------------------------ catalog
    async def catalog(self, disk_gb: int = 0) -> list[dict]:
        """Every rentable on-demand offer for a mapped GPU on a host that passes the lab's filters."""
        if time.time() - self._offers_at > self.OFFERS_TTL_S or disk_gb > self._offers_disk:
            q = {"verified": {"eq": True}, "external": {"eq": False}, "rentable": {"eq": True},
                 "rented": {"eq": False}, "type": "on-demand", "limit": 1000,
                 "gpu_name": {"in": sorted({v[0] for v in self.gpu_map.values()})},
                 "reliability2": {"gte": self.min_reliability}, "inet_down": {"gte": self.min_inet_down},
                 "inet_down_cost": {"lte": self.max_inet_usd_per_tb / 1000}, "cuda_max_good": {"gte": self.min_cuda},
                 "duration": {"gte": self.min_hours * 3600}, "direct_port_count": {"gte": 1},
                 "order": [["dph_total", "asc"]]}
            if disk_gb:
                q |= {"disk_space": {"gte": disk_gb}, "allocated_storage": disk_gb}
            if self.datacenter_only:
                q["hosting_type"] = {"eq": 1}
            self._offers = (await self._req("POST", "/api/v0/bundles/", q) or {}).get("offers", [])
            self._offers_at, self._offers_disk = time.time(), disk_gb
        return self._offers

    def _matches(self, o: dict, gpu_type: str) -> bool:
        want = self.gpu_map.get(gpu_type)
        return bool(want) and o.get("gpu_name") == want[0] and (not want[1] or (o.get("gpu_ram") or 0) >= want[1] * 1000)

    async def offers(self, gpu_type: str, count: int, disk_gb: int = 0, avoid=()) -> list[dict]:
        """Offers that can be rented now, cheapest first, skipping machines the fleet blacklisted."""
        out = [o for o in await self.catalog(disk_gb) if self._matches(o, gpu_type) and o.get("num_gpus") == count
               and str(o.get("machine_id")) not in avoid]
        return sorted(out, key=lambda o: o.get("dph_total") or 0)

    async def gpu_prices(self) -> dict[str, dict]:
        """Same shape as Runpod.gpu_prices, under the VAST cloud: cheapest $/GPU/h and the largest count."""
        out: dict[str, dict] = {}
        offers = await self.catalog()
        for gid in self.gpu_map:
            os_ = [o for o in offers if self._matches(o, gid)]
            if os_:
                out[gid] = {CLOUD: round(min(o["dph_total"] / max(1, o["num_gpus"]) for o in os_), 4),
                            "max": {CLOUD: max(o["num_gpus"] for o in os_)}}
        return out

    async def stock(self, shapes: list[tuple[str, int, str]]) -> dict[tuple[str, int, str], str | None]:
        """Rentable offers per shape: 3+ High, 2 Medium, 1 Low, none None."""
        out: dict[tuple[str, int, str], str | None] = {}
        for shape in shapes:
            n = len(await self.offers(shape[0], shape[1]))
            out[shape] = "High" if n >= 3 else "Medium" if n == 2 else "Low" if n == 1 else None
        return out

    # ------------------------------------------------------------ ssh keys
    async def ensure_account_key(self, public_key: str) -> None:
        """Register the lab key on the account if absent: Vast copies account keys into new instances only."""
        blob = public_key.split()[1]
        d = await self._req("GET", "/api/v0/ssh/")
        keys = d if isinstance(d, list) else (d or {}).get("ssh_keys") or (d or {}).get("keys") or []
        if any(blob in str(k.get("key") or k.get("public_key") or k.get("ssh_key") or "").split()
               for k in keys if isinstance(k, dict) and not k.get("deleted_at")):
            return
        log.info("vast: registering the lab ssh key on the account")
        try:
            await self._req("POST", "/api/v0/ssh/", {"ssh_key": public_key})
        except VastError as e:
            if e.status in (401, 403):
                raise
            # attach_key after the create and the fleet's probe still get the key onto the instance
            log.warning("vast: registering the lab key failed: %s", e)

    async def attach_key(self, pod_id: str, public_key: str) -> None:
        """Put the lab key into root's authorized_keys on this instance, through the API (no ssh needed)."""
        try:
            await self._req("POST", f"/api/v0/instances/{pod_id}/ssh/", {"ssh_key": public_key})
        except VastError as e:
            if "already associated" not in str(e):    # the account key got there first: that is success
                raise

    # ------------------------------------------------------------ instances
    async def list_pods(self) -> list[Pod]:
        rows, params = [], {"select_filters": "{}", "order_by": json.dumps([{"col": "id", "dir": "asc"}]), "limit": 25}
        for _ in range(100):
            d = await self._req("GET", "/api/v1/instances/", params=params) or {}
            rows += d.get("instances") or []
            if not d.get("next_token"):
                break
            params["after_token"] = d["next_token"]
        return [normalise(i) for i in rows]

    async def get_pod(self, pod_id: str) -> Pod | None:
        try:
            i = (await self._req("GET", f"/api/v0/instances/{pod_id}/", params={"owner": "me"}) or {}).get("instances")
        except VastError as e:
            if e.status == 404:
                return None
            raise
        return normalise(i) if i else None

    async def create_pod(self, *, name: str, gpu_type: str, gpu_count: int, cloud: str, image: str,
                         container_disk_gb: int, volume_gb: int, env: dict[str, str], avoid=()) -> Pod:
        """Rent the cheapest offer, falling through to the next when one is taken. A Vast instance has one
        disk (container + /workspace), sized container_disk_gb + volume_gb."""
        if gpu_type not in self.gpu_map:
            raise VastError(f"{gpu_type} has no Vast equivalent ([vast] gpu_map)", 400)
        disk = container_disk_gb + volume_gb
        offers = await self.offers(gpu_type, gpu_count, disk, avoid)
        if not offers:
            raise VastError(f"no Vast stock for {gpu_count}× {gpu_type}", 503, capacity=True)
        key = env["PUBLIC_KEY"]
        await self.ensure_account_key(key)
        body = {"client_id": "me", "image": self.image or image, "disk": disk, "label": name,
                "runtype": "ssh_direc ssh_proxy", "onstart": ONSTART.replace("{key}", shlex.quote(key)),
                "env": {"PUBLIC_KEY": key}, "cancel_unavail": True}
        errors: list[VastError] = []
        for o in offers[:5]:
            try:
                iid = str((await self._req("PUT", f"/api/v0/asks/{o['id']}/", body))["new_contract"])
            except VastError as e:
                if e.status in (401, 402, 403) or not e.capacity:
                    raise
                log.info("vast offer %s (machine %s): %s", o["id"], o.get("machine_id"), e)
                errors.append(e)
                continue
            self._offers_at = 0.0          # that offer is gone
            try:
                await self.attach_key(iid, key)
            except VastError as e:         # the account key and onstart still cover it; the probe retries
                log.warning("vast: attach key to %s failed: %s", iid, e)
            pod = await self.get_pod(iid) or Pod(iid, name, "CREATED", o.get("gpu_name"), gpu_count,
                                                 float(o.get("dph_total") or 0), None, None,
                                                 str(o.get("machine_id")), CLOUD, {})
            pod.name = name
            return pod
        raise VastError(f"every Vast offer for {gpu_count}× {gpu_type} was taken; last: {errors[-1]}",
                        errors[-1].status, capacity=True)

    async def start(self, pod_id: str) -> None:
        await self._req("PUT", f"/api/v0/instances/{pod_id}/", {"state": "running"})

    async def stop(self, pod_id: str) -> None:
        await self._req("PUT", f"/api/v0/instances/{pod_id}/", {"state": "stopped"})

    async def delete(self, pod_id: str) -> None:
        await self._req("DELETE", f"/api/v0/instances/{pod_id}/", {})
