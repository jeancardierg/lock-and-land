"""CLI: python -m jobhunter <command>"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time

from .config import DATA, HOME, ROOT, load_config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jobhunter")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="one synchronous cycle: fetch, prepare, score, draft (never sends)")
    sub.add_parser("fetch")
    sub.add_parser("recruiters")
    sub.add_parser("doctor", help="check config, profile, models, mail")
    ga = sub.add_parser("gmail-auth", help="interactive Gmail consent (gmail_api backend only)")
    ga.add_argument("--force", action="store_true", help="ignore the saved token and sign in again")
    sub.add_parser("dashboard", help="Streamlit dashboard (tracker, settings) on :8501")
    sub.add_parser("review", help="fast review page on :8502")
    w = sub.add_parser("workers", help="run the main and gpu queues in this process")
    w.add_argument("--only", nargs="*", choices=["main", "gpu"])
    sub.add_parser("migrate", help="back up and migrate the database")
    sub.add_parser("connections", help="show sign-in, keychain and encryption status (no secret values)")
    im = sub.add_parser("import", help="import an older install (settings, data, secrets -> keychain, DB encrypted)")
    im.add_argument("path")
    im.add_argument("--wipe", action="store_true", help="also wipe the old .env / token.json / credentials.json")
    sub.add_parser("encrypt", help="encrypt every plain database in data/")
    wf = sub.add_parser("watchlist-find", help="find a company's Greenhouse / Lever / Ashby job board")
    wf.add_argument("names", nargs="+")
    sh = sub.add_parser("set-home", help="point the desktop app at another data folder (restart the app after)")
    sh.add_argument("path")
    sub.add_parser("profile-check", help="check facts/master_profile.json")
    sub.add_parser("recheck", help="re-run the fact checker on every letter waiting for review (after checker updates)")
    r = sub.add_parser("redraft", help="regenerate letters for jobs in a state (default: scored with score >= draft)")
    r.add_argument("--ids", nargs="*", type=int)
    r.add_argument("--limit", type=int, default=50)
    e = sub.add_parser("eval", help="run the real models on N jobs and report validator results")
    e.add_argument("--n", type=int, default=10)
    e.add_argument("--state", default="scored")
    args = ap.parse_args(argv)

    from .config import FROZEN
    if FROZEN:
        from .connections import ensure_home
        ensure_home()
    DATA.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        # pythonw (hidden start) has no console: uvicorn and print() would crash or vanish, so give them a file
        con = open(DATA / "console.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or con
        sys.stderr = sys.stderr or con
    from .secrets import RedactFilter, log_handler
    handlers: list[logging.Handler] = [log_handler(DATA / "jobhunter.log")]
    if sys.stderr is not None:  # None under pythonw (scheduled task)
        handlers.append(logging.StreamHandler())
        handlers[-1].addFilter(RedactFilter())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(args.config)

    if args.cmd == "run":
        from .pipeline import run
        print(json.dumps(run(cfg), indent=2))
    elif args.cmd == "fetch":
        from .db import DB
        from .pipeline import ingest
        print(json.dumps(ingest(DB(), cfg), indent=2))
    elif args.cmd == "recruiters":
        from . import recruiters
        from .db import DB
        db = DB()
        print({"new": recruiters.pull(db, cfg), "drafted": recruiters.classify_and_draft(db, cfg, None)})
    elif args.cmd == "doctor":
        return doctor(cfg)
    elif args.cmd == "gmail-auth":
        from . import gmail
        try:
            prof = gmail.service(interactive=True, force=args.force).users().getProfile(userId="me").execute()
        except Exception as e:  # visible to the dashboard, which starts this without a console
            logging.exception("gmail-auth failed")
            (DATA / "gmail_auth_error.txt").write_text(f"{type(e).__name__}: {e}", encoding="utf-8")
            print(f"Gmail sign-in failed: {e}")
            return 1
        (DATA / "gmail_auth_error.txt").unlink(missing_ok=True)
        print(f"Gmail OK: {prof['emailAddress']}")
    elif args.cmd == "dashboard":
        return subprocess.call([sys.executable, "-m", "streamlit", "run", str(ROOT / "jobhunter" / "dashboard.py"),
                                "--server.headless", "true", "--server.address", "127.0.0.1",
                                "--browser.gatherUsageStats", "false", "--client.toolbarMode", "minimal"])
    elif args.cmd == "review":
        import uvicorn
        r = cfg.get("review") or {}
        uvicorn.run("jobhunter.review.app:app", host=r.get("host", "127.0.0.1"), port=r.get("port", 8502),
                    log_level="warning")
    elif args.cmd == "workers":
        return workers(args.only)
    elif args.cmd == "migrate":
        from .config import DATA as D
        from .migrate import migrate
        print(json.dumps(migrate(D / "jobhunter.db"), indent=2))
    elif args.cmd == "connections":
        from . import connections
        print(json.dumps(connections.status(refresh=True), indent=2, default=str))
    elif args.cmd == "import":
        from . import connections
        print(json.dumps(connections.import_install(args.path, wipe_secrets=args.wipe), indent=2))
    elif args.cmd == "watchlist-find":
        from .sources import watchlist
        for n in args.names:
            hits = watchlist.find(n)
            print(json.dumps(hits) if hits else f"{n}: no Greenhouse/Lever/Ashby board found")
    elif args.cmd == "set-home":
        from . import connections
        print(f"Data folder set to {connections.set_home(args.path)}. Restart jobhunter.")
    elif args.cmd == "encrypt":
        from . import connections
        print(json.dumps(connections.encrypt_all(), indent=2))
    elif args.cmd == "profile-check":
        from . import master
        p = master.load()
        probs = master.problems(p)
        print(f"{len(p.facts)} facts, {len(p.qa_bank)} stored answers, version {p.version}, sha {p.sha}")
        for x in probs:
            print(f"[!!] {x}")
        return 1 if any("APPROVED" not in x for x in probs) else 0
    elif args.cmd == "recheck":
        return recheck(cfg)
    elif args.cmd == "redraft":
        return redraft(cfg, args.ids, args.limit)
    elif args.cmd == "eval":
        return evaluate(cfg, args.n, args.state)
    return 0


def start_workers(only: list[str] | None = None) -> list:
    """Start the main and gpu queues as threads in this process. Returns the consumers (stop them with
    stop_workers). Used by `workers` and by the desktop app."""
    from huey.consumer import Consumer
    from . import tasks
    consumers = []
    for n in only or ["main", "gpu"]:
        # flush_locks: a worker killed mid-task (reboot, Ctrl+C) must not leave the "gpu" lock held forever
        # the GPU queue reacts fast; main mostly runs periodic tasks, so they poll less often when idle
        c = Consumer(tasks.QUEUES[n], workers=1, worker_type="thread", periodic=True, check_worker_health=True,
                     flush_locks=True, initial_delay=1, max_delay={"gpu": 5, "main": 10}.get(n, 20))
        c.start()
        consumers.append(c)
        logging.info("worker %s started", n)
    return consumers


def stop_workers(consumers: list) -> None:
    for c in consumers:
        try:
            c.stop(graceful=True)
        except Exception as e:  # never block quitting
            logging.warning("stopping worker: %s", e)


def beat() -> None:
    (DATA / "workers.heartbeat").write_text(str(time.time()))


def workers(only: list[str] | None) -> int:
    consumers = start_workers(only)
    try:
        while True:
            beat()
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_workers(consumers)
    return 0


def recheck(cfg: dict) -> int:
    """Letters already waiting for review get the current checker's verdict; failures go back for a rewrite."""
    from . import grounding, master
    from .db import DB
    db, prof = DB(), master.get()
    out = {"ok": 0, "sent_back": 0}
    for j in db.in_state("needs_review"):
        app = db.application(j["id"])
        if not app or not app["letter_text"]:
            continue
        letter_json = json.loads(app["letter_json"] or "null")
        v = grounding.revalidate_text(cfg, prof, letter_json, app["letter_text"], app["lang"] or "en")
        db.update_application(app["id"], validation_json=v, validation_score=v["score"])
        if v["hard"] and not app["edited_by_jean"]:
            db.move(j["id"], "scored", reason="recheck: " + "; ".join(v["hard"][:2])[:300])
            out["sent_back"] += 1
            print(f"job {j['id']} {j['title'][:50]}: sent back for a rewrite ({v['hard'][0]})")
        else:
            out["ok"] += 1
    from . import pipeline
    out.update(pipeline.refit(db, cfg))
    out["signatures_fixed"] = fix_signatures(db, prof, cfg)
    print(json.dumps(out))
    return 0


def fix_signatures(db, prof, cfg: dict) -> int:
    """Older letters were signed with the config name; the approved master profile name is the one to use."""
    name = prof.identity.get("name") or ""
    if not name:
        return 0
    n = 0
    old_names = {(cfg.get("profile") or {}).get("name") or ""} - {name, ""}
    rows = db.conn.execute("SELECT a.id, a.job_id, a.letter_text FROM applications a JOIN jobs j ON j.id=a.job_id "
                           "WHERE j.state IN ('needs_review','approved','needs_human','awaiting_click')").fetchall()
    for r in rows:
        t = r["letter_text"] or ""
        nt = t
        for o in old_names:
            nt = nt.replace(o, name)
        if nt != t:
            db.update_application(r["id"], letter_text=nt)
            job = db.job(r["job_id"])
            if job["cover_letter"]:
                cl = job["cover_letter"]
                for o in old_names:
                    cl = cl.replace(o, name)
                db.update_job(r["job_id"], cover_letter=cl)
            n += 1
    return n


def redraft(cfg: dict, ids: list[int] | None, limit: int) -> int:
    from . import pipeline
    from .db import DB
    db = DB()
    th = cfg.get("thresholds", {}).get("draft_score", 70)
    todo = ids or [j["id"] for j in db.in_state("scored", "failed") if (j["score"] or 0) >= th][:limit]
    res: dict[str, int] = {}
    for jid in todo:
        t0 = time.time()
        st = pipeline.draft_job(db, cfg, None, jid) and "needs_review" or db.job(jid)["state"]
        res[st] = res.get(st, 0) + 1
        app = db.application(jid)
        v = json.loads(app["validation_json"] or "{}") if app else {}
        print(f"job {jid}: {st} in {time.time() - t0:.0f}s, score {v.get('score')}, "
              f"attempts {app['attempt'] if app else '-'}, hard {len(v.get('hard', []))}, soft {len(v.get('soft', []))}")
    print(json.dumps(res))
    return 0


def evaluate(cfg: dict, n: int, state: str) -> int:
    """Real-model smoke test: generate for N jobs WITHOUT changing their state, print validator stats."""
    from . import grounding, master
    from .db import DB
    from .vectors import Store
    db, prof, store = DB(), master.get(), Store(cfg)
    rows = db.in_state(state)[:n]
    stats = {"jobs": 0, "ok": 0, "attempts": 0, "soft": 0, "secs": 0.0}
    for j in rows:
        t0 = time.time()
        out = grounding.screen_and_write(cfg, dict(j), prof, None, store)
        dt = time.time() - t0
        v = out["validation"]
        stats["jobs"] += 1
        stats["ok"] += out["ok"]
        stats["attempts"] += out["attempts"]
        stats["soft"] += len(v["soft"])
        stats["secs"] += dt
        print(f"--- job {j['id']} {j['title'][:60]} | ok={out['ok']} attempts={out['attempts']} "
              f"score={v['score']} {dt:.0f}s gaps={out['retrieval']['gaps'][:3]}")
        for h in v["hard"][:5]:
            print(f"   HARD {h}")
        for s in v["soft"][:5]:
            print(f"   soft {s}")
        if out["ok"]:
            print("   " + out["text"].replace("\n", "\n   "))
    if stats["jobs"]:
        print(json.dumps({**stats, "avg_secs": round(stats["secs"] / stats["jobs"], 1),
                          "avg_attempts": round(stats["attempts"] / stats["jobs"], 2)}, indent=2))
    return 0


def doctor(cfg: dict) -> int:
    from . import llm, master
    from .config import attachment_problem, cv_attachment, env, resolve
    ok = True

    def check(name: str, cond: bool, hint: str = "", required: bool = True) -> None:
        nonlocal ok
        tag = "OK" if cond else ("!!" if required else "--")
        print(f"[{tag}] {name}" + ("" if cond else f"  -> {hint}"))
        if required:
            ok &= cond

    check("config.yaml", (HOME / "config.yaml").exists(), "copy config.example.yaml to config.yaml")
    check("profile.email set", "@" in (cfg["profile"].get("email") or "") and "you@" not in cfg["profile"]["email"],
          "set profile.email in config.yaml")
    try:
        p = master.load()
        probs = master.problems(p)
        check(f"master profile ({len(p.facts)} facts)", not [x for x in probs if "APPROVED" not in x], "; ".join(probs))
        check("master profile approved by you", p.approved,
              "read facts/master_profile.json, fix anything wrong, then set \"status\": \"APPROVED\"", required=False)
    except Exception as e:
        check("master profile", False, str(e))
    for v, vc in (cfg["profile"].get("cv_variants") or {}).items():
        check(f"CV text ({v})", resolve(vc["markdown"]).exists(), f"create {vc['markdown']}", required=(v == "main"))
        problem = attachment_problem(cv_attachment(cfg, v))
        check(f"CV PDF ready ({v})", problem is None, f"{problem}", required=(v == "main"))
    missing = llm.missing_models(cfg)
    check("Ollama models", not missing, "ollama pull " + " && ollama pull ".join(missing))
    try:
        from . import ai_advisor, hardware
        plan = ai_advisor.recommend(hardware.detect(), hardware.ollama_info(llm.host(cfg)),
                                    {t: llm.configured_model(cfg, t) for t in ("writer", "extractor", "checker", "embed")})
        check(f"models suit this computer ({plan['level']})", plan["applied"] and plan["level"] != "too_weak",
              "suggested: " + ", ".join(f"{t['task']}={t['model']}" for t in plan["tasks"])
              + " (Configuration -> Local AI -> Use these settings)", required=False)
    except Exception as e:
        check("hardware check", False, str(e), required=False)
    try:
        from .db import DB
        st = dict(DB().conn.execute("SELECT state, count(*) FROM jobs GROUP BY state").fetchall())
        check(f"database migrated ({sum(st.values())} jobs)", True)
    except Exception as e:
        check("database", False, str(e))
    if cfg["sources"].get("adzuna", {}).get("enabled"):
        check("Adzuna keys", bool(env("ADZUNA_APP_ID") and env("ADZUNA_APP_KEY")), "add to .env or disable adzuna")
    backend = (cfg.get("mail") or {}).get("backend", "gmail_api")
    if backend == "imap":
        check("Gmail app password (IMAP/SMTP, no weekly expiry)", bool(env("GMAIL_APP_PASSWORD")),
              "Google Account -> Security -> App passwords; put it in .env as GMAIL_APP_PASSWORD")
    else:
        from . import secrets
        check("Gmail OAuth client", secrets.has("GMAIL_CLIENT"), "Configuration page: load the OAuth client JSON")
        check("Gmail token", secrets.has("GMAIL_TOKEN"), "Configuration page: Sign in (or: jobhunter gmail-auth)")
    check("Folder not synced by OneDrive", "onedrive" not in str(HOME).lower(),
          "move jobhunter out of OneDrive: data\\ holds your mail credentials")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
