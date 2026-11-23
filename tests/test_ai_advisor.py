"""Model suggestions per device: the plan must fit the memory, stay sensible on weak machines, and write config."""
import yaml

from jobhunter import ai_advisor as A


def gpu(name, vram, vendor="NVIDIA", ok=True):
    return {"vendor": vendor, "name": name, "vram_gb": vram, "ollama_can_use": ok, "integrated": False, "why": "CUDA"}


def pc(ram, *gpus):
    return {"os": "Windows", "arch": "AMD64", "cpu": "Ryzen 7", "cores": 16, "ram_gb": ram, "gpus": list(gpus),
            "apple_silicon": False, "unified_memory": False}


def mac(ram):
    lim = round(ram * (0.75 if ram >= 64 else 0.67), 1)
    return {"os": "Darwin", "arch": "arm64", "cpu": "Apple M1", "cores": 8, "ram_gb": ram, "apple_silicon": True,
            "unified_memory": True, "gpus": [{"vendor": "Apple", "name": "Apple M1 GPU", "vram_gb": lim,
                                              "ollama_can_use": True, "integrated": False, "why": "Metal"}]}


RUNNING = {"installed": True, "running": True, "version": "0.12.3", "local": True, "models": {}, "loaded": []}


def by_task(plan):
    return {t["task"]: t for t in plan["tasks"]}


def test_every_gpu_pick_fits_its_budget():
    for hw in (pc(32, gpu("RTX 3060", 12)), pc(32, gpu("RTX 4070", 8)), pc(64, gpu("RTX 4090", 24)), mac(16),
               mac(24), mac(36), mac(8)):
        plan = A.recommend(hw, RUNNING)
        b = plan["budget"]
        for t in plan["tasks"]:
            if t["task"] != "embed" and plan["level"] != "too_weak":
                assert t["need_gb"] <= b["gpu"], (hw["gpus"][0]["name"], hw["ram_gb"], t)
                assert t["where"] == "gpu"


def test_12gb_card_gets_14b_for_letters_and_scoring():
    t = by_task(A.recommend(pc(32, gpu("RTX 3060", 12)), RUNNING))
    assert t["writer"]["model"] == "qwen2.5:14b" and t["writer"]["num_ctx"] == 8192
    assert t["extractor"]["model"] == t["writer"]["model"]  # no swapping between jobs
    assert t["checker"]["model"] == "qwen2.5:7b-instruct"   # independent second opinion
    assert t["embed"]["model"] == "bge-m3"


def test_8gb_card_7b_with_bigger_split_option():
    plan = A.recommend(pc(32, gpu("RTX 4060", 8)), RUNNING)
    assert by_task(plan)["writer"]["size"] == "7B"
    assert plan["level"] == "ok"
    assert plan["alternative"] and plan["alternative"]["model"] == "qwen2.5:14b"


def test_24gb_card_gets_32b_letters():
    plan = A.recommend(pc(64, gpu("RTX 4090", 24)), RUNNING)
    assert by_task(plan)["writer"]["model"] == "qwen2.5:32b" and plan["level"] == "great"
    assert by_task(plan)["extractor"]["size"] == "14B"


def test_macs_by_unified_memory():
    assert by_task(A.recommend(mac(16), RUNNING))["writer"]["size"] == "7B"
    assert by_task(A.recommend(mac(24), RUNNING))["writer"]["size"] == "14B"
    assert by_task(A.recommend(mac(36), RUNNING))["writer"]["size"] == "32B"
    p8 = A.recommend(mac(8), RUNNING)
    assert by_task(p8)["writer"]["size"] == "3B" and p8["level"] in ("weak", "too_weak") and p8["fallback"]


def test_cpu_only_is_slow_but_works():
    plan = A.recommend(pc(32), RUNNING)
    assert plan["level"] == "slow" and by_task(plan)["writer"]["where"] == "cpu" and plan["fallback"]


def test_tiny_machine_too_weak_with_fallback():
    plan = A.recommend(pc(6), RUNNING)
    assert plan["level"] == "too_weak" and plan["fallback"] and plan["warnings"]


def test_unsupported_amd_card_falls_back_to_cpu():
    hw = pc(16, gpu("AMD Radeon RX 580", 8, vendor="AMD", ok=False) | {"why": "not on ROCm list"})
    plan = A.recommend(hw, RUNNING)
    assert plan["budget"]["kind"] == "cpu"
    assert any("RX 580" in w for w in plan["warnings"])


def test_installed_quant_is_reused_and_download_counted():
    have = dict(RUNNING, models={"qwen2.5:14b": 9.0, "bge-m3:latest": 1.2})
    plan = A.recommend(pc(32, gpu("RTX 4080", 16)), have)
    w = by_task(plan)["writer"]
    assert w["model"] == "qwen2.5:14b" and w["installed"]  # q8 would fit, but keep what's downloaded
    assert by_task(plan)["embed"]["installed"]
    assert plan["download_gb"] == 4.7  # only the 7B checker


def test_ollama_states_warn():
    assert A.recommend(mac(16), {"installed": False})["ollama_state"] == "not_installed"
    assert A.recommend(mac(16), {"installed": True, "running": False})["ollama_state"] == "not_running"
    old = A.recommend(mac(16), dict(RUNNING, version="0.4.7"))
    assert any("too old" in w for w in old["warnings"])
    spill = A.recommend(pc(32, gpu("RTX 3060", 12)),
                        dict(RUNNING, loaded=[{"name": "qwen2.5:14b", "size_gb": 11, "vram_gb": 7, "gpu_share": 0.6}]))
    assert any("60% on the GPU" in w for w in spill["warnings"])


def test_apply_writes_models_and_context(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"llm": {"ollama": {"host": "http://localhost:11434"}, "models": {"vision": ""}}}))
    plan = A.recommend(pc(32, gpu("RTX 4060", 8)), RUNNING)
    A.apply(plan, cfg, use_alternative=True)
    raw = yaml.safe_load(cfg.read_text())
    assert raw["llm"]["models"]["writer"] == "qwen2.5:14b" and raw["llm"]["models"]["vision"] == ""
    assert raw["llm"]["num_ctx"]["checker"] == 4096 and raw["llm"]["tuned_for"]
    assert list(tmp_path.glob("config.yaml.bak-*"))
    from jobhunter import llm
    loaded = {"llm": raw["llm"]}
    assert llm.task_ctx(loaded, "checker") == 4096 and llm.task_ctx(loaded, "vision") == 8192
    assert A.recommend(pc(32, gpu("RTX 4060", 8)), RUNNING,
                       {t: llm.model(loaded, t) for t in ("writer", "extractor", "checker", "embed")})["applied"] is False


def test_detect_runs_here_without_raising():
    from jobhunter import hardware
    hw = hardware.detect(refresh=True)
    assert hw["ram_gb"] > 0 and isinstance(hw["gpus"], list)
    info = hardware.ollama_info("http://127.0.0.1:9")
    assert info["running"] is False


def test_missing_facts_profile_is_reported_not_crashing(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from jobhunter import master, connections
    from jobhunter.review.app import app
    monkeypatch.setattr(master, "PROFILE_PATH", tmp_path / "facts" / "master_profile.json")
    monkeypatch.setattr(connections, "disk_encryption", lambda *a, **k: {"tool": "x", "on": True, "detail": "on"})
    monkeypatch.setattr(connections, "ollama_state", lambda cfg: {"state": "ok", "missing": [], "host": "h", "pull": {}})
    monkeypatch.setattr(connections, "gmail_state", lambda cfg, refresh=False: {"state": "ok"})
    c = TestClient(app)
    q = c.get("/api/queue")
    assert q.status_code == 200 and q.json()["profile_missing"] is True
    assert "facts" in connections.status()["setup_needed"]
