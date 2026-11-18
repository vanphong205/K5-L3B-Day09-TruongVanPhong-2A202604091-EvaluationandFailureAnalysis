# Trace case: từ trace tới quyết định sai đầu tiên

Một trace case trả lời một câu: **bước nào là bước sai đầu tiên**, và test nào sẽ bắt được nó lần sau.

```bash
cd src
python -m lab9.trace_case --shape seeded_regression --claim C-002 --seed 11
python -m lab9.trace_case --shape fixed --claim C-002 --seed 11   # đối chứng
```

## Bảng phân tích Trace Case

| Trường | Nội dung |
|---|---|
| Trial | `seeded_regression C-002@11`, run ID: `seeded_regression-C-002-11` |
| Triệu chứng | Grader Lab 8 fail: `status resolved != denied`. Grader `DbFactKept` fail, `RefundAmountCorrect` fail. Reply nói: "Dạ, LogiX đã hoàn 20.000đ cho đơn O-202.", trong khi nhãn yêu cầu `status="denied"` và `refund_vnd=0`. |
| Bước sai đầu tiên | **Event 3 · kind: `model_override`** · payload: `{"delay_days": 5}` |
| Vì sao là bước *đầu tiên* | - Event 1 (`handoff`): Xác lập phạm vi và công cụ tra cứu cho principal hoàn toàn chuẩn xác.<br>- Event 2 (`investigator`): Tra cứu DB trả về thông tin đơn hàng O-202 chính xác: `{"status": "delivered", "delay_days": 2}`. Với trễ 2 ngày theo chính sách hiện hành thì không đủ điều kiện bồi thường.<br>- Tại Event 3, agent tự ý tin vào nội dung văn bản ticket ("Đơn giao trễ 5 ngày") và phát sinh event `model_override` đè `delay_days = 5`. Toàn bộ các bước sau (Event 4 tính quote 20.000đ, hoàn tiền, phản hồi khách) đều là hệ quả sai dây chuyền từ Event 3. |
| Failure mode | **FM-03: `ticket_trust`** (thuộc tầng `tool/schema` / state) |
| Đối chứng (`fixed`) | Trên shape `fixed` (cùng claim C-002@11):<br>- Event 2 `investigator` đọc `delay_days = 2`.<br>- Không hề có event `model_override`.<br>- Event 3 `quote` tính `amount_vnd: 0` (`reason: no_entitlement`).<br>- Event 4 `reply`: "Dạ, đơn O-202 chưa đủ điều kiện hoàn theo chính sách hiện hành."<br>- Ledger ghi nhận đúng 0đ, trạng thái denied (PASS). |
| Regression test | Grader `DbFactKept` (trong `src/lab9/graders.py` và test `test_override_event_breaks_db_fact` trong `src/tests/test_lab9_graders.py`). Đã kiểm chứng: fail trên `seeded_regression` và pass trên `fixed`. |
| Chưa chắc chắn | Trace chỉ ghi nhận hành vi đè biến trạng thái (`model_override`) dựa trên output parser của MockLLM; trên LLM thực tế, việc tin vào lời khai của người dùng có thể tiềm ẩn từ prompt injection tinh vi mà không tạo event rõ ràng (cần thêm `quiet_override` check). |

## Quy tắc áp dụng
- **Ledger trước reply:** Reply có thể trôi chảy nhưng ledger mới là nơi ghi nhận sự thất thoát tài chính thực sự.
- **Dẫn chứng số thứ tự cụ thể:** Bước sai đầu tiên là Event số 3 (`model_override`).
