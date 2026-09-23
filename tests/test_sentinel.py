import json

from lab.sentinel import Sentinel, load_plugin


def make(db, project, fetcher):
    plugin = load_plugin(project.dir / "plugin.py")
    return Sentinel(db, project, plugin, fetcher=fetcher), plugin


async def poll_all(s):
    for src in s.sources:
        await s.poll(src)
    s.rebuild_world()


def events(db, prefix=""):
    return [dict(r) for r in db.all("SELECT * FROM events ORDER BY id") if r["topic"].startswith(prefix)]


async def test_baseline_then_quiet(db, project, fetcher):
    s, _ = make(db, project, fetcher)
    await poll_all(s)
    assert events(db, "world.change") == []
    assert len(events(db, "sentinel.baseline")) == len(s.sources)
    w = json.loads((project.world_dir / "world.json").read_text())
    assert w["contract"]["subnet.weight_version_key"] == 23
    assert w["corpus"]["corpus_epoch"] == 74
    assert w["king"]["reign_number"] == 21
    assert w["world_version"].startswith("wvk23-e74-")
    assert w["teacher"] == "Qwen/Qwen3.8-27B"
    assert w["llms"]["latest_fork"].startswith("Fork history: wvk 23")
    await poll_all(s)  # nothing changed
    assert events(db, "world.change") == []


async def test_wvk_bump_is_major_and_marks_stale(db, project, fetcher):
    s, _ = make(db, project, fetcher)
    await poll_all(s)
    c = json.loads(fetcher._body("/contract"))
    c["subnet"]["weight_version_key"] = 24
    c["duel"]["max_thought_tokens"] = 8192
    fetcher.overrides["/contract"] = json.dumps(c)
    for src in s.sources:
        if src.name == "contract":
            await s.poll(src)
    s.rebuild_world()
    ev = events(db, "world.change.contract")
    assert len(ev) == 1 and ev[0]["severity"] == "major" and "23 → 24" in ev[0]["summary"]


async def test_new_fork_section_and_king_change(db, project, fetcher):
    s, _ = make(db, project, fetcher)
    await poll_all(s)
    llms = fetcher._body("llms.txt")
    fetcher.overrides["llms.txt"] = llms + "\n## Fork history: wvk 24 — something new (effective 2026-09-30)\nbody\n"
    snap = json.loads(fetcher._body("/snapshot"))
    snap["king"]["revision"] = "f" * 64
    snap["king"]["reign_number"] = 22
    fetcher.overrides["/snapshot"] = json.dumps(snap)
    for src in s.sources:
        if src.name in ("llms", "board", "code"):
            await s.poll(src)
    llms_ev = events(db, "world.change.llms")
    assert llms_ev and llms_ev[0]["severity"] == "major"
    king = events(db, "board.king")
    assert king and king[0]["severity"] == "major" and "reign 22" in king[0]["summary"]


async def test_corpus_epoch_change(db, project, fetcher):
    s, _ = make(db, project, fetcher)
    await poll_all(s)
    d = json.loads(fetcher._body("/dataset"))
    d["corpus_epoch"] = 75
    fetcher.overrides["/dataset"] = json.dumps(d)
    for src in s.sources:
        if src.name == "corpus":
            await s.poll(src)
    ev = events(db, "world.change.corpus")
    assert ev and "74 → 75" in ev[0]["summary"]


async def test_our_crown_expiry(db, project, fetcher):
    snap = json.loads(fetcher._body("/snapshot"))
    member = snap["reign"]["members"][0]
    project.extra.setdefault("affine", {})["our_hotkeys"] = [member["hotkey"]]
    import time
    member["paid_until"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 48 * 3600))
    fetcher.overrides["/snapshot"] = json.dumps(snap)
    s, _ = make(db, project, fetcher)
    await poll_all(s)
    member["paid_until"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5 * 3600))
    fetcher.overrides["/snapshot"] = json.dumps(snap)
    for src in s.sources:
        if src.name == "board":
            await s.poll(src)
    assert events(db, "board.crown_expiring")


async def test_source_failure_backoff(db, project, fetcher):
    s, _ = make(db, project, fetcher)
    fetcher.overrides["/contract"] = "not json"
    await s.tick()
    assert s.failures.get("contract") == 1
    assert s.next_due["contract"] > s.next_due.get("llms", 0) - 10_000


async def test_mirror_flapping_is_not_a_change(db, project, fetcher):
    s, _ = make(db, project, fetcher)
    await poll_all(s)
    primary = json.loads(fetcher._body("/contract"))
    mirror = json.loads(json.dumps(primary))
    mirror["serving"] = {"engine": "vllm", "dtype": "bfloat16"}      # key set only the mirror has
    mirror["dashboard"].pop("public_base_url", None)                 # key only the primary has
    fetcher.overrides["data/contract.json"] = json.dumps(mirror)
    contract = next(x for x in s.sources if x.name == "contract")
    fetcher.mirror = True
    await s.poll(contract)                      # primary down → mirror
    fetcher.mirror = False
    await s.poll(contract)                      # primary back
    assert events(db, "world.change.contract") == []
    # a real value change on the mirror still counts
    mirror["subnet"]["weight_version_key"] = 24
    fetcher.overrides["data/contract.json"] = json.dumps(mirror)
    fetcher.mirror = True
    await s.poll(contract)
    ev = events(db, "world.change.contract")
    assert len(ev) == 1 and "23 → 24" in ev[0]["summary"]
