"""A stand-in for Ollama in tests: deterministic embeddings and scripted structured outputs."""
from __future__ import annotations

import hashlib
import re

import numpy as np

SYN = {"intune": "endpoint", "autopilot": "endpoint", "mdm": "endpoint", "kandji": "macos", "iru": "macos",
       "mac": "macos", "okta": "identity", "sso": "identity", "mfa": "identity", "iam": "identity",
       "entra": "identity", "edr": "security", "bitdefender": "security", "iso": "governance",
       "27001": "governance", "policies": "governance", "tickets": "servicedesk", "sla": "servicedesk",
       "jira": "servicedesk", "salary": "salary", "salario": "salary", "relocate": "relocate",
       "sponsorship": "sponsor", "visa": "sponsor", "authorised": "workauth", "authorized": "workauth",
       "permiso": "workauth", "right": "workauth"}


def fake_embed(cfg, texts):
    out = []
    for t in texts:
        v = np.zeros(256, dtype=np.float32)
        for w in re.findall(r"[a-z0-9]+", t.lower()):
            for tok in {w, SYN.get(w, w)}:
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                v[h % 256] += 1.0
        out.append((v / (np.linalg.norm(v) or 1)).tolist())
    return out


class Script:
    """Queue of responses for llm.structured, matched by task."""

    def __init__(self):
        self.queue: dict[str, list] = {}
        self.calls: list[dict] = []

    def push(self, task: str, out):
        self.queue.setdefault(task, []).append(out)

    def structured(self, cfg, system, prompt, schema, *, task="extractor", temperature=0.0, num_ctx=None, images=None):
        self.calls.append({"task": task, "system": system, "prompt": prompt, "schema": schema})
        q = self.queue.get(task) or []
        if not q:
            if task == "checker" and "results" in schema.get("properties", {}):
                return {"results": []}
            raise RuntimeError(f"no scripted output for {task}")
        out = q.pop(0)
        return out(prompt, schema) if callable(out) else out
