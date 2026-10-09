"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        **kw,
    }
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec))
    return rec


def failover(target: str, backend: str, wait: float = 60.0) -> dict:
    primary = "a" if target == "b" else "b"

    # Bước 1: 1_verify_target
    try:
        r = httpx.get(f"{URL[target]}/v1/state", timeout=5.0)
        target_state = r.json() if r.status_code == 200 else {}
    except Exception as e:
        target_state = {"error": type(e).__name__}
    emit(step="1_verify_target", target=target, state=target_state)

    # Bước 2: 2_restore_snapshot
    meta = snapshot.get(target, backend)
    primary_db = pathlib.Path(f"state/region-{primary}/vectors.sqlite")
    target_db = pathlib.Path(f"state/region-{target}/vectors.sqlite")
    rpo_info = snapshot.rpo(primary_db, target_db)

    emit(
        step="2_restore_snapshot",
        target=target,
        backend=backend,
        rpo_seconds=rpo_info["rpo_seconds"],
        docs_lost=rpo_info["docs_lost"],
        embed_model_version=meta.get("embed_model_version"),
        snapshot_meta=meta,
    )

    # Bước 3: 3_scale_pool
    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text("full")
    emit(step="3_scale_pool", target=target, pool_state="full")

    # Bước 4: 4_wait_ready (chờ warm-up xong)
    t0 = time.time()
    ready = False
    while time.time() - t0 < wait:
        try:
            res = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            if res.status_code == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.5)

    waited_s = round(time.time() - t0, 2)
    emit(step="4_wait_ready", target=target, ready=ready, waited_s=waited_s)

    # Nếu target không ready -> ABORT, tuyệt đối không cutover DNS!
    if not ready:
        return {
            "ok": False,
            "error": "target_not_ready_timeout",
            "waited_s": waited_s,
            "target": target,
        }

    # Bước 5: 5_dns_cutover (chỉ cutover khi target đã hoàn toàn ready)
    active_file = pathlib.Path("edge/active_region")
    active_file.parent.mkdir(parents=True, exist_ok=True)
    active_file.write_text(target)
    emit(step="5_dns_cutover", target=target, active_region=target)

    return {
        "ok": True,
        "target": target,
        "rpo_seconds": rpo_info["rpo_seconds"],
        "docs_lost": rpo_info["docs_lost"],
        "waited_s": waited_s,
        "embed_model_version": meta.get("embed_model_version"),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
