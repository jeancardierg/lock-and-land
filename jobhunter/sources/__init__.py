from __future__ import annotations

import logging
from typing import Callable, Iterator

from .base import Job
from . import (adzuna, arbeitnow, careerjet, findwork, himalayas, hnhiring, jobicy, jobtech, jooble,
               linkedin_alerts, remoteok, remotive, themuse, watchlist, weworkremotely, workingnomads)

log = logging.getLogger(__name__)
LAST_STATS: dict[str, str] = {}  # source -> "ok: N jobs" | "failed: reason" (filled by fetch_all)

REGISTRY: dict[str, Callable[[dict], Iterator[Job]]] = {
    "jobtech": jobtech.fetch,
    "adzuna": adzuna.fetch,
    "remotive": remotive.fetch,
    "arbeitnow": arbeitnow.fetch,
    "linkedin_alerts": linkedin_alerts.fetch,
    "himalayas": himalayas.fetch,
    "jobicy": jobicy.fetch,
    "weworkremotely": weworkremotely.fetch,
    "workingnomads": workingnomads.fetch,
    "jooble": jooble.fetch,
    "watchlist": watchlist.fetch,
    "remoteok": remoteok.fetch,
    "hnhiring": hnhiring.fetch,
    "themuse": themuse.fetch,
    "careerjet": careerjet.fetch,
    "findwork": findwork.fetch,
}


# v0.4 sources that need no key: on by default even if an older config.yaml doesn't mention them
DEFAULT_ON = {"himalayas", "jobicy", "weworkremotely", "workingnomads", "remoteok", "hnhiring", "themuse"}


def with_defaults(cfg: dict) -> dict:
    src = cfg.setdefault("sources", {})
    for name in DEFAULT_ON:
        src.setdefault(name, {"enabled": True})
    return cfg


def _run_source(name: str, fn, cfg: dict) -> list[Job]:
    """One source, fully read into a list (runs on a worker thread; the database is written by the caller)."""
    jobs = list(fn(cfg))
    log.info("source %s: %d jobs", name, len(jobs))
    return jobs


def fetch_all(cfg: dict) -> Iterator[Job]:
    """All enabled sources, a few at a time (limits.fetch_workers, default 4): one slow or retrying board no longer
    holds up the others. Jobs are yielded on the calling thread as each source finishes, so the database is only
    ever written from one thread. One broken source never stops the run."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    LAST_STATS.clear()
    with_defaults(cfg)
    todo = [(n, f) for n, f in REGISTRY.items() if (cfg.get("sources", {}).get(n) or {}).get("enabled", False)]
    if not todo:
        return
    workers = max(1, min(len(todo), int((cfg.get("limits") or {}).get("fetch_workers", 4))))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fetch") as pool:
        futs = {pool.submit(_run_source, n, f, cfg): n for n, f in todo}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                jobs = fut.result()
            except Exception as e:  # one broken source must not kill the run
                from ..secrets import redact
                log.warning("source %s failed: %s", name, redact(e))
                LAST_STATS[name] = f"failed: {redact(e)[:200]}"
                continue
            LAST_STATS[name] = f"ok: {len(jobs)} jobs"
            yield from jobs
