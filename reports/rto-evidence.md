# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | 2026-10-09T04:40:52 | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.0s | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | NO_RECOVERY | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0s | `action:kill` | `chaos/chaos-events.jsonl:4` |
| User thấy lỗi đầu tiên | +0.1s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | +15.1s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +16.5s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:10` |
| Region phụ ready | +16.6s | `step:4_wait_ready` | `reports/failover-events.jsonl:12` |
| DNS cutover | +16.6s | `step:5_dns_cutover` | `reports/failover-events.jsonl:13` |
| **RTO đo được** | 16.9s | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:33` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | 16.9s | 300s (5 phút) | PASS |
| RPO — Vector DB | 2.0s / 1 doc | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 15.0s | `interval_s × threshold` trong `reports/health-events.jsonl:2` | Giảm interval (ví dụ xuống 2s) hoặc threshold, nhưng phải đánh đổi bằng nguy cơ flapping cao hơn do mạng dao động ngắn hạn. |
| Snapshot restore | 0.01s | 2_restore → 3_scale trong `reports/failover-events.jsonl:10` | Dùng volume snapshot cấp hạ tầng (như AWS EBS snapshot / Copy-on-Write) hoặc replication incremental streaming (WAL) thay vì full backup copy. |
| GPU pool warm-up | 0.1s | `waited_s` ở `4_wait_ready` trong `reports/failover-events.jsonl:12` | Duy trì active-warm standby pool (nạp sẵn trọng số mô hình vào VRAM GPU), sử dụng vLLM sleep mode hoặc CUDA graph pre-compilation. |
| DNS/LB TTL cache | 0.3s | t_recovered − t_cutover trong `reports/drill-2-withdr.jsonl:33` | Hạ TTL ở tầng DNS xuống 1s - 2s hoặc chuyển sang Anycast Global Load Balancer (AWS Global Accelerator / Cloudflare) để định tuyến tức thời tại edge. |
