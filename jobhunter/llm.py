"""Local Ollama only. No paid APIs.

- ollama_structured(): JSON Schema in Ollama's `format`, so decoding is constrained to valid output.
- embed(): vectors from a local embedding model (bge-m3 by default).
Models per task come from config `llm.models` (writer / extractor / checker / embed / vision).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

log = logging.getLogger(__name__)

DEFAULT_MODELS = {"writer": "qwen2.5:14b", "extractor": "qwen2.5:7b-instruct", "checker": "qwen2.5:7b-instruct",
                  "embed": "bge-m3", "vision": "qwen2.5vl:7b"}


def _o(cfg: dict) -> dict:
    return cfg.get("llm", {}).get("ollama", {}) or {}


def host(cfg: dict) -> str:
    return (_o(cfg).get("host") or "http://localhost:11434").rstrip("/")


def configured_model(cfg: dict, task: str) -> str:
    models = cfg.get("llm", {}).get("models") or {}
    if task in models:
        return models[task]
    if task == "writer" and _o(cfg).get("model"):
        return _o(cfg)["model"]
    return DEFAULT_MODELS[task]


def model(cfg: dict, task: str) -> str:
    """The model for a task: the configured one, unless it ran out of memory this session (see _smaller)."""
    want = configured_model(cfg, task)
    return _fallback.get((task, want), want)


# ---- out of memory: drop to the next smaller installed Qwen for this session ----

class OutOfMemory(RuntimeError):
    pass


_fallback: dict[tuple[str, str], str] = {}  # (task, configured model) -> smaller model used instead, until restart
OOM = re.compile(r"out of memory|requires more system memory|cudaMalloc|insufficient memory|not enough memory|"
                 r"failed to allocate|unable to allocate", re.I)
_SIZE = re.compile(r":(\d+(?:\.\d+)?)b", re.I)


def _size(name: str) -> float | None:
    m = _SIZE.search(name)
    return float(m.group(1)) if m else None


def _smaller(cfg: dict, current: str) -> str | None:
    """The largest installed model of the same family that is smaller than `current` (prefers the default quant)."""
    fam, size = current.split(":")[0], _size(current)
    if size is None:
        return None
    cands = [n for n in available(cfg) if n.split(":")[0] == fam and (_size(n) or 1e9) < size]
    if not cands:
        return None
    return max(cands, key=lambda n: (_size(n), "-q" not in n, -len(n)))


def fallbacks() -> list[dict]:
    """What the Configuration page shows: tasks running on a smaller model because the chosen one didn't fit."""
    return [{"task": t, "wanted": w, "using": u} for (t, w), u in _fallback.items()]


def task_ctx(cfg: dict, task: str) -> int:
    """Context per task: llm.num_ctx.<task> (set by the Local AI suggestion), else llm.ollama.num_ctx, else 8192."""
    per = cfg.get("llm", {}).get("num_ctx")
    if isinstance(per, dict) and per.get(task):
        return int(per[task])
    return int(_o(cfg).get("num_ctx", 8192))


# Sampling per task. Structured (JSON) tasks use repeat_penalty 1.0: a penalty on repeated tokens fights the
# braces, quotes and keys that valid JSON has to repeat. num_predict caps the reply so a model stuck in a loop
# (a known failure of constrained JSON decoding) stops instead of filling the whole context for minutes.
# Override any value in config: llm.options.<task>.<name>.
TASK_OPTIONS = {
    "writer": {"top_p": 0.9, "repeat_penalty": 1.05, "num_predict": 1400},
    "extractor": {"top_p": 0.9, "repeat_penalty": 1.0, "num_predict": 1500},
    "checker": {"top_p": 0.9, "repeat_penalty": 1.0, "num_predict": 800},
    "vision": {"top_p": 0.9, "repeat_penalty": 1.0, "num_predict": 1200},
}
CHARS_PER_TOKEN = 3.0  # conservative for English + Spanish with Qwen's tokenizer (real: ~3.5-4)


def options(cfg: dict, task: str) -> dict:
    out = dict(TASK_OPTIONS.get(task) or TASK_OPTIONS["writer"])
    over = ((cfg.get("llm") or {}).get("options") or {}).get(task)
    if isinstance(over, dict):
        out.update(over)
    return out


def char_budget(cfg: dict, task: str, reserve_tokens: int = 0) -> int:
    """How many prompt characters fit this task's context after the reply (num_predict) and reserve_tokens.
    Ollama silently drops the *start* of a prompt that is too long, which is where the instructions are, so
    prompts are trimmed to this budget instead."""
    free = task_ctx(cfg, task) - int(options(cfg, task).get("num_predict") or 0) - reserve_tokens
    return max(2000, int(free * CHARS_PER_TOKEN))


def _timeout(cfg: dict) -> tuple[float, float]:
    """(connect, read): a stopped Ollama fails in seconds; a slow first load (big model, CPU) gets the full time."""
    return (10.0, float(_o(cfg).get("timeout_s", 600)))


def reachable(cfg: dict) -> bool:
    try:
        return requests.get(f"{host(cfg)}/api/version", timeout=4).ok
    except requests.RequestException:
        return False


def _extract_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"No JSON in LLM output: {text[:200]}")
    return json.loads(m.group(0))


def chat(cfg: dict, system: str, prompt: str, *, task: str = "writer", fmt: Any = None, temperature: float = 0.2,
         num_ctx: int | None = None, images: list[str] | None = None) -> str:
    o = _o(cfg)
    user: dict[str, Any] = {"role": "user", "content": prompt}
    if images:
        user["images"] = images  # base64 PNGs, vision models only
    body: dict[str, Any] = {
        "model": model(cfg, task), "stream": False, "keep_alive": o.get("keep_alive", "10m"),
        "options": {**options(cfg, task), "temperature": temperature, "num_ctx": num_ctx or task_ctx(cfg, task)},
        "messages": [{"role": "system", "content": system}, user],
    }
    if fmt is not None:
        body["format"] = fmt
    # letters are long: stream them so Pause can stop one mid-way (closing the stream stops Ollama generating)
    stream = task == "writer" and (cfg.get("llm") or {}).get("stream", True) is not False
    body["stream"] = stream
    post = lambda: requests.post(f"{host(cfg)}/api/chat", json=body, timeout=_timeout(cfg), stream=stream)  # noqa: E731
    r = post()
    if r.status_code >= 500 and OOM.search(r.text or ""):
        smaller = _smaller(cfg, body["model"])
        if not smaller:
            raise OutOfMemory(f"{body['model']} doesn't fit in memory and no smaller model is installed: {r.text[:200]}")
        log.warning("%s ran out of memory for %s; using %s until restart", body["model"], task, smaller)
        _fallback[(task, configured_model(cfg, task))] = smaller
        body["model"] = smaller
        r = post()
    r.raise_for_status()
    data = _read_stream(r) if stream else r.json()
    if data.get("done_reason") == "length":
        log.warning("%s reply hit num_predict (%s tokens): output may be cut", task, body["options"].get("num_predict"))
    return data["message"]["content"]


class Paused(RuntimeError):
    """Background work was paused while a reply was being generated; the job goes back to its queue."""


def _paused() -> bool:
    from .config import DATA
    return (DATA / "paused").exists()


def _read_stream(r) -> dict:
    import time
    parts: list[str] = []
    last: dict = {}
    check = time.time()
    try:
        for line in r.iter_lines():
            if not line:
                continue
            ev = json.loads(line)
            if ev.get("error"):
                raise RuntimeError(f"Ollama: {ev['error']}")
            parts.append((ev.get("message") or {}).get("content") or "")
            last = ev
            if time.time() - check > 0.5:
                check = time.time()
                if _paused():
                    raise Paused("paused while writing")
    finally:
        r.close()
    return {"message": {"content": "".join(parts)}, "done_reason": last.get("done_reason")}


def unload(cfg: dict, task: str) -> bool:
    """Free the model's memory now (keep_alive 0) instead of holding it for keep_alive after the last request."""
    try:
        return requests.post(f"{host(cfg)}/api/generate", json={"model": model(cfg, task), "keep_alive": 0},
                             timeout=(5, 20)).ok
    except requests.RequestException:
        return False


def structured(cfg: dict, system: str, prompt: str, schema: dict, *, task: str = "extractor",
               temperature: float = 0.0, num_ctx: int | None = None, images: list[str] | None = None) -> dict:
    """Schema-constrained output (Ollama >= 0.5 accepts a JSON Schema in `format`)."""
    return _extract_json(chat(cfg, system, prompt, task=task, fmt=schema, temperature=temperature,
                              num_ctx=num_ctx, images=images))


def ollama_json(cfg: dict, system: str, prompt: str, task: str = "extractor", schema: dict | None = None,
                temperature: float = 0.1) -> dict:
    """JSON output. With a schema, decoding is constrained to it; without, free-form JSON (v0.2 classifiers)."""
    return _extract_json(chat(cfg, system, prompt, task=task, fmt=schema or "json", temperature=temperature))


def embed(cfg: dict, texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    r = requests.post(f"{host(cfg)}/api/embed", json={"model": model(cfg, "embed"), "input": texts,
                                                      "keep_alive": _o(cfg).get("keep_alive", "10m")},
                      timeout=_timeout(cfg))
    r.raise_for_status()
    return r.json()["embeddings"]


def available(cfg: dict) -> set[str]:
    try:
        r = requests.get(f"{host(cfg)}/api/tags", timeout=5)
        return {m["name"] for m in r.json().get("models", [])}
    except Exception:
        return set()


def _has(names: set[str], want: str) -> bool:
    return want in names or (":" not in want and f"{want}:latest" in names)


def missing_models(cfg: dict, tasks: tuple[str, ...] = ("writer", "extractor", "checker", "embed")) -> list[str]:
    names = available(cfg)
    return sorted({configured_model(cfg, t) for t in tasks if not _has(names, configured_model(cfg, t))})


def ollama_ok(cfg: dict) -> bool:
    return not missing_models(cfg, ("writer", "extractor"))


# ---- v0.2 compatibility: recruiter replies still use free text from the writer model ----

def draft_text(cfg: dict, system: str, prompt: str, max_tokens: int | None = None) -> str:
    d = cfg.get("llm", {}).get("drafting") or {}
    return chat(cfg, system, prompt, task="writer", temperature=d.get("temperature", 0.3)).strip()
