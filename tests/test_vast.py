import json

import httpx
import pytest

from lab.vast import CLOUD, ONSTART, Vast, VastError, normalise

H100 = "NVIDIA H100 80GB HBM3"
PUB = "ssh-ed25519 AAAAkey lab@pic"


def offer(oid, gpu, n, dph, machine, ram=81559):
    return {"id": oid, "gpu_name": gpu, "num_gpus": n, "dph_total": dph, "machine_id": machine, "gpu_ram": ram}


OFFERS = [
    offer(1, "H100 SXM", 1, 3.40, 101),
    offer(2, "H100 SXM", 1, 2.90, 102),
    offer(3, "H100 SXM", 2, 5.60, 103),
    offer(4, "A100 SXM4", 1, 0.90, 104, ram=40960),     # a 40 GB A100 is not an A100-SXM4-80GB
    offer(5, "A100 SXM4", 1, 1.10, 105),
    offer(6, "RTX 4090", 1, 0.40, 106),
]


class API:
    """Fake Vast REST API."""

    def __init__(self):
        self.searches: list[dict] = []
        self.creates: list[tuple[int, dict]] = []
        self.attached: list[tuple[str, str]] = []
        self.taken: set[int] = set()
        self.instances: dict[str, dict] = {}
        self.keys = [{"id": 7, "key": "ssh-ed25519 AAAAother x@y", "deleted_at": None},
                     {"id": 8, "key": PUB, "deleted_at": "2026-09-01"}]     # deleted: must be re-added
        self.puts: list[tuple[str, dict]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer k"
        path, m = req.url.path, req.method
        body = json.loads(req.content) if req.content else None
        if path == "/api/v0/bundles/":
            self.searches.append(body)
            return httpx.Response(200, json={"offers": [o for o in OFFERS if o["gpu_name"] in body["gpu_name"]["in"]]})
        if path == "/api/v0/ssh/":
            if m == "POST":
                self.keys.append({"id": len(self.keys) + 7, "key": body["ssh_key"], "deleted_at": None})
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json=self.keys)
        if path.startswith("/api/v0/asks/"):
            oid = int(path.split("/")[4])
            self.creates.append((oid, body))
            if oid in self.taken:
                return httpx.Response(400, json={"success": False, "error": "no_such_ask", "msg": "Instance type no longer available"})
            iid = str(1000 + len(self.creates))
            o = next(o for o in OFFERS if o["id"] == oid)
            self.instances[iid] = {"id": int(iid), "label": body["label"], "actual_status": "loading",
                                   "intended_status": "running", "gpu_name": o["gpu_name"], "num_gpus": o["num_gpus"],
                                   "dph_total": o["dph_total"], "machine_id": o["machine_id"], "status_msg": "pulling"}
            return httpx.Response(200, json={"success": True, "new_contract": int(iid)})
        if path.endswith("/ssh/") and path.startswith("/api/v0/instances/"):
            if (path.split("/")[4], body["ssh_key"]) in self.attached:     # seen live on 2026-09-24
                return httpx.Response(200, json={"success": False, "msg": "SSH key already associated with instance."})
            self.attached.append((path.split("/")[4], body["ssh_key"]))
            return httpx.Response(200, json={"success": True})
        if path == "/api/v1/instances/":
            return httpx.Response(200, json={"instances": list(self.instances.values())})
        if path.startswith("/api/v0/instances/"):
            iid = path.split("/")[4]
            if m == "GET":
                return httpx.Response(200, json={"instances": self.instances.get(iid)})
            if m == "PUT":
                self.puts.append((iid, body))
                return httpx.Response(200, json={"success": True})
            if m == "DELETE":
                self.instances.pop(iid, None)
                return httpx.Response(200, json={"success": True})
        return httpx.Response(404)


@pytest.fixture
def api():
    return API()


@pytest.fixture
def vast(api):
    return Vast("k", client=httpx.AsyncClient(transport=httpx.MockTransport(api)))


async def test_prices_and_stock_in_runpod_terms(vast, api):
    prices = await vast.gpu_prices()
    assert prices[H100] == {CLOUD: 2.8, "max": {CLOUD: 2}}           # $5.60 for 2 is $2.80/GPU
    assert prices["NVIDIA A100-SXM4-80GB"][CLOUD] == 1.1              # the 40 GB card is not counted
    assert "NVIDIA H200" not in prices
    st = await vast.stock([(H100, 1, CLOUD), (H100, 2, CLOUD), (H100, 8, CLOUD)])
    assert st == {(H100, 1, CLOUD): "Medium", (H100, 2, CLOUD): "Low", (H100, 8, CLOUD): None}
    q = api.searches[0]
    assert q["verified"] == {"eq": True} and q["reliability2"]["gte"] == 0.97 and q["type"] == "on-demand"
    assert len(api.searches) == 1                                       # cached between calls


async def test_create_registers_and_attaches_the_lab_key(vast, api):
    """Vast copies account keys into an instance only at create: the lab key must be there first (Shadeform
    locked the lab out by using someone else's default key), and it is attached and appended as well."""
    pod = await vast.create_pod(name="Pi_affine-04", gpu_type=H100, gpu_count=1, cloud=CLOUD, image="img:1",
                                container_disk_gb=80, volume_gb=200, env={"PUBLIC_KEY": PUB})
    assert [k["key"] for k in api.keys if not k["deleted_at"]][-1] == PUB
    oid, body = api.creates[0]
    assert oid == 2 and body["disk"] == 280 and body["label"] == "Pi_affine-04" and body["image"] == "img:1"
    assert body["runtype"] == "ssh_direc ssh_proxy" and body["cancel_unavail"] is True
    assert api.attached == [(pod.id, PUB)]
    await vast.attach_key(pod.id, PUB)                                  # already there: not an error
    on = body["onstart"]
    assert PUB in on and on.index("authorized_keys") < on.index("apt-get") and on.rstrip().endswith("lab-bootstrap.done")
    assert len(on) < 4000                                               # Vast's onstart limit is 4048 chars
    assert (pod.name, pod.status, pod.cloud, pod.machine_id, pod.price_hr) == ("Pi_affine-04", "CREATED", CLOUD, "102", 2.9)
    await vast.create_pod(name="p", gpu_type=H100, gpu_count=1, cloud=CLOUD, image="x",
                          container_disk_gb=80, volume_gb=200, env={"PUBLIC_KEY": PUB})
    assert len([k for k in api.keys if k["key"] == PUB]) == 2           # registered once, not per create


async def test_taken_offer_falls_through_and_blacklist_is_skipped(vast, api):
    api.taken = {2}
    pod = await vast.create_pod(name="p", gpu_type=H100, gpu_count=1, cloud=CLOUD, image="x",
                                container_disk_gb=80, volume_gb=200, env={"PUBLIC_KEY": PUB})
    assert [c[0] for c in api.creates] == [2, 1] and pod.machine_id == "101"
    api.taken = {1, 2}
    vast._offers_at = 0
    with pytest.raises(VastError) as e:
        await vast.create_pod(name="p", gpu_type=H100, gpu_count=1, cloud=CLOUD, image="x",
                              container_disk_gb=80, volume_gb=200, env={"PUBLIC_KEY": PUB})
    assert e.value.capacity
    with pytest.raises(VastError) as e:                                  # both hosts blacklisted: nothing to rent
        await vast.create_pod(name="p", gpu_type=H100, gpu_count=1, cloud=CLOUD, image="x",
                              container_disk_gb=80, volume_gb=200, env={"PUBLIC_KEY": PUB}, avoid={"101", "102"})
    assert e.value.capacity and "no Vast stock" in str(e.value)


async def test_instances_normalise_stop_start_delete(vast, api):
    pod = await vast.create_pod(name="p", gpu_type=H100, gpu_count=1, cloud=CLOUD, image="x",
                                container_disk_gb=80, volume_gb=200, env={"PUBLIC_KEY": PUB})
    api.instances[pod.id] |= {"actual_status": "running", "public_ipaddr": "1.2.3.4 ",
                              "ports": {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41022"}]},
                              "ssh_host": "ssh5.vast.ai", "ssh_port": 12345}
    got = await vast.get_pod(pod.id)
    assert (got.status, got.ssh_host, got.ssh_port) == ("RUNNING", "1.2.3.4", 41022)    # direct beats proxy
    assert [p.id for p in await vast.list_pods()] == [pod.id]
    await vast.stop(pod.id)
    await vast.start(pod.id)
    assert api.puts == [(pod.id, {"state": "stopped"}), (pod.id, {"state": "running"})]
    await vast.delete(pod.id)
    assert await vast.get_pod(pod.id) is None


def test_status_and_proxy_ssh():
    base = {"id": 1, "machine_id": 9}
    assert normalise(base | {"actual_status": "loading", "intended_status": "running"}).status == "CREATED"
    assert normalise(base | {"actual_status": "exited", "intended_status": "stopped"}).status == "EXITED"
    assert normalise(base | {"actual_status": "exited", "intended_status": "running"}).status == "ERROR"
    assert normalise(base | {"actual_status": "offline"}).status == "ERROR"
    p = normalise(base | {"actual_status": "running", "ssh_host": "ssh5.vast.ai", "ssh_port": 12345})
    assert (p.ssh_host, p.ssh_port, p.machine_id) == ("ssh5.vast.ai", 12345, "9")


def test_onstart_quotes_the_key():
    assert "echo 'ssh-ed25519 AAAAkey lab@pic' >>" in ONSTART.replace("{key}", "'" + PUB + "'")
