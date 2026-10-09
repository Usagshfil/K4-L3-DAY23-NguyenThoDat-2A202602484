"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"RUNBOOK [Step {n}: {name}]", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    if auto:
        return True
    try:
        ans = input(f"{msg} (y/N): ").strip().lower()
        return ans == "y"
    except EOFError:
        return True


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    t_start = time.time()

    # Bước 1: 1 xac_nhan_outage — probe 3 lần liên tiếp (chống flapping)
    prim_alive = False
    for i in range(3):
        try:
            r = httpx.get(f"{URL[primary]}/readyz", timeout=2.0)
            if r.status_code == 200:
                prim_alive = True
                break
        except Exception:
            pass
        if i < 2:
            time.sleep(5.0)
    step(1, "xac_nhan_outage", primary=primary, primary_ready=prim_alive, target=target)

    # Bước 2: 2 thong_bao_incident
    if not confirm(auto, f"Xác nhận outage trên region {primary}. Tiến hành failover sang {target}?"):
        step(2, "thong_bao_incident", confirmed=False, status="aborted_by_operator")
        return {"ok": False, "aborted": True}
    step(2, "thong_bao_incident", confirmed=True, operator="on-call")

    # Bước 3: 3 scale_gpu_pool (gọi failover MỘT LẦN DUY NHẤT)
    fo_res = fo.failover(target=target, backend=backend)
    step(3, "scale_gpu_pool", ok=fo_res.get("ok"), target=target)
    if not fo_res.get("ok"):
        return {"ok": False, "error": "failover_failed", "fo_res": fo_res}

    # Bước 4: 4 verify_state_replica (chỉ đọc kết quả từ region phụ)
    try:
        target_state = httpx.get(f"{URL[target]}/v1/state", timeout=2.0).json()
    except Exception:
        target_state = {}
    step(
        4,
        "verify_state_replica",
        target=target,
        vector_count=target_state.get("count"),
        weights=target_state.get("weights"),
        pool_state=target_state.get("pool_state"),
        rpo_seconds=fo_res.get("rpo_seconds"),
        docs_lost=fo_res.get("docs_lost"),
    )

    # Bước 5: 5 dns_cutover (xác nhận lại active_region)
    active_path = pathlib.Path("edge/active_region")
    active_reg = active_path.read_text().strip() if active_path.exists() else None
    cutover_ok = active_reg == target
    step(5, "dns_cutover", active_region=active_reg, target=target, ok=cutover_ok)

    # Bước 6: 6 verify_golden_signals (10 request thật vào region phụ)
    latencies = []
    errors = 0
    for _ in range(10):
        t0 = time.time()
        try:
            resp = httpx.get(f"{URL[target]}/v1/infer", timeout=3.0)
            lat = (time.time() - t0) * 1000
            latencies.append(lat)
            if resp.status_code != 200:
                errors += 1
        except Exception:
            errors += 1
        time.sleep(0.05)

    latencies.sort()
    p95 = round(latencies[int(len(latencies) * 0.95) - 1] if latencies else 0.0, 1)
    error_rate = round(errors / 10.0, 2)
    step(6, "verify_golden_signals", p95_latency_ms=p95, error_rate=error_rate, sample_count=10)

    # Bước 7: 7 post_incident
    elapsed_s = round(time.time() - t_start, 2)
    step(
        7,
        "post_incident",
        elapsed_s=elapsed_s,
        status="resolved",
        cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl",
    )

    return {
        "ok": True,
        "elapsed_s": elapsed_s,
        "target": target,
        "p95_latency_ms": p95,
        "error_rate": error_rate,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
