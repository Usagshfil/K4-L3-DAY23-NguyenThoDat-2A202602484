# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | `a.ready=false` 3 lần liên tiếp | on-call |
| 2 | Mở incident + bấm giờ RTO | `python3 dr/runbook.py --primary a --target b --backend fs` | ts ghi vào `reports/runbook-run.jsonl` | Incident Commander / on-call |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py get --region b --backend fs` | `MANIFEST.json` và `vectors.sqlite` có mặt ở `state/region-b`; log `2_restore_snapshot` có `docs_lost` và `rpo_seconds` | automation / on-call |
| 4 | Scale pool warm→full | `echo full > state/region-b/pool_state` | `curl -s localhost:8002/readyz` trả HTTP 200 sau khi warmup kết thúc | automation / on-call |
| 5 | DNS/LB cutover | `printf b > edge/active_region` | `curl -s localhost:8080/edge/state` trả `active_region=b` | automation / on-call |
| 6 | Verify golden signals | `curl -s localhost:8080/v1/infer` (gửi 10 request) | p95 latency < 200ms, error rate < 1% (10/10 request trả 200) | on-call |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl` | `rto_verdict` trả về `PASS` (RTO ≤ 300s) | Incident Commander |

**Rollback (failover ngược):** điều kiện nào thì trả traffic về region A? Ai quyết định?
- **Điều kiện rollback:**
  1. Region A đã được khôi phục, khởi động lại thành công, và vượt qua health check liên tục trong tối thiểu 15 phút không có lỗi.
  2. Toàn bộ dữ liệu mới được ingest tại Region B trong thời gian sự cố đã được sync ngược (`reverse replication`) về Region A mà không bị mất mát hay xung đột.
  3. Quá trình chuyển ngược traffic phải thực hiện ngoài giờ cao điểm (maintenance window) với sự giám sát chặt chẽ.
- **Ai quyết định:**
  Incident Commander (hoặc Lead SRE on-call) phê duyệt thủ công sau khi kiểm tra đủ 3 điều kiện trên. Tuyệt đối không tự động rollback bằng script để tránh flapping gây gián đoạn dịch vụ hai chiều liên tục (§4 Anti-Patterns).
