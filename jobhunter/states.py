"""Job state machine. Every state change goes through transition(): guarded UPDATE + job_events row,
in one transaction, so two workers (or a double click) can never move the same job twice.

`state` drives the pipeline. The v0.2 `status` column is kept in sync for the Streamlit dashboard and the
tracker (which adds interview / offer / rejected / withdrawn after a job is submitted).
"""
from __future__ import annotations

import sqlite3

STATES = ["queued", "extracted", "filtered", "relocation", "scored", "generated", "needs_review", "approved",
          "submitting", "awaiting_click", "needs_human", "submitted", "rejected", "failed"]

ALLOWED: dict[str, set[str]] = {
    "queued": {"extracted", "filtered", "relocation", "failed"},
    "extracted": {"scored", "filtered", "relocation", "failed"},
    "filtered": {"queued", "scored", "submitted"},                       # the user can pull a filtered job back
    "relocation": {"generated", "scored", "rejected", "queued", "submitted"},
    "scored": {"generated", "filtered", "rejected", "queued", "submitted"},
    "generated": {"needs_review", "failed"},
    "needs_review": {"approved", "rejected", "generated", "needs_review", "submitted", "scored"},
    "approved": {"submitting", "needs_review", "submitted", "rejected", "needs_human"},
    "submitting": {"awaiting_click", "needs_human", "failed", "submitted", "approved"},
    "awaiting_click": {"submitted", "needs_human", "failed", "approved"},
    "needs_human": {"submitted", "rejected", "approved", "failed"},
    "failed": {"queued", "scored", "rejected", "generated", "submitted"},
    "rejected": {"queued", "scored"},
    "submitted": set(),
}
# who may wait in which state for whom (used by the review page)
ACTIVE = {"queued", "extracted", "generated", "submitting"}
FROM: dict[str, list[str]] = {}
for _src, _dsts in ALLOWED.items():
    for _d in _dsts:
        FROM.setdefault(_d, []).append(_src)

LEGACY = {"queued": "new", "extracted": "new", "filtered": "filtered", "relocation": "relocation",
          "scored": "scored", "generated": "shortlisted", "needs_review": "drafted", "approved": "approved",
          "submitting": "sending", "awaiting_click": "sending", "needs_human": "approved", "submitted": "applied",
          "rejected": "skipped", "failed": "error"}
FROM_LEGACY = {"new": "queued", "filtered": "filtered", "relocation": "relocation", "scored": "scored",
               "shortlisted": "scored", "drafted": "needs_review", "approved": "approved", "sending": "approved",
               "applied": "submitted", "interview": "submitted", "offer": "submitted", "rejected": "submitted",
               "withdrawn": "submitted", "skipped": "rejected", "error": "failed"}


class IllegalTransition(RuntimeError):
    pass


def transition(conn: sqlite3.Connection, job_id: int, to: str, actor: str = "system", reason: str = "",
               legacy: str | None = None, **fields) -> str:
    """Move job_id to `to`. Returns the previous state. Raises IllegalTransition if not allowed now."""
    if to not in ALLOWED:
        raise ValueError(f"unknown state {to}")
    srcs = FROM.get(to, [])
    cols = "".join(f", {k}=?" for k in fields)
    with conn:
        row = conn.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise IllegalTransition(f"job {job_id} does not exist")
        prev = row[0]
        cur = conn.execute(
            f"UPDATE jobs SET state=?, status=?, updated_at=datetime('now'){cols} "
            f"WHERE id=? AND state IN ({','.join('?' * len(srcs))})",
            (to, legacy or LEGACY[to], *fields.values(), job_id, *srcs))
        if cur.rowcount != 1:
            raise IllegalTransition(f"job {job_id}: {prev} -> {to} not allowed")
        conn.execute("INSERT INTO job_events(job_id, from_state, to_state, actor, reason) VALUES (?,?,?,?,?)",
                     (job_id, prev, to, actor, reason[:500]))
    return prev


def try_transition(conn, job_id: int, to: str, **kw) -> bool:
    try:
        transition(conn, job_id, to, **kw)
        return True
    except IllegalTransition:
        return False
