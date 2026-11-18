"""Shape A — deterministic workflow. Student work.

Signature (do not change):

    run_claim(claim_id, *, seed, store_path, crash_at=None) -> RunResult

Suggested steps. Money stays in code (`lab8.tools.compute_compensation`).

1. Open the store. Read the claim through `get_claim_view` (not SELECT *).
2. Missing order_id -> status `needs_info`. Do not write the ledger.
3. `lookup_order`. If not visible -> `escalated`. Do not copy phone, address, item, internal note.
4. Quote from the order row. Ignore delay numbers written in the ticket.
5. Quote amount 0 -> `denied`. Do not call issue_refund.
6. Quote amount > 0 -> `durability.issue_refund(conn, claim_id, <full quote>)`.
   Pass the FULL quote. The host checks the claim's own approval and subtracts
   what earlier claims already got for the same order (T13, T14). Do not
   subtract it yourself or the host subtracts twice.
   Auth failure or order already refunded -> `denied` (the host writes `policy_denied`).
   `proposal_amount` stays the full quote. Amount conflict -> `failed`.
7. Success -> `resolved`. One ledger row, one `refund_committed` audit.
8. Use MockLLM only to draft the reply text, if you want a model call at all.

The model must not choose the refund amount and must not commit the refund.

Trace contract v1 (docs/contracts.md): return a trace_path to a JSON file written in this run,
{"run_id": <same run_id>, "events": [...]}, on every return path including early
ones. `lab8.trace.Trace` does this; your own tracer is fine if it matches.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.mock_llm import complete
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish, safe_dump
from lab8.tools import compute_compensation, get_claim_view, lookup_order, tool_cost
from lab8.trace import Trace

SHAPE = "workflow"


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    conn = connect(store_path)
    try:
        return _run(conn, claim_id, seed=seed, crash_at=crash_at)
    finally:
        conn.close()


def _run(conn: sqlite3.Connection, claim_id: str, *, seed: int, crash_at: str | None) -> RunResult:
    budget = Budget()
    # Mỗi event trong trace ứng với một thao tác thật (tool, host, model): benchmark đếm
    # số event làm metric steps, nên không thêm event chỉ để ghi chép.
    # run_id đã mang shape, claim_id và seed.
    trace = Trace(make_run_id(SHAPE, claim_id, seed))

    def done(status: str, proposal: int, reason: str | None) -> RunResult:
        # Mọi nhánh return đều đi qua đây: soạn reply bằng MockLLM (chỉ chữ, không quyết tiền),
        # ghi event draft kèm quyết định, rồi finish() lấy refund_effects/audit từ ledger và ghi trace.
        # Prompt chỉ gồm status + số tiền: không đưa ticket hay order row vào model.
        complete(budget, seed=seed, purpose="draft", prompt=f"status={status} proposal={proposal}")
        trace.add("draft", {"status": status, "proposal_amount": int(proposal), "reason": reason})
        return finish(
            conn,
            shape=SHAPE,
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=proposal,
            budget=budget,
            trace=trace,
            error_reason=reason,
        )

    # Bước 1: đọc claim qua view allowlist (claim_id, principal_id, order_id, ticket).
    # Không SELECT * vì claim row có parent_secret và raw_headers.
    claim = get_claim_view(conn, claim_id)
    budget.add_tool(tool_cost())
    if not claim["found"]:
        trace.add("claim", {"found": False})
        return done("failed", 0, "unknown_claim")
    # Ticket không vào trace: đó là chữ khách viết, có thể chứa SĐT, địa chỉ hay chuỗi bất kỳ.
    trace.add("claim", {"found": True, "order_id": claim["order_id"]})

    # Bước 2: thiếu order_id -> needs_info. Không đoán mã đơn từ câu chữ trong ticket.
    if not claim["order_id"]:
        return done("needs_info", 0, "missing_order_id")

    # Bước 3: tra đơn trong phạm vi principal. Không thấy hoặc không phải chủ đơn -> escalated.
    looked = lookup_order(conn, claim["order_id"], claim["principal_id"])
    budget.add_tool(tool_cost())
    if not looked["visible"]:
        trace.add("order", {"visible": False, "reason": looked["reason"]})
        return done("escalated", 0, looked["reason"])
    order: dict[str, Any] = looked["order"]  # chỉ còn ORDER_FIELDS: không phone/address/note/item
    trace.add("order", {"visible": True, "order": safe_dump(order)})

    # Bước 4: quote từ order row + policy hiện hành. delay_days lấy từ DB, không từ ticket.
    # compensation.quote đã lấy max các clause đủ điều kiện (không cộng ship + damage).
    quote = compute_compensation(conn, order)
    budget.add_tool(tool_cost())
    proposal = int(quote["amount_vnd"])
    trace.add("quote", safe_dump(quote))

    # Bước 5: quote = 0 -> denied, không gọi host (nên không có audit policy_denied, đúng contract).
    if proposal <= 0:
        return done("denied", 0, quote["reason"])

    # Bước 6: host là người duy nhất ghi tiền. Truyền quote ĐẦY ĐỦ: host tự kiểm approval,
    # tự trừ phần các claim khác đã hoàn cho cùng đơn, tự chống ghi trùng.
    try:
        outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
    except InjectedCrash:
        # Ghi trace rồi ném tiếp: T12 cần thấy crash thật, lần chạy lại sẽ là idempotent replay.
        trace.add("crash", {"at": crash_at})
        trace.write()
        raise
    effect = outcome.get("effect") or {}
    trace.add(
        "host",
        {
            "ok": bool(outcome["ok"]),
            "reason": outcome["reason"],
            "conflict": bool(outcome["conflict"]),
            "amount_vnd": effect.get("amount_vnd"),
        },
    )
    if not outcome["ok"]:
        # Conflict = gọi lại với số khác số đã commit -> failed. Còn lại (approval hỏng,
        # approval của claim khác, đơn đã hoàn đủ) -> denied; proposal vẫn là quote đầy đủ.
        status = "failed" if outcome["conflict"] else "denied"
        return done(status, proposal, outcome["reason"])

    # Bước 7: thành công (kể cả idempotent_replay khi chạy lại) -> resolved.
    return done("resolved", proposal, None)
