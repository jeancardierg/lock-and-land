"""Pick Ollama models, quantisation and context size for this computer, per task.

How it decides (all sizes are what Ollama really needs, in GB):
  need = model weights (file size at that quantisation) + KV cache (grows with the context) + ~0.4 compute buffers
  budget = where the model can live:
    NVIDIA / supported AMD   VRAM minus ~1 GB (driver, desktop, other apps)
    Apple Silicon            the GPU share of unified memory macOS allows (about 2/3 of RAM, 3/4 from 64 GB),
                             capped at RAM minus 4 GB so macOS, the app and Chrome keep room
    no usable GPU            RAM minus 6 GB, on the CPU (works, but slow)
A model that fits the budget runs fully on the GPU. One that only fits GPU + RAM is split ("partly on CPU", 2-5x
slower). Bigger models beat higher-precision small ones, so the size is chosen first (at Q4_K_M, Ollama's default),
then the quantisation is raised to Q8/Q6 only when there is room to spare.

Tasks (llm.models in config.yaml):
  writer     cover letters and written answers, EN + ES. Quality matters most: biggest model that fits.
  extractor  job-ad facts and the 0-100 fit score. Runs on every job, so it must be accurate *and* quick.
  checker    the second-opinion fact check. A different model from the writer on purpose, so it doesn't share
             the writer's blind spots. Short prompts, so a smaller context.
  embed      bge-m3: multilingual retrieval, small, fine on the CPU.
All recommended chat models are Qwen 2.5 instruct: the prompts and JSON schemas were tested with it, and its Spanish
is strong down to 7B. 3B is the floor: its scores are usable, its letters need a careful read.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

# weights in GB per (size, quant); KV cache GB per 1k tokens at f16; Ollama tag per quant
FAMILY = {
    "3b": {"kv": 0.035, "q": {"q4": (1.9, "qwen2.5:3b"), "q8": (3.3, "qwen2.5:3b-instruct-q8_0")}},
    "7b": {"kv": 0.055, "q": {"q4": (4.7, "qwen2.5:7b-instruct"), "q6": (6.3, "qwen2.5:7b-instruct-q6_K"),
                              "q8": (8.1, "qwen2.5:7b-instruct-q8_0")}},
    "14b": {"kv": 0.19, "q": {"q4": (9.0, "qwen2.5:14b"), "q6": (12.1, "qwen2.5:14b-instruct-q6_K"),
                              "q8": (15.7, "qwen2.5:14b-instruct-q8_0")}},
    "32b": {"kv": 0.25, "q": {"q4": (19.9, "qwen2.5:32b"), "q6": (27.0, "qwen2.5:32b-instruct-q6_K")}},
}
QUANT_NAME = {"q4": "Q4_K_M", "q6": "Q6_K", "q8": "Q8_0"}
EMBED = ("bge-m3", 1.2)
OVERHEAD = 0.4
CTX = {"writer": (8192, 4096), "extractor": (8192, 4096), "checker": (4096, 4096)}
TASK_LABEL = {"writer": "Cover letters and answers (EN/ES)", "extractor": "Job facts and fit score",
              "checker": "Fact check (second opinion)", "embed": "CV-to-job matching (embeddings)"}
MIN_OLLAMA = (0, 5, 0)  # JSON-schema structured output


def need(size: str, quant: str, ctx: int) -> float:
    return round(FAMILY[size]["q"][quant][0] + FAMILY[size]["kv"] * ctx / 1024 + OVERHEAD, 1)


def _tag(size: str, quant: str) -> str:
    return FAMILY[size]["q"][quant][1]


def _installed(models: dict, tag: str) -> bool:
    return tag in models or (":" not in tag and f"{tag}:latest" in models)


def budgets(hw: dict) -> dict:
    """Memory each place can give a model, and which GPU (if any) Ollama will use."""
    ram = hw.get("ram_gb") or 0.0
    cpu = max(0.0, round(ram - 6, 1))
    gpus = [g for g in hw.get("gpus", []) if g.get("ollama_can_use")]
    if hw.get("apple_silicon") and gpus:
        g = gpus[0]
        gpu = round(max(0.0, min(g["vram_gb"], ram - 4) - 0.5), 1)
        # unified: whatever the GPU uses comes out of the same RAM, so the split budget is not GPU + CPU
        return {"kind": "apple", "gpu": gpu, "cpu": cpu, "split": max(gpu, round(ram - 5, 1)), "gpu_name": g["name"],
                "gpu_mem": g["vram_gb"]}
    if gpus:
        g = max(gpus, key=lambda x: x.get("vram_gb") or 0)
        gpu = round(max(0.0, (g.get("vram_gb") or 0) - 1.0), 1)
        return {"kind": g["vendor"].lower(), "gpu": gpu, "cpu": cpu, "split": round(gpu + cpu, 1), "gpu_name": g["name"],
                "gpu_mem": g.get("vram_gb") or 0}
    return {"kind": "cpu", "gpu": 0.0, "cpu": cpu, "split": cpu, "gpu_name": "", "gpu_mem": 0}


def _fit(sizes: list[str], budget: float, task: str, models: dict, upgrade: bool = True):
    """Largest size that fits at the preferred context (else the minimum one), then the best quant with room."""
    pref, low = CTX[task]
    for ctx in (pref, low):
        for s in sizes:
            if need(s, "q4", ctx) <= budget:
                quant = "q4"
                if upgrade:  # Q8 is worth it on small models; on 14B+ Q6_K is near-lossless and faster
                    for q in (("q8", "q6") if s in ("3b", "7b") else ("q6",)):
                        if q in FAMILY[s]["q"] and need(s, q, ctx) <= budget:
                            quant = q
                            break
                # already have this size downloaded at another quant that fits? keep it, skip a big download
                have = [q for q in FAMILY[s]["q"] if _installed(models, _tag(s, q)) and need(s, q, ctx) <= budget]
                if have and quant not in have:
                    quant = max(have, key=lambda q: FAMILY[s]["q"][q][0])
                return s, quant, ctx
        if pref == low:
            break
    return None


def _where(n: float, b: dict) -> str:
    if b["kind"] != "cpu" and n <= b["gpu"]:
        return "gpu"
    if b["kind"] != "cpu" and n <= b["split"]:
        return "split"
    return "cpu" if n <= b["cpu"] else "too_big"


def _pick(task: str, sizes: list[str], b: dict, models: dict, upgrade: bool = True) -> dict | None:
    place = b["gpu"] if b["kind"] != "cpu" else b["cpu"]
    if b["kind"] == "cpu":  # on a CPU speed follows model size: 7B at Q4 at most, bigger only as the slow option
        sizes = [x for x in sizes if x in ("7b", "3b")]
    got = _fit(sizes, place, task, models, upgrade=upgrade and b["kind"] != "cpu")
    if not got:
        return None
    s, q, ctx = got
    n = need(s, q, ctx)
    return {"task": task, "label": TASK_LABEL[task], "model": _tag(s, q), "size": s.upper(), "quant": QUANT_NAME[q],
            "num_ctx": ctx, "need_gb": n, "where": _where(n, b), "installed": _installed(models, _tag(s, q)),
            "download_gb": 0.0 if _installed(models, _tag(s, q)) else FAMILY[s]["q"][q][0]}


def _ver(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("-")[0].split(".")[:3])
    except ValueError:
        return (0, 0, 0)


def recommend(hw: dict, ollama: dict | None = None, current: dict | None = None) -> dict:
    """The plan for this computer: per-task models and context, warnings, a fallback, and plain-language reasons."""
    ollama = ollama or {}
    models = ollama.get("models") or {}
    b = budgets(hw)
    warnings: list[str] = []
    reasons: list[str] = []
    tips: list[str] = []
    gpu_on = b["kind"] != "cpu"

    writer = _pick("writer", ["32b", "14b", "7b", "3b"], b, models)
    w_size = writer["size"].lower() if writer else "3b"
    # scoring runs on every job, so it stays at Q4 (faster); at the writer's size it reuses the writer's exact model
    if w_size == "32b":
        extractor = _pick("extractor", ["14b", "7b"], b, models, upgrade=False)
    elif gpu_on and w_size == "14b":
        extractor = _pick("extractor", ["14b", "7b"], b, models, upgrade=False)
    else:
        extractor = _pick("extractor", ["7b", "3b"], b, models, upgrade=False)
    if writer and extractor and extractor["size"] == writer["size"] and extractor["num_ctx"] == writer["num_ctx"]:
        extractor = {**writer, "task": "extractor", "label": TASK_LABEL["extractor"]}  # one model, no swapping
    checker = _pick("checker", ["7b", "3b"], b, {m: s for m, s in models.items() if "14b" not in m and "32b" not in m})
    if checker:  # keep the checker at Q4: a second opinion doesn't need the extra download
        s = checker["size"].lower()
        checker = {**checker, "model": _tag(s, "q4"), "quant": "Q4_K_M", "need_gb": need(s, "q4", checker["num_ctx"]),
                   "installed": _installed(models, _tag(s, "q4")),
                   "download_gb": 0.0 if _installed(models, _tag(s, "q4")) else FAMILY[s]["q"]["q4"][0]}
    if writer and checker and checker["size"] == writer["size"]:  # same size anyway: reuse it, skip a download
        checker = {**writer, "task": "checker", "label": TASK_LABEL["checker"], "num_ctx": CTX["checker"][0],
                   "need_gb": need(writer["size"].lower(), {v: k for k, v in QUANT_NAME.items()}[writer["quant"]],
                                   CTX["checker"][0])}
    embed = {"task": "embed", "label": TASK_LABEL["embed"], "model": EMBED[0], "size": "568M", "quant": "F16",
             "num_ctx": None, "need_gb": EMBED[1], "where": "gpu" if gpu_on else "cpu",
             "installed": _installed(models, EMBED[0]), "download_gb": 0.0 if _installed(models, EMBED[0]) else EMBED[1]}

    level = "good"
    if not writer:  # not even 3B fits fully: run 3B split/CPU as the last resort
        level = "too_weak"
        writer = {"task": "writer", "label": TASK_LABEL["writer"], "model": _tag("3b", "q4"), "size": "3B",
                  "quant": "Q4_K_M", "num_ctx": 4096, "need_gb": need("3b", "q4", 4096),
                  "where": _where(need("3b", "q4", 4096), b), "installed": _installed(models, _tag("3b", "q4")),
                  "download_gb": 0.0 if _installed(models, _tag("3b", "q4")) else 1.9}
    extractor = extractor or {**writer, "task": "extractor", "label": TASK_LABEL["extractor"]}
    checker = checker or {**writer, "task": "checker", "label": TASK_LABEL["checker"]}
    tasks = [writer, extractor, checker, embed]

    # ---- the verdict, in plain words ----
    if w_size == "3b" or level == "too_weak":
        level = "too_weak" if (hw.get("ram_gb") or 0) < 8 or level == "too_weak" else "weak"
    elif w_size == "7b" and not gpu_on:
        level = "slow"
    elif w_size == "7b":
        level = "ok"
    else:
        level = "good" if w_size == "14b" else "great"

    where = {"apple": f"{b['gpu_name']} with {hw.get('ram_gb')} GB unified memory (macOS lets the GPU use about "
                      f"{b['gpu_mem']} GB of it)",
             "cpu": f"no GPU Ollama can use, so models run on the CPU ({hw.get('cpu') or 'CPU'}, {hw.get('cores')} "
                    f"threads) from {hw.get('ram_gb')} GB RAM"}.get(
        b["kind"], f"{b['gpu_name']} with {b['gpu_mem']} GB VRAM, plus {hw.get('ram_gb')} GB RAM")
    reasons.append(f"This computer: {where}. About {b['gpu'] if gpu_on else b['cpu']} GB is free for one model "
                   f"after leaving room for the system and other apps.")
    reasons.append(f"Letters: {writer['size']} at {writer['quant']} ({writer['need_gb']} GB with a "
                   f"{writer['num_ctx']}-token context) is the largest that fits {'fully on the GPU' if writer['where'] == 'gpu' else 'here'}."
                   " For writing, a bigger model matters more than precision, so the size is chosen first.")
    if extractor["model"] == writer["model"]:
        reasons.append("Scoring uses the same model as letters: it fits, scores more consistently than 7B, and "
                       "Ollama never has to swap models between jobs.")
    else:
        reasons.append(f"Scoring uses {extractor['size']}: it runs on every new job, so it has to stay quick; "
                       f"{extractor['size']} still follows the scoring rubric reliably.")
    if checker["model"] != writer["model"]:
        reasons.append("The fact check uses a different, smaller model on purpose: a second opinion that doesn't "
                       "share the writer's mistakes. Its prompts are short, so 4k context is enough.")
    else:
        reasons.append("The fact check reuses the letter model (nothing else fits better here) with a short 4k "
                       "context. The rule-based validator still checks every claim against your facts.")
    reasons.append("Context: 8k tokens fits a long job ad, the matched CV facts and the instructions. Too small a "
                   "context makes Ollama silently cut the prompt, which is the most common cause of wrong scores.")

    if level == "great":
        reasons.append("This machine has room for 32B letters: noticeably better tone and structure, especially in Spanish.")
    if level == "slow":
        warnings.append("No usable GPU: a letter takes a few minutes and scoring a batch of jobs can take an hour. "
                        "It works because jobhunter runs in the background, but keep the computer awake.")
    if level == "weak":
        warnings.append("Only a 3B model fits. Scores are usable, but letters (Spanish especially) are plainer and need "
                        "a careful read before you send them.")
    if level == "too_weak":
        warnings.append(f"This computer is too small for good results ({hw.get('ram_gb')} GB RAM"
                        f"{'' if gpu_on else ', no usable GPU'}). jobhunter can still run 3B models, slowly.")
    fallback = None
    if level in ("weak", "too_weak", "slow"):
        fallback = ("Better option: run Ollama on a stronger computer you own (a desktop with an NVIDIA card with 8 GB "
                    "or more, or a Mac with 16 GB or more), start it with OLLAMA_HOST=0.0.0.0, and put "
                    "http://<that-computer>:11434 in llm.ollama.host. Keep it on your home network or a private VPN; "
                    "never expose port 11434 to the internet.")
    # a bigger writer that only fits when part of it runs on the CPU
    alternative = None
    bigger = {"3b": "7b", "7b": "14b", "14b": "32b"}.get(w_size)
    if bigger and not gpu_on and bigger == "14b" and need("14b", "q4", 8192) <= b["cpu"]:
        n = need("14b", "q4", 8192)
        alternative = {"model": _tag("14b", "q4"), "size": "14B", "need_gb": n, "num_ctx": 8192,
                       "installed": _installed(models, _tag("14b", "q4")),
                       "why": "Better letters, especially in Spanish, but about twice as slow as 7B on the CPU "
                              "(several minutes per letter). Scoring stays on 7B."}
    elif bigger and gpu_on and need(bigger, "q4", 8192) <= b["split"]:
        n = need(bigger, "q4", 8192)
        if _where(n, b) == "split" and b["gpu"] / n >= 0.6:  # mostly on the CPU would be far too slow
            alternative = {"model": _tag(bigger, "q4"), "size": bigger.upper(), "need_gb": n, "num_ctx": 8192,
                           "installed": _installed(models, _tag(bigger, "q4")),
                           "why": f"Better letters with {bigger.upper()}, but about {round(n - b['gpu'], 1)} GB of it "
                                  "runs on the CPU, so each letter takes 2-5x longer. Scoring stays on the GPU."}
    if b["kind"] == "apple" and bigger == "14b" and need("14b", "q4", 8192) <= (hw.get("ram_gb") or 0) - 4:
        tips.append("Advanced (Mac): `sudo sysctl iogpu.wired_limit_mb=12288` lets the GPU use more unified memory, "
                    "enough for 14B letters. It resets at restart; close heavy apps while jobhunter writes.")

    # ---- other hardware notes ----
    for g in hw.get("gpus", []):
        if not g.get("ollama_can_use") and not g.get("integrated") and g["vendor"] in ("AMD", "Intel"):
            warnings.append(f"{g['name']}: {g['why']}, so Ollama uses the CPU instead.")
    if len([g for g in hw.get("gpus", []) if g.get("ollama_can_use")]) > 1 and b["kind"] != "apple":
        reasons.append("Several usable GPUs: Ollama can split one model across them, so the numbers above are "
                       "conservative (they use the biggest card only).")
    if gpu_on and b["kind"] != "apple" and writer["size"] == "14B" and b["gpu_mem"] < 16:
        tips.append("Set OLLAMA_FLASH_ATTENTION=1 and OLLAMA_KV_CACHE_TYPE=q8_0 (system environment variables, then "
                    "quit and reopen Ollama): the context then takes half the VRAM, which gives more headroom.")
    if writer["num_ctx"] < 8192:
        tips.append("The context had to be cut to 4k to fit. OLLAMA_FLASH_ATTENTION=1 plus OLLAMA_KV_CACHE_TYPE=q8_0 "
                    "halves its memory; long job ads may otherwise be cut short.")
    if level in ("weak", "too_weak") or (b["kind"] == "apple" and (hw.get("ram_gb") or 0) <= 16):
        tips.append("OLLAMA_MAX_LOADED_MODELS=1 keeps only one model in memory at a time, so other apps stay responsive.")

    # ---- Ollama itself ----
    state = "not_installed" if not ollama.get("installed") else "not_running" if not ollama.get("running") else "ok"
    if state == "not_installed":
        warnings.insert(0, "Ollama is not installed. Get it free from ollama.com/download, open it once, then come back.")
    elif state == "not_running":
        warnings.insert(0, "Ollama is installed but not running.")
    elif _ver(ollama.get("version", "")) < MIN_OLLAMA:
        warnings.insert(0, f"Ollama {ollama.get('version')} is too old for structured output: update to 0.5 or newer.")
    if ollama and not ollama.get("local", True):
        reasons.insert(0, f"Ollama runs on another computer ({ollama.get('host')}): these suggestions are for THIS "
                          "computer's hardware, so check them against the one running Ollama.")
    for m in ollama.get("loaded") or []:
        if m.get("size_gb") and m.get("gpu_share", 1) < 0.95 and gpu_on:
            warnings.append(f"{m['name']} is loaded with only {int(m['gpu_share'] * 100)}% on the GPU; the rest runs "
                            "on the CPU, which is much slower. Close GPU-heavy apps or apply the suggestion below.")

    download = round(sum(t["download_gb"] for t in {t["model"]: t for t in tasks}.values()), 1)
    if b["kind"] != "cpu":
        disk_free = None
        try:
            disk_free = round(shutil.disk_usage(Path.home()).free / 1024 ** 3, 1)
        except OSError:
            pass
        if disk_free is not None and download > disk_free - 5:
            warnings.append(f"Downloading {download} GB needs more free disk space (about {disk_free} GB free).")

    cur = current or {}
    applied = bool(cur) and all(cur.get(t["task"]) == t["model"] for t in tasks)
    return {"level": level, "budget": b, "tasks": tasks, "alternative": alternative, "fallback": fallback,
            "warnings": warnings, "reasons": reasons, "tips": tips, "download_gb": download, "ollama_state": state,
            "server_env": server_env(level),
            "applied": applied}


def server_env(level: str = "ok") -> dict:
    """Ollama's own settings (environment variables of the Ollama server, set once per computer).
    Flash attention + an 8-bit KV cache halve the context's memory; one request at a time keeps one KV cache;
    two loaded models let the extractor and the embedder stay in memory together."""
    return {"OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0", "OLLAMA_NUM_PARALLEL": "1",
            "OLLAMA_MAX_LOADED_MODELS": "1" if level in ("weak", "too_weak") else "2"}


def apply(plan: dict, config_path: Path, use_alternative: bool = False) -> dict:
    """Write the plan into config.yaml (llm.models, llm.num_ctx), keeping a dated backup."""
    import yaml
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    shutil.copy2(config_path, config_path.with_name(f"config.yaml.bak-{datetime.now():%Y%m%d-%H%M%S}"))
    llm = raw.setdefault("llm", {})
    models = llm.setdefault("models", {})
    ctx = llm.get("num_ctx") if isinstance(llm.get("num_ctx"), dict) else {}
    for t in plan["tasks"]:
        models[t["task"]] = t["model"]
        if t["num_ctx"]:
            ctx[t["task"]] = t["num_ctx"]
    if use_alternative and plan.get("alternative"):
        models["writer"] = plan["alternative"]["model"]
        ctx["writer"] = plan["alternative"]["num_ctx"]
    llm["num_ctx"] = ctx
    llm["tuned_for"] = f"{datetime.now():%Y-%m-%d} {plan['level']} ({plan['budget']['kind']}, {plan['budget']['gpu'] or plan['budget']['cpu']} GB)"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
    return {"models": models, "num_ctx": ctx}
