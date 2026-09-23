"""Daily GPU budget: spend ledger, pool caps and reservations.

Spend is accrued minute by minute from each running pod's hourly price.
A lease reserves its remaining max_hours × price, so parallel requests cannot
jointly overshoot a pool. Days roll over at local midnight in the lab timezone.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .db import DB, now

ACTIVE_LEASE = ("requested", "provisioning", "granted")


def day_start(tz: str, ts: float | None = None) -> float:
    z = ZoneInfo(tz)
    d = datetime.fromtimestamp(ts or now(), z)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def next_local(tz: str, hhmm: str, after: float | None = None) -> float:
    z = ZoneInfo(tz)
    base = datetime.fromtimestamp(after or now(), z)
    h, m = (int(x) for x in hhmm.split(":"))
    t = base.replace(hour=h, minute=m, second=0, microsecond=0)
    if t.timestamp() <= base.timestamp():
        t += timedelta(days=1)
    return t.timestamp()


class Budget:
    def __init__(self, db: DB, project, tz: str):
        self.db = db
        self.p = project
        self.tz = tz

    def cap(self, pool: str | None = None) -> float | None:
        """Daily cap, or a pool's cap; None when the pool is a label without its own cap."""
        if pool is None:
            return self.p.daily_usd
        frac = self.p.pools.get(pool)
        return None if frac is None else self.p.daily_usd * frac

    def spent(self, pool: str | None = None, since: float | None = None) -> float:
        since = since if since is not None else day_start(self.tz)
        q = "SELECT COALESCE(SUM(usd),0) s FROM ledger WHERE project=? AND ts>=?"
        args: list = [self.p.name, since]
        if pool is not None:
            q += " AND pool=?"
            args.append(pool)
        return float(self.db.one(q, args)["s"])

    def committed(self, pool: str | None = None) -> float:
        """Reserved but not yet spent: remaining hours of every active lease."""
        rows = self.db.all(
            "SELECT l.*, COALESCE(l.pool, e.pool) AS lpool, COALESCE(l.est_usd, e.est_cost_usd) AS est "
            "FROM leases l LEFT JOIN experiments e ON e.id=l.experiment_id "
            "WHERE l.project=? AND l.status IN (?,?,?)", (self.p.name, *ACTIVE_LEASE))
        t = now()
        total = 0.0
        for r in rows:
            if pool is not None and r["lpool"] != pool:
                continue
            if r["price_hr"] and r["granted_at"]:
                left = max(0.0, r["max_hours"] - (t - r["granted_at"]) / 3600)
                total += r["price_hr"] * left
            else:
                total += r["est"] or 0.0
        return total

    def remaining(self, pool: str | None = None) -> float:
        cap = self.cap(pool)
        if cap is None:
            return self.remaining(None)
        return cap - self.spent(pool) - self.committed(pool)

    def check(self, pool: str, est_usd: float, *, approved: bool = False, credit: float = 0.0) -> tuple[bool, str]:
        """Can `est_usd` be reserved in `pool` today? `credit` is a reservation the
        caller already holds (e.g. its own requested lease) and gets back first.
        Approved experiments may exceed the per-experiment cap, never the pool."""
        if pool not in self.p.pools:
            return False, f"unknown pool {pool!r} (pools: {', '.join(self.p.pools)})"
        if not approved and self.p.per_experiment_usd and est_usd > self.p.per_experiment_usd:
            return False, (f"estimated ${est_usd:.0f} exceeds the per-experiment cap "
                           f"${self.p.per_experiment_usd:.0f}")
        rem_pool = self.remaining(pool) + credit
        rem_all = self.remaining(None) + credit
        if self.cap(pool) is not None and est_usd > rem_pool:
            return False, (f"pool {pool} has ${rem_pool:.0f} left today (cap ${self.cap(pool):.0f}, "
                           f"spent ${self.spent(pool):.0f}, reserved ${self.committed(pool):.0f}); needs ${est_usd:.0f}")
        if est_usd > rem_all:
            return False, f"daily budget has ${rem_all:.0f} left; needs ${est_usd:.0f}"
        return True, "ok"

    def summary(self) -> dict:
        return {
            "daily_cap": self.p.daily_usd,
            "spent_today": round(self.spent(), 2),
            "reserved": round(self.committed(), 2),
            "pools": {p: {"cap": round(self.cap(p), 2) if self.cap(p) is not None else None,
                          "spent": round(self.spent(p), 2),
                          "reserved": round(self.committed(p), 2)} for p in self.p.pools},
        }

    def record(self, usd: float, pool: str, *, experiment_id: str | None, pod_id: str | None, note: str = ""):
        if usd <= 0:
            return
        self.db.insert("ledger", project=self.p.name, ts=now(), pool=pool, experiment_id=experiment_id,
                       pod_id=pod_id, usd=usd, note=note)
        if experiment_id:
            self.db.x("UPDATE experiments SET spent_usd=COALESCE(spent_usd,0)+? WHERE id=?", (usd, experiment_id))
