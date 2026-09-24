"""Shadeform REST client: GPU VMs from many clouds behind one API, the lab's second GPU source.

Only the daemon constructs this: it is the one process that holds SHADEFORM_API_KEY. To the fleet,
Shadeform is one more cloud ("SHADEFORM") next to Runpod's COMMUNITY/SECURE, addressed with the
same Runpod GPU ids, and it answers with the same `Pod` shape. Two differences the fleet handles:
- an instance cannot be stopped, only deleted (billing ends and its disk goes with it);
- instances are plain Ubuntu VMs, so a startup script makes them look like a Runpod pod: a large
  /workspace and root ssh with the lab key, which is how labrun, `lab push` and the watchdog work.
"""
from __future__ import annotations

import base64
import logging
import shlex
import time

import httpx

from .runpod import CAPACITY_HINTS, Pod, RunpodError

log = logging.getLogger("lab.shadeform")

API = "https://api.shadeform.ai/v1"
CLOUD = "SHADEFORM"
MAX_BOOT_S = 45 * 60      # offers advertising a slower boot are skipped (fleet waits up to 60 min)

# Runpod GPU ids (what leases name) → Shadeform (gpu_type, interconnect); None = any interconnect.
GPU_MAP: dict[str, tuple[str, str | None]] = {
    "NVIDIA H100 80GB HBM3": ("H100", "sxm5"),
    "NVIDIA H100 PCIe": ("H100", "pcie"),
    "NVIDIA H100 NVL": ("H100_nvl", None),
    "NVIDIA H200": ("H200", None),
    "NVIDIA B200": ("B200", None),
    "NVIDIA A100-SXM4-80GB": ("A100_80G", "sxm4"),
    "NVIDIA A100 80GB PCIe": ("A100_80G", "pcie"),
    "NVIDIA L40S": ("L40S", None),
    "NVIDIA L40": ("L40", None),
    "NVIDIA RTX A6000": ("A6000", None),
    "NVIDIA GeForce RTX 4090": ("RTX4090", None),
    "NVIDIA GeForce RTX 5090": ("RTX5090", None),
    "NVIDIA RTX PRO 6000 Blackwell Server Edition": ("RTXPro6000", None),
}

CAPACITY = CAPACITY_HINTS + ("not available", "out of stock", "sold out", "no availability")

STATUS = {"active": "RUNNING", "creating": "CREATED", "pending_provider": "CREATED", "pending": "CREATED",
          "error": "ERROR", "deleting": "TERMINATED", "deleted": "TERMINATED"}

# Runs as root once the VM is active. Root ssh is enabled last, so a successful probe means it all ran.
BOOTSTRAP = """#!/bin/bash
# lab bootstrap: make this VM look like a Runpod pod (big /workspace, root ssh with the lab key)
best=$(df -P --output=avail,target -x tmpfs -x devtmpfs -x overlay -x squashfs 2>/dev/null | tail -n +2 \\
       | sort -n | tail -1 | awk '{print $2}')
if [ -n "$best" ] && [ "$best" != "/" ] && [ ! -e /workspace ]; then
  mkdir -p "$best/workspace" && ln -sfn "$best/workspace" /workspace
fi
mkdir -p /workspace
command -v rsync >/dev/null || { apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rsync; }
mkdir -p /root/.ssh /etc/ssh/sshd_config.d
echo {key} > /root/.ssh/authorized_keys
chown -R root:root /root/.ssh && chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys
echo 'PermitRootLogin prohibit-password' > /etc/ssh/sshd_config.d/00-lab.conf
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
"""


class ShadeformError(RunpodError):
    """The fleet catches RunpodError for every provider."""


def normalise(i: dict) -> Pod:
    rt = i.get("rental_terms") or {}
    cents = i.get("hourly_price")
    price = float(rt["hourly_price"]) if rt.get("hourly_price") else (cents or 0) / 100
    cfg = i.get("configuration") or {}
    return Pod(id=i["id"], name=i.get("name", ""), status=STATUS.get(str(i.get("status")), "UNKNOWN"),
               gpu_type=cfg.get("gpu_type"), gpu_count=int(cfg.get("num_gpus") or 0), price_hr=price,
               ssh_host=i.get("ip") or None, ssh_port=int(i["ssh_port"]) if i.get("ssh_port") else None,
               machine_id=i.get("cloud_assigned_id"), cloud=CLOUD, raw=i)


class Shadeform:
    TYPES_TTL_S = 60

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None,
                 gpu_map: dict[str, list | tuple] | None = None):
        if not api_key:
            raise ShadeformError("SHADEFORM_API_KEY is not configured")
        self.key = api_key
        self.client = client or httpx.AsyncClient(timeout=60)
        self.gpu_map = {**GPU_MAP, **{k: tuple(v) for k, v in (gpu_map or {}).items()}}
        self._types: list[dict] = []
        self._types_at = 0.0

    async def _req(self, method: str, path: str, body: dict | None = None):
        r = await self.client.request(method, API + path, json=body, headers={"X-API-KEY": self.key})
        if r.status_code >= 400:
            text = r.text[:500]
            raise ShadeformError(f"shadeform {method} {path}: {r.status_code} {text}", r.status_code,
                                 capacity=any(h in text.lower() for h in CAPACITY))
        return r.json() if r.content else None

    # ------------------------------------------------------------ catalog
    async def types(self) -> list[dict]:
        if time.time() - self._types_at > self.TYPES_TTL_S:
            self._types = (await self._req("GET", "/instances/types") or {}).get("instance_types", [])
            self._types_at = time.time()
        return self._types

    def _matches(self, t: dict, gpu_type: str) -> bool:
        want = self.gpu_map.get(gpu_type)
        c = t.get("configuration") or {}
        return bool(want) and c.get("gpu_type") == want[0] and (want[1] is None or c.get("interconnect") == want[1])

    @staticmethod
    def _regions(t: dict) -> list[str]:
        """Regions where this type is rentable on demand now; none if it boots slower than MAX_BOOT_S."""
        if (t.get("boot_time") or {}).get("max_boot_in_sec", 0) > MAX_BOOT_S:
            return []
        return [a["region"] for a in t.get("availability") or []
                if a.get("available") and a.get("rental_type", "on_demand") == "on_demand"]

    async def offers(self, gpu_type: str, count: int) -> list[tuple[dict, str]]:
        """(instance type, region) pairs that can be rented now, cheapest first."""
        out = [(t, r) for t in await self.types()
               if self._matches(t, gpu_type) and (t.get("configuration") or {}).get("num_gpus") == count
               for r in self._regions(t)]
        return sorted(out, key=lambda o: o[0].get("hourly_price") or 0)

    async def gpu_prices(self) -> dict[str, dict]:
        """Same shape as Runpod.gpu_prices, under the SHADEFORM cloud: cheapest $/GPU/h (in stock if any)."""
        out: dict[str, dict] = {}
        types = await self.types()
        for gid in self.gpu_map:
            ts = [t for t in types if self._matches(t, gid)]
            if not ts:
                continue
            pool = [t for t in ts if self._regions(t)] or ts
            per_gpu = min((t.get("hourly_price") or 0) / 100 / max(1, t["configuration"]["num_gpus"]) for t in pool)
            out[gid] = {CLOUD: round(per_gpu, 4),
                        "max": {CLOUD: max(t["configuration"]["num_gpus"] for t in ts)}}
        return out

    async def stock(self, shapes: list[tuple[str, int, str]]) -> dict[tuple[str, int, str], str | None]:
        """Rentable (type, region) pairs per shape: 3+ High, 2 Medium, 1 Low, none None."""
        out: dict[tuple[str, int, str], str | None] = {}
        for shape in shapes:
            n = len(await self.offers(shape[0], shape[1]))
            out[shape] = "High" if n >= 3 else "Medium" if n == 2 else "Low" if n == 1 else None
        return out

    # ------------------------------------------------------------ instances
    async def list_pods(self) -> list[Pod]:
        d = await self._req("GET", "/instances")
        return [normalise(i) for i in (d or {}).get("instances", [])]

    async def get_pod(self, pod_id: str) -> Pod | None:
        try:
            return normalise(await self._req("GET", f"/instances/{pod_id}/info"))
        except ShadeformError as e:
            if e.status == 404:
                return None
            raise

    async def create_pod(self, *, name: str, gpu_type: str, gpu_count: int, cloud: str, image: str,
                         container_disk_gb: int, volume_gb: int, env: dict[str, str]) -> Pod:
        """Rent the cheapest in-stock offer, falling through to the next on errors other than auth.
        `image`/`container_disk_gb`/`volume_gb` are Runpod's; a VM brings its own OS and disk."""
        if gpu_type not in self.gpu_map:
            raise ShadeformError(f"{gpu_type} has no Shadeform equivalent (fleet.shadeform.gpu_map)", 400)
        offers = await self.offers(gpu_type, gpu_count)
        if not offers:
            raise ShadeformError(f"no Shadeform stock for {gpu_count}× {gpu_type}", 503, capacity=True)
        script = base64.b64encode(BOOTSTRAP.replace("{key}", shlex.quote(env["PUBLIC_KEY"])).encode()).decode()
        errors: list[ShadeformError] = []
        for t, region in offers:
            body = {"cloud": t["cloud"], "region": region, "shade_instance_type": t["shade_instance_type"],
                    "shade_cloud": True, "name": name.replace("_", "-").lower(),
                    "launch_configuration": {"type": "script", "script_configuration": {"base64_script": script}},
                    "tags": ["lab", name]}
            os_ = next((o for o in (t.get("configuration") or {}).get("os_options") or [] if "cuda" in o), None)
            if os_:
                body["os"] = os_
            try:
                iid = (await self._req("POST", "/instances/create", body))["id"]
            except ShadeformError as e:
                if e.status in (401, 403):
                    raise
                log.info("shadeform %s/%s %s: %s", t["cloud"], region, t["shade_instance_type"], e)
                errors.append(e)
                continue
            pod = await self.get_pod(iid)
            if pod is None:
                pod = Pod(iid, name, "CREATED", gpu_type, gpu_count, (t.get("hourly_price") or 0) / 100,
                          None, None, None, CLOUD, {})
            pod.name = name
            return pod
        raise ShadeformError(f"every Shadeform offer for {gpu_count}× {gpu_type} failed; last: {errors[-1]}",
                             errors[-1].status, capacity=all(e.capacity for e in errors))

    async def delete(self, pod_id: str) -> None:
        await self._req("POST", f"/instances/{pod_id}/delete")
