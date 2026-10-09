# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T05:03:57 | outage bắt đầu | `chaos/chaos-events.jsonl:4` |
| 2026-10-09T05:03:57 | user đầu tiên bị ảnh hưởng | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-09T05:04:12 | health check alert | `reports/health-events.jsonl:2` |
| 2026-10-09T05:04:14 | operator confirm cutover | `reports/runbook-run.jsonl:2` |
| 2026-10-09T05:04:14 | resolved (request đầu tiên OK từ region phụ) | `reports/drill-2-withdr.jsonl:33` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `16.9s` · gap: `-283.1s` (đạt mục tiêu, nhanh hơn 283.1s)
- RPO mục tiêu: 300s · đo được: `2.0s` (`1` doc bị mất) · gap: `-298.0s` (đạt mục tiêu, mất mát cực thấp)
- **Bước tốn nhiều giây nhất:** `Health-check detection floor` (15.1s) — vì sao?
  Health checker cấu hình `interval=5s` và `threshold=3`. Để chống flapping, hệ thống cần tích lũy đúng 3 lần probe liên tiếp thất bại trước khi chuyển trạng thái sang `UNHEALTHY`. Thời gian detection floor lý thuyết là 15.0s, thực tế mất 15.1s, chiếm tới ~89.3% toàn bộ RTO (15.1s / 16.9s).

## 3. Root cause (5 whys)

Không phải "vì tôi chạy chaos script". Câu hỏi: *nếu đây là outage thật, bước nào
trong runbook của tôi sẽ thất bại?*

1. **Tại sao Region A không phục vụ được?** Phân vùng mạng (network partition) hoặc tiến trình bị treo khiến request bị treo cho đến khi timeout.
2. **Tại sao phải cutover sang Region B?** Vì Region A không tự phục hồi trong ngưỡng giám sát và người vận hành cần khôi phục dịch vụ cho khách hàng ngay lập tức.
3. **Nếu đây là sự cố production thật, bước nào trong runbook có nguy cơ thất bại cao nhất?** Bước `2_restore_snapshot` và `3_scale_pool`.
4. **Tại sao hai bước này có thể thất bại trên hệ thống thật?**
   - Kích thước Vector DB và trọng số mô hình AI thực tế lên tới hàng chục hoặc hàng trăm GB. Việc tải và khôi phục snapshot qua cross-region network vào thời điểm xảy ra sự cố có thể mất hàng chục phút, vi phạm cam kết RTO 300s.
   - Khi scale pool, nếu region phụ không có sẵn quota GPU hoặc gặp tình trạng khan hiếm GPU (GPU capacity shortage) từ cloud provider, quá trình cấp phát GPU instance sẽ thất bại.
5. **Cải tiến kiến trúc căn bản là gì?** Cần triển khai kiến trúc Active-Warm Standby hoặc Active-Active: model weights được load sẵn vào bộ nhớ VRAM của GPU standby, vector DB được đồng bộ liên tục qua streaming replication (như WAL streaming), loại bỏ hoàn toàn bước khôi phục file thủ công lúc gặp sự cố.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Cấu hình continuous streaming replication cho Vector DB (thay vì snapshot chu kỳ 30s) | Data Platform Lead | 2026-10-25 | Giảm RPO từ 2.0s xuống < 0.5s |
| 2 | Duy trì Active-Warm GPU pool ở Region B (nạp sẵn model weights vào GPU VRAM) | AI Serving Team | 2026-10-30 | Loại bỏ thời gian chờ snapshot restore và GPU warmup |
| 3 | Tối ưu hóa health-checker probe: chuyển sang interval 3s với adaptive threshold | Monitoring SRE | 2026-10-20 | Giảm detection floor từ 15s xuống 9s (tiết kiệm ~6s RTO) |

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?
   - Cấu hình là `5.0s × 3 = 15.0s`.
   - Thời gian phát hiện thực tế là `15.1s`, chiếm **89.3%** tổng RTO đo được (`15.1s / 16.9s`).
2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?
   - Nếu hạ interval xuống 1s (với threshold=3), detection floor giảm từ 15s xuống 3s, giúp giảm được **12 giây** RTO.
   - Cái giá phải trả: Hệ thống sẽ cực kỳ nhạy cảm với các biến động mạng tạm thời (transient network jitter, temporary packet drop, GC pause ngắn). Bất kỳ sự cố vi mô nào kéo dài quá 3 giây cũng sẽ kích hoạt quy trình failover tốn kém, dẫn đến hiện tượng flapping (chuyển đổi qua lại liên tục giữa 2 region), làm gián đoạn phiên người dùng và gây nguy cơ split-brain (§4 Anti-Patterns).
3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?
   - `docs_lost` (trong drill này là 1 tài liệu, tương ứng 2.0s dữ liệu) là lượng dữ liệu giao dịch phát sinh trong khoảng thời gian giữa lần snapshot cuối cùng và thời điểm xảy ra sự cố.
   - Nếu Region A mất vĩnh viễn, khách hàng sẽ mất hoàn toàn các giao dịch hoặc tài liệu đã gửi trong khoảng 2 giây đó. Khách hàng sẽ phải nhập lại dữ liệu, hoặc hệ thống cần cơ chế replay event từ message log (Kafka / Event Bus) để tái tạo lại trạng thái dữ liệu đã mất.
