import base64
import json

import httpx
import pytest

from lab.shadeform import CLOUD, Shadeform, ShadeformError

H100 = "NVIDIA H100 80GB HBM3"
PUB = "ssh-ed25519 AAAAkey lab@pic"


def itype(cloud, sit, gpu, inter, n, cents, regions, os_=("ubuntu22.04_cuda12.4_shade_os",)):
    return {"cloud": cloud, "shade_instance_type": sit, "hourly_price": cents, "deployment_type": "vm",
            "configuration": {"gpu_type": gpu, "interconnect": inter, "num_gpus": n, "os_options": list(os_)},
            "availability": [{"region": r, "available": ok, "rental_type": "on_demand"} for r, ok in regions]}


TYPES = [
    itype("lambdalabs", "H100_sxm5", "H100", "sxm5", 1, 436, [("us-1", True)]),
    itype("voltagepark", "H100_sxm5", "H100", "sxm5", 1, 199, [("us-2", False)]),
    itype("verda", "H100_sxm5", "H100", "sxm5", 1, 331, [("fi-1", True), ("fi-2", True)]),
    itype("hyperstack", "H100", "H100", "pcie", 1, 250, [("ca-1", True)]),
    itype("voltagepark", "H100_sxm5x8", "H100", "sxm5", 8, 2800, [("us-2", True)]),
    itype("slowcloud", "H100_sxm5", "H100", "sxm5", 1, 100, [("us-9", True)]),   # boots in hours: skipped
]
TYPES[-1]["boot_time"] = {"min_boot_in_sec": 14400, "max_boot_in_sec": 20820}


class API:
    """Fake Shadeform REST API."""

    def __init__(self):
        self.creates: list[dict] = []
        self.fail_clouds: dict[str, tuple[int, str]] = {}
        self.instances: dict[str, dict] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["X-API-KEY"] == "k"
        path = req.url.path.removeprefix("/v1")
        body = json.loads(req.content) if req.content else None
        if path == "/instances/types":
            return httpx.Response(200, json={"instance_types": TYPES})
        if path == "/instances/create":
            self.creates.append(body)
            if body["cloud"] in self.fail_clouds:
                code, msg = self.fail_clouds[body["cloud"]]
                return httpx.Response(code, json={"message": msg})
            iid = f"i{len(self.creates)}"
            t = next(t for t in TYPES if t["cloud"] == body["cloud"] and t["shade_instance_type"] == body["shade_instance_type"])
            self.instances[iid] = {"id": iid, "name": body["name"], "status": "pending", "ip": None, "ssh_port": 22,
                                   "cloud_assigned_id": "c-" + iid, "configuration": t["configuration"],
                                   "hourly_price": t["hourly_price"]}
            return httpx.Response(200, json={"id": iid})
        if path == "/instances":
            return httpx.Response(200, json={"instances": list(self.instances.values())})
        if path.endswith("/info"):
            i = self.instances.get(path.split("/")[2])
            return httpx.Response(200, json=i) if i else httpx.Response(404, json={"message": "not found"})
        if path.endswith("/delete"):
            self.instances[path.split("/")[2]]["status"] = "deleting"
            return httpx.Response(200)
        return httpx.Response(404)


@pytest.fixture
def api():
    return API()


@pytest.fixture
def sf(api):
    return Shadeform("k", client=httpx.AsyncClient(transport=httpx.MockTransport(api)))


async def test_prices_and_stock_in_runpod_terms(sf):
    prices = await sf.gpu_prices()
    assert prices[H100] == {CLOUD: 3.31, "max": {CLOUD: 8}}         # cheapest in-stock SXM, not the sold-out $1.99
    assert prices["NVIDIA H100 PCIe"][CLOUD] == 2.5
    assert "NVIDIA H200" not in prices
    st = await sf.stock([(H100, 1, CLOUD), (H100, 8, CLOUD), (H100, 2, CLOUD)])
    assert st == {(H100, 1, CLOUD): "High", (H100, 8, CLOUD): "Low", (H100, 2, CLOUD): None}


async def test_create_rents_cheapest_offer_with_bootstrap(sf, api):
    pod = await sf.create_pod(name="Pi_affine-03", gpu_type=H100, gpu_count=1, cloud=CLOUD, image="x",
                              container_disk_gb=80, volume_gb=200, env={"PUBLIC_KEY": PUB})
    body = api.creates[0]
    assert (body["cloud"], body["region"]) == ("verda", "fi-1") and body["shade_cloud"] is True
    assert body["name"] == "pi-affine-03" and "Pi_affine-03" in body["tags"]
    script = base64.b64decode(body["launch_configuration"]["script_configuration"]["base64_script"]).decode()
    assert PUB in script and "/workspace" in script and "PermitRootLogin" in script
    assert body["os"].startswith("ubuntu") and "cuda" in body["os"]
    assert pod.name == "Pi_affine-03" and pod.status == "CREATED" and pod.cloud == CLOUD and pod.price_hr == 3.31


async def test_create_falls_through_offers_then_reports_capacity(sf, api):
    api.fail_clouds = {"verda": (400, "Instance type is not available in this region")}
    pod = await sf.create_pod(name="p", gpu_type=H100, gpu_count=1, cloud=CLOUD, image="x",
                              container_disk_gb=80, volume_gb=200, env={"PUBLIC_KEY": PUB})
    assert [c["cloud"] for c in api.creates] == ["verda", "verda", "lambdalabs"] and pod.price_hr == 4.36
    api.fail_clouds["lambdalabs"] = (500, "sold out")
    with pytest.raises(ShadeformError) as e:
        await sf.create_pod(name="p", gpu_type=H100, gpu_count=1, cloud=CLOUD, image="x",
                            container_disk_gb=80, volume_gb=200, env={"PUBLIC_KEY": PUB})
    assert e.value.capacity
    api.fail_clouds = {"verda": (401, "Invalid API key")}
    with pytest.raises(ShadeformError) as e:
        await sf.create_pod(name="p", gpu_type=H100, gpu_count=1, cloud=CLOUD, image="x",
                            container_disk_gb=80, volume_gb=200, env={"PUBLIC_KEY": PUB})
    assert e.value.status == 401 and not e.value.capacity
    with pytest.raises(ShadeformError) as e:
        await sf.create_pod(name="p", gpu_type=H100, gpu_count=2, cloud=CLOUD, image="x",
                            container_disk_gb=80, volume_gb=200, env={"PUBLIC_KEY": PUB})
    assert e.value.capacity


async def test_instances_normalise_and_delete(sf, api):
    pod = await sf.create_pod(name="p", gpu_type="NVIDIA H100 PCIe", gpu_count=1, cloud=CLOUD, image="x",
                              container_disk_gb=80, volume_gb=200, env={"PUBLIC_KEY": PUB})
    api.instances[pod.id].update(status="active", ip="1.2.3.4", ssh_port=2222)
    got = await sf.get_pod(pod.id)
    assert (got.status, got.ssh_host, got.ssh_port, got.price_hr) == ("RUNNING", "1.2.3.4", 2222, 2.5)
    await sf.delete(pod.id)
    assert [p.status for p in await sf.list_pods()] == ["TERMINATED"]
    assert await sf.get_pod("nope") is None


def test_gpu_map_override():
    sf = Shadeform("k", gpu_map={"NVIDIA H200 NVL": ["H200", None]})
    assert sf.gpu_map["NVIDIA H200 NVL"] == ("H200", None) and H100 in sf.gpu_map
