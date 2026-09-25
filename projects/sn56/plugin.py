"""SN56 (Gradients environment tournament) sentinel sources and World State builder.

Public API: https://api.gradients.io (docs: https://api.gradients.io/docs, schema:
https://api.gradients.io/openapi.json). Verified live against the real API on
2026-09-25 — endpoints and field names below are NOT guessed from docs text, they
were fetched and inspected directly (see the session that wrote this file for the
raw responses). `/tournament/latest/details` returns THREE tournament tracks in one
call: "text", "image", "environment" — this plugin only cares about "environment"
(the PvP game-environment tournament: goofspiel, clobber, intercode, leduc_poker,
othello, gin_rummy, ... — the actual set varies per tournament/task and is read
from the data itself, never hardcoded here).

Round shape (per environment tournament):
  rounds[].tasks[] -> {task_id, participant_scores[{hotkey,...}], environment_names[],
                        pvp_pair_results[{hotkey_a, hotkey_b, environment_name,
                        hotkey_a_wins, hotkey_b_wins, draws, total_games}],
                        pvp_individual_scores[{hotkey, environment_name, score}]}
  boss_round_performance[] -> {task_id, boss_score, challenger_score, challenger_won, ...}
    (no hotkey on this record directly — cross-referenced against the task's own
    participant_scores by task_id to find which of our_hotkeys was the challenger)

This is the same per-environment win/loss breakdown the team spent an entire prior
session reconstructing by hand from raw PvP eval logs (see the project's recap doc)
— the API already has it; this plugin just watches it for changes.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from lab.sentinel import Change, Emit, Source, flatten

API = "https://api.gradients.io"

# The three tracks /tournament/latest/details returns; we only sentinel "environment"
# fully, but keep text/image status visible in World State for context (cheap, same call).
TRACKS = ("environment", "text", "image")


# ---------------------------------------------------------------- fetchers


async def fetch_latest(f):
    """One call covers all three tracks — cheapest possible poll."""
    d = await f.json(f"{API}/tournament/latest/details")
    out: dict = {}
    for track in TRACKS:
        t = d.get(track) or {}
        out[f"{track}.tournament_id"] = t.get("tournament_id")
        out[f"{track}.status"] = t.get("status")
        out[f"{track}.winner_hotkey"] = t.get("winner_hotkey")
        out[f"{track}.base_winner_hotkey"] = t.get("base_winner_hotkey")
        out[f"{track}.num_rounds"] = len(t.get("rounds") or [])
        out[f"{track}.num_participants"] = len(t.get("participants") or [])
    return out


def _task_hotkeys(task: dict) -> set[str]:
    return {p.get("hotkey") for p in (task.get("participant_scores") or []) if p.get("hotkey")}


def _our_environment_results(env_tournament: dict, our_hotkeys: set[str]) -> dict:
    """Per-environment_name win/loss/draw totals across every pvp_pair_result that
    involves one of our_hotkeys, plus per-task boss_round_performance entries where
    the task's participants include one of our_hotkeys (best-effort: the API does not
    label which participant was "the challenger" on that record, so if more than one
    of our hotkeys is in the task we attribute the boss-round result to the task as a
    whole rather than guessing a specific hotkey)."""
    out: dict = {}
    for round_ in env_tournament.get("rounds") or []:
        for task in round_.get("tasks") or []:
            task_hotkeys = _task_hotkeys(task)
            ours_in_task = task_hotkeys & our_hotkeys
            if not ours_in_task:
                continue
            for pair in task.get("pvp_pair_results") or []:
                if pair.get("hotkey_a") not in ours_in_task and pair.get("hotkey_b") not in ours_in_task:
                    continue
                env_name = pair.get("environment_name", "?")
                key = f"env_wins.{env_name}"
                bucket = out.setdefault(key, {"wins": 0, "losses": 0, "draws": 0, "games": 0})
                we_are_a = pair.get("hotkey_a") in ours_in_task
                w = pair.get("hotkey_a_wins", 0) if we_are_a else pair.get("hotkey_b_wins", 0)
                l = pair.get("hotkey_b_wins", 0) if we_are_a else pair.get("hotkey_a_wins", 0)
                bucket["wins"] += w
                bucket["losses"] += l
                bucket["draws"] += pair.get("draws", 0)
                bucket["games"] += pair.get("total_games", 0)
    for bp in env_tournament.get("boss_round_performance") or []:
        task = next((t for r in (env_tournament.get("rounds") or []) for t in (r.get("tasks") or [])
                     if t.get("task_id") == bp.get("task_id")), None)
        if task and _task_hotkeys(task) & our_hotkeys:
            out[f"boss_round.{bp['task_id'][:8]}"] = {
                "boss_score": bp.get("boss_score"), "challenger_score": bp.get("challenger_score"),
                "challenger_won": bp.get("challenger_won"),
            }
    return out


def our_results_fetcher(our_hotkeys: list[str]):
    ours = {h for h in our_hotkeys if h}

    async def fetch_our_results(f):
        if not ours:
            return {}
        d = await f.json(f"{API}/tournament/latest/details")
        env = d.get("environment") or {}
        out = _our_environment_results(env, ours)
        # NOTE: deliberately NOT passed through flatten() -- each env_wins.<name> /
        # boss_round.<id> value here is a small dict ({"wins":.., "losses":..}) that
        # we want the sentinel to diff and store as ONE atomic fact (Change.new is
        # then that whole dict, which classify_our_results below expects). flatten()
        # would recurse into it and produce env_wins.<name>.wins /
        # env_wins.<name>.losses as separate facts instead, breaking that contract
        # (verified against lab/sentinel.py's diff(): it hashes+compares whatever
        # value is under each top-level key, no forced flatness, so this is safe).
        out["_tournament_id"] = env.get("tournament_id")
        return out
    return fetch_our_results


async def fetch_fees(f):
    d = await f.json(f"{API}/tournament/fees")
    return {k: v for k, v in flatten(d).items()}


def balance_fetcher(coldkey: str):
    async def fetch_balance(f):
        if not coldkey:
            return {}
        d = await f.json(f"{API}/tournament/balance/{coldkey}")
        return {k: v for k, v in flatten(d).items()}
    return fetch_balance


# ---------------------------------------------------------------- classifiers


def classify_latest(changes: list[Change]) -> list[Emit]:
    out: list[Emit] = []
    for track in TRACKS:
        tid = next((c for c in changes if c.key == f"{track}.tournament_id"), None)
        if tid and tid.kind != "removed":
            out.append(Emit(f"world.change.{track}_tournament",
                            f"new {track} tournament: {tid.new} (was {tid.old})", "major",
                            payload=tid.brief()))
            continue  # a brand-new tournament id makes the status-change below redundant noise
        status = next((c for c in changes if c.key == f"{track}.status"), None)
        if status:
            sev = "major" if status.new == "completed" else "normal"
            out.append(Emit(f"world.change.{track}_tournament",
                            f"{track} tournament {status.new}: status {status.old} -> {status.new}", sev,
                            payload=status.brief()))
    return out


def classify_our_results(changes: list[Change]) -> list[Emit]:
    out: list[Emit] = []
    for c in changes:
        if c.key.startswith("env_wins."):
            env_name = c.key.split(".", 1)[1]
            new = c.new or {}
            wins, losses, draws = new.get("wins", 0), new.get("losses", 0), new.get("draws", 0)
            total = wins + losses + draws
            wr = round(100 * wins / total, 1) if total else None
            out.append(Emit("sn56.our_env_result",
                            f"our result on {env_name}: {wins}W-{losses}L-{draws}D"
                            + (f" ({wr}% win rate)" if wr is not None else ""), "major", payload=new))
        elif c.key.startswith("boss_round."):
            new = c.new or {}
            won = new.get("challenger_won")
            out.append(Emit("sn56.boss_round",
                            f"boss round {'WON' if won else 'lost'}: challenger {new.get('challenger_score')} "
                            f"vs boss {new.get('boss_score')}", "major" if won else "normal", payload=new))
    return out


def classify_fees(changes: list[Change]) -> list[Emit]:
    return [Emit("world.change.fees", "tournament fees changed: " + ", ".join(c.key for c in changes), "minor",
                 payload=[c.brief() for c in changes])]


def classify_balance(changes: list[Change]) -> list[Emit]:
    return [Emit("world.change.balance", "our tournament balance changed: " +
                 ", ".join(c.brief().get("key", c.key) for c in changes), "info",
                 payload=[c.brief() for c in changes])]


# ---------------------------------------------------------------- plugin API


def sources(project) -> list[Source]:
    cfg = project.extra.get("sn56", {})
    our_hotkeys = [h for h in cfg.get("our_hotkeys", []) if h]
    coldkey = cfg.get("our_coldkey", "")
    iv = project.sentinel.get("intervals", {})
    return [
        Source("latest", iv.get("latest", 60), fetch_latest, classify_latest),
        Source("our_results", iv.get("our_results", 60), our_results_fetcher(our_hotkeys), classify_our_results),
        Source("fees", iv.get("fees", 3600), fetch_fees, classify_fees),
        Source("balance", iv.get("balance", 900), balance_fetcher(coldkey), classify_balance),
    ]


def world_lines(w: dict) -> list[str]:
    """The facts every agent prompt shows (after the world_version line)."""
    lines = []
    for track in TRACKS:
        t = w.get(track, {})
        if t.get("tournament_id"):
            lines.append(f"{track} tournament: {t['tournament_id']} status={t.get('status')} "
                        f"rounds={t.get('num_rounds')} participants={t.get('num_participants')}")
    env_results = w.get("our_env_results", {})
    if env_results:
        lines.append("our environment results (this tournament): " + json.dumps(env_results))
    else:
        lines.append("our environment results (this tournament): (none seen yet)")
    boss = w.get("our_boss_rounds", {})
    if boss:
        lines.append("our boss-round outcomes: " + json.dumps(boss))
    return lines


def build_world(facts: dict[str, dict]) -> dict:
    latest = facts.get("latest", {})
    our = facts.get("our_results", {})
    balance = facts.get("balance", {})
    fees = facts.get("fees", {})

    tracks_out = {}
    for track in TRACKS:
        tracks_out[track] = {
            "tournament_id": latest.get(f"{track}.tournament_id"),
            "status": latest.get(f"{track}.status"),
            "winner_hotkey": latest.get(f"{track}.winner_hotkey"),
            "num_rounds": latest.get(f"{track}.num_rounds"),
            "num_participants": latest.get(f"{track}.num_participants"),
        }

    env_results = {k[len("env_wins."):]: v for k, v in our.items() if k.startswith("env_wins.")}
    boss_rounds = {k[len("boss_round."):]: v for k, v in our.items() if k.startswith("boss_round.")}

    return {
        "world_version": f"t{latest.get('environment.tournament_id', 'none')}",
        **tracks_out,
        "our_env_results": env_results,
        "our_boss_rounds": boss_rounds,
        "balance": balance,
        "fees": fees,
    }
