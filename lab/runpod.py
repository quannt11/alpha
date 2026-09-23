"""Runpod REST v1 client (pods) + GraphQL for GPU prices.

Only the daemon constructs this: it is the one process that holds the API key.
Responses are normalised so the rest of the code sees one pod shape.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("lab.runpod")

REST = "https://rest.runpod.io/v1"
GRAPHQL = "https://api.runpod.io/graphql"


class RunpodError(Exception):
    def __init__(self, msg: str, status: int | None = None, capacity: bool = False):
        super().__init__(msg)
        self.status = status
        self.capacity = capacity


@dataclass
class Pod:
    id: str
    name: str
    status: str            # RUNNING | EXITED | TERMINATED | CREATED | ...
    gpu_type: str | None
    gpu_count: int
    price_hr: float
    ssh_host: str | None
    ssh_port: int | None
    machine_id: str | None
    cloud: str | None
    raw: dict


def normalise(p: dict) -> Pod:
    status = p.get("desiredStatus") or p.get("status") or "UNKNOWN"
    gpu = p.get("gpu") or {}
    gpu_type = gpu.get("id") or gpu.get("gpuTypeId") or (p.get("machine") or {}).get("gpuTypeId")
    gpu_count = int(gpu.get("count") or p.get("gpuCount") or 0)
    price = p.get("costPerHr") or p.get("adjustedCostPerHr") or p.get("cost") or 0
    host = port = None
    ssh = (p.get("ssh") or {}).get("direct") or {}
    if ssh.get("host"):
        host, port = ssh.get("host"), ssh.get("port")
    elif p.get("publicIp") and isinstance(p.get("portMappings"), dict):
        host, port = p["publicIp"], p["portMappings"].get("22")
    else:
        for rp in (p.get("runtime") or {}).get("ports") or []:
            if rp.get("privatePort", rp.get("private")) == 22 and rp.get("isIpPublic", True):
                host, port = rp.get("ip"), rp.get("publicPort", rp.get("public"))
    return Pod(id=p["id"], name=p.get("name", ""), status=str(status).upper(), gpu_type=gpu_type,
               gpu_count=gpu_count, price_hr=float(price or 0), ssh_host=host,
               ssh_port=int(port) if port else None, machine_id=p.get("machineId"),
               cloud=p.get("cloudType") or p.get("cloud"), raw=p)


CAPACITY_HINTS = ("no instances", "not enough", "capacity", "unavailable", "no longer any instances",
                  "could not find", "no gpu")


class Runpod:
    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        if not api_key:
            raise RunpodError("RUNPOD_API_KEY is not configured")
        self.key = api_key
        self.client = client or httpx.AsyncClient(timeout=60)

    async def _req(self, method: str, path: str, body: dict | None = None):
        r = await self.client.request(method, REST + path, json=body,
                                      headers={"Authorization": f"Bearer {self.key}"})
        if r.status_code >= 400:
            text = r.text[:500]
            raise RunpodError(f"runpod {method} {path}: {r.status_code} {text}", r.status_code,
                              capacity=any(h in text.lower() for h in CAPACITY_HINTS))
        return r.json() if r.content else None

    async def list_pods(self) -> list[Pod]:
        d = await self._req("GET", "/pods")
        items = d if isinstance(d, list) else (d or {}).get("pods", [])
        return [normalise(p) for p in items]

    async def get_pod(self, pod_id: str) -> Pod | None:
        try:
            return normalise(await self._req("GET", f"/pods/{pod_id}"))
        except RunpodError as e:
            if e.status == 404:
                return None
            raise

    async def create_pod(self, *, name: str, gpu_type: str, gpu_count: int, cloud: str, image: str,
                         container_disk_gb: int, volume_gb: int, env: dict[str, str]) -> Pod:
        body = {
            "name": name, "imageName": image, "gpuTypeIds": [gpu_type], "gpuCount": gpu_count,
            "cloudType": cloud, "containerDiskInGb": container_disk_gb, "volumeInGb": volume_gb,
            "volumeMountPath": "/workspace", "ports": ["8888/http", "22/tcp"], "env": env,
            "supportPublicIp": True,
        }
        return normalise(await self._req("POST", "/pods", body))

    async def start(self, pod_id: str) -> None:
        await self._req("POST", f"/pods/{pod_id}/start")

    async def stop(self, pod_id: str) -> None:
        await self._req("POST", f"/pods/{pod_id}/stop")

    async def gpu_prices(self) -> dict[str, dict]:
        """{gpu_id: {"COMMUNITY": price, "SECURE": price, "memory": GB, "max": {cloud: max GPUs per pod}}}."""
        q = ("query { gpuTypes { id displayName memoryInGb communityPrice securePrice "
             "maxGpuCountCommunityCloud maxGpuCountSecureCloud } }")
        r = await self.client.post(GRAPHQL, params={"api_key": self.key}, json={"query": q})
        r.raise_for_status()
        out = {}
        for g in (r.json().get("data") or {}).get("gpuTypes") or []:
            out[g["id"]] = {"COMMUNITY": g.get("communityPrice"), "SECURE": g.get("securePrice"),
                            "memory": g.get("memoryInGb"), "name": g.get("displayName"),
                            "max": {"COMMUNITY": g.get("maxGpuCountCommunityCloud"),
                                    "SECURE": g.get("maxGpuCountSecureCloud")}}
        return out

    async def stock(self, shapes: list[tuple[str, int, str]]) -> dict[tuple[str, int, str], str | None]:
        """Stock status per (gpu_type, count, cloud): "High" | "Medium" | "Low", or None for none.
        Uses GraphQL lowestPrice.stockStatus (verified against the MCP's availability on 2026-09-23)."""
        out: dict[tuple[str, int, str], str | None] = {}
        for i in range(0, len(shapes), 40):
            batch = shapes[i:i + 40]
            parts = [f'g{j}: gpuTypes(input: {{id: "{t}"}}) {{ lowestPrice(input: {{gpuCount: {c}, '
                     f'secureCloud: {"true" if cl == "SECURE" else "false"}}}) {{ stockStatus }} }}'
                     for j, (t, c, cl) in enumerate(batch)]
            r = await self.client.post(GRAPHQL, params={"api_key": self.key},
                                       json={"query": "query {\n" + "\n".join(parts) + "\n}"})
            r.raise_for_status()
            data = r.json().get("data") or {}
            for j, shape in enumerate(batch):
                rows = data.get(f"g{j}") or []
                out[shape] = ((rows[0].get("lowestPrice") or {}).get("stockStatus")) if rows else None
        return out
