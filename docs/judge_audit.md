# Judge audit

Chạy: `python -m lab9.judge --csv <file> --audit 5`

| Tiêu chí | n | κ | Precision | Recall | Đạt FLOORS (κ≥0.6, P≥0.8, R≥0.7)? | Quyết định |
|---|---|---|---|---|---|---|
| tone_ok | 100 | 0.757 | 0.857 | 0.800 | ĐẠT (0.757 ≥ 0.6, 0.857 ≥ 0.8, 0.8 ≥ 0.7) | DÙNG |
| helpful | 100 | 0.375 | 1.000 | 0.300 | KHÔNG ĐẠT (κ=0.375 < 0.6, R=0.3 < 0.7) | LOẠI |

## ≥5 case audit thủ công (từ judge_tone)
| # | Case | Judge nói | Người nói | Ai đúng? | Ghi chú |
|---|---|---|---|---|---|
| 1 | H-008: "Em đã hoàn tiền cho anh rồi nha, anh check giúp em." | PASS | FAIL | Người đúng | Giọng điệu suồng sã, xưng hô "anh - em" không phù hợp với chuẩn mực dịch vụ khách hàng chuyên nghiệp (B2C chuẩn). Rubric cần bổ sung quy định cấm đại từ xưng hô thân mật tự phát. |
| 2 | H-017: "Dạ, bạn ơi LogiX đã hoàn 25.000đ cho đơn O-606 rồi nha." | PASS | FAIL | Người đúng | Dùng từ ngữ thiếu trang trọng ("bạn ơi", "rồi nha") trong bối cảnh giải quyết khiếu nại tài chính. Rubric cần nhấn mạnh tính lịch thiệp, nghiêm túc. |
| 3 | H-029: "Dạ, vui lòng chờ. Hệ thống đang xử lý, không cần nhắn thêm." | PASS | FAIL | Người đúng | Thái độ cộc cằn, ra lệnh ("không cần nhắn thêm"), gây ức chế cho khách hàng. Rubric cần cấm ngôn từ gây cảm giác xua đuổi. |
| 4 | H-042: "Dạ, đơn O-808 đã hoàn 120.000đ. Tiền về trong 3–5 ngày ạ." | FAIL | PASS | Người đúng | Câu văn lịch sự, ngắn gọn và cung cấp rõ thời hạn nhận tiền (3-5 ngày). Judge bắt nhầm (FP) do hiểu lầm việc thiếu lời cảm ơn là vi phạm tone. |
| 5 | H-051: "Dạ, LogiX đã refund 90.000đ cho đơn O-202 ạ." | FAIL | PASS | Người đúng | Dùng chêm từ tiếng Anh ("refund") nhưng vẫn giữ thái độ lịch sự, khách hàng vẫn hiểu. Judge quá khắt khe với từ mượn thông dụng. |

## Kết luận
- **Tiêu chí bị loại:** `helpful` bị LOẠI hoàn toàn khỏi CI gate.
  - **Lý do:** Ma trận nhầm lẫn của `helpful` là `{'tp': 9, 'fp': 0, 'fn': 21, 'tn': 70}`. Recall chỉ đạt 0.300 (bỏ sót tới 21/30 ca lỗi, chiếm 70% số lỗi thực tế). Cohen's Kappa chỉ đạt 0.375 (dưới ngưỡng tối thiểu 0.600). Judge này bị thiên kiến "khen ngợi các câu trả lời khách sáo dài dòng", bỏ qua việc agent không thực sự giải quyết được vấn đề của khách hàng. Nếu dùng `helpful` làm gate, agent có thể qua mặt bằng cách sinh các câu xin lỗi rập khuôn.
- **Tiêu chí được chọn:** `tone_ok` đạt chuẩn để đưa vào quan sát/hỗ trợ đánh giá (κ = 0.757, P = 0.857, R = 0.800). Tỷ lệ lỗi quan sát được từ 0.28 sau khi hiệu chỉnh bằng Rogan-Gladen là 0.30.
- **Rủi ro còn lại:** Judge mới chỉ được kiểm định trên tập dữ liệu replay công khai; trong môi trường sản xuất, cần thu thập nhãn của con người trên tập dữ liệu held-out độc lập và liên tục audit khi có drift prompt.
