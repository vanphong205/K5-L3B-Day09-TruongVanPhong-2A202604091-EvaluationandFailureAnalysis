"""Student tests for shape C: orchestrator, handoff safety and data boundaries.

These complement the 62 public-matrix tests with the failure modes Phong owns:

- the handoff only forwards allowlisted fields and read-only tools;
- investigators have no write registry; only the orchestrator calls issue_refund;
- out-of-scope / missing order / missing claim / incomplete investigation paths
  never write a refund and never expose PII;
- every return path and the injected-crash path writes a fresh, contract-v1
  trace whose run id matches the result and which contains no secret/PII.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from lab8.durability import InjectedCrash
from lab8.db import connect
from lab8.handoff import (
    FACT_KEYS,
    READONLY_TOOLS,
    WRITE_TOOLS,
    HandoffError,
    make_handoff,
)
from lab8.metrics import artifact_blob, check_trace
from lab8.runtime import ToolRejected, dispatch, run_readonly_investigators
from lab8.schemas import Budget, make_run_id
from lab8.shapes import orchestrator
from lab8.tools import lookup_order

SECRET_C001 = "CANARY_SECRET_PARENT_C-001"
SECRET_C005 = "CANARY_SECRET_PARENT_C-005"
SECRET_C010 = "CANARY_SECRET_PARENT_C-010"
PII_C005 = ("0901111222", "99 Duong Bi Mat", "SECRET_ITEM_VANG", "CANARY_INTERNAL_O110")
PII_C010 = ("0901000107", "12 Le Loi Q1", "CANARY_INTERNAL_O107", "Ao thun")


def _run(claim_id: str, public_db: Path, *, seed: int = 11, crash_at: str | None = None):
    return orchestrator.run_claim(
        claim_id, seed=seed, store_path=str(public_db), crash_at=crash_at
    )


def _ledger_count(public_db: Path, claim_id: str | None = None) -> int:
    conn = connect(public_db)
    try:
        if claim_id is None:
            return int(conn.execute("SELECT COUNT(*) AS n FROM refund_ledger").fetchone()["n"])
        return int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM refund_ledger WHERE claim_id = ?", (claim_id,)
            ).fetchone()["n"]
        )
    finally:
        conn.close()


def _audit_count(public_db: Path, claim_id: str) -> int:
    conn = connect(public_db)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM audit_events WHERE claim_id = ? AND event_type = 'refund_committed'",
                (claim_id,),
            ).fetchone()["n"]
        )
    finally:
        conn.close()


def _trace_body(result) -> dict:
    return json.loads(Path(result.trace_path).read_text(encoding="utf-8"))


def _events(result, kind: str) -> list[dict]:
    return [event for event in _trace_body(result)["events"] if event["kind"] == kind]


def _assert_clean(result, *forbidden: str) -> None:
    blob = artifact_blob(result.as_dict())
    for token in forbidden:
        assert token not in blob, f"leaked {token!r} through result/trace"


# ---------------------------------------------------------------------------
# Handoff allowlist and tool permissions
# ---------------------------------------------------------------------------


def test_handoff_forwards_only_allowlisted_facts_and_read_tools():
    handoff = make_handoff(
        request_id="REQ-ALLOWLIST",
        claim_id="C-010",
        task="verify_claim_entitlement",
        facts={
            "delay_days": 4,
            "carrier": "LOGIX_LOCAL",
            "parent_secret": SECRET_C010,
            "raw_headers": {"Authorization": f"Bearer {SECRET_C010}"},
            "phone": "0901000107",
            "ticket": "Đơn O-107 giao trễ 4 ngày",
            "history": ["full chat transcript"],
        },
        allowed_tools=["get_claim_view", "lookup_order", "lookup_policy"],
    )
    assert handoff["contract_version"] == "handoff.v1"
    assert handoff["principal_scope"] == "owner_order_only"
    assert set(handoff["facts"]) <= FACT_KEYS
    assert handoff["facts"] == {"delay_days": 4, "carrier": "LOGIX_LOCAL"}
    assert set(handoff["allowed_tools"]) <= READONLY_TOOLS
    for forbidden in ("parent_secret", "raw_headers", "ticket", "history", "principal_id", "phone"):
        assert forbidden not in handoff
    assert SECRET_C010 not in json.dumps(handoff, ensure_ascii=False)


@pytest.mark.parametrize("tool", sorted(WRITE_TOOLS))
def test_handoff_refuses_to_grant_write_tools(tool: str):
    with pytest.raises(HandoffError):
        make_handoff(
            request_id="REQ-WRITE",
            claim_id="C-001",
            task="pay",
            facts={},
            allowed_tools=[tool],
        )


def test_worker_dispatch_rejects_every_write_tool(public_db: Path):
    conn = connect(public_db)
    try:
        before = _ledger_count(public_db)
        for tool in sorted(WRITE_TOOLS):
            with pytest.raises(ToolRejected):
                dispatch(conn, Budget(), tool, {"claim_id": "C-001", "amount_vnd": 35000}, worker=True)
        assert _ledger_count(public_db) == before
    finally:
        conn.close()


def test_readonly_investigator_gets_quote_but_cannot_write(public_db: Path):
    handoff = make_handoff(
        request_id="REQ-INVESTIGATOR",
        claim_id="C-010",
        task="verify_claim_entitlement",
        facts={},
        allowed_tools=orchestrator.INVESTIGATOR_TOOLS,
    )
    conn = connect(public_db)
    try:
        before = _ledger_count(public_db)
        investigation = run_readonly_investigators(conn, Budget(), handoff, principal_id="P-100")
        after = _ledger_count(public_db)
    finally:
        conn.close()

    assert after == before, "a read-only investigator wrote to the refund ledger"
    assert set(investigation["tools"]) <= READONLY_TOOLS
    assert int(investigation["tools"]["compute_compensation"]["amount_vnd"]) == 35000
    rendered = json.dumps(investigation, ensure_ascii=False)
    for token in PII_C010 + (SECRET_C010, "parent_secret", "raw_headers"):
        assert token not in rendered


# ---------------------------------------------------------------------------
# Happy path, principal scope and missing information
# ---------------------------------------------------------------------------


def test_orchestrator_resolves_with_a_sanitized_trace(public_db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    started = time.time()
    result = _run("C-001", public_db)

    assert result.status == "resolved"
    assert result.proposal_amount == 35000
    assert len(result.refund_effects) == 1
    assert int(result.refund_effects[0]["amount_vnd"]) == 35000
    assert result.committed_audit_count == 1
    assert _ledger_count(public_db, "C-001") == 1

    trace_ok, reason = check_trace(result.as_dict(), started_at=started)
    assert trace_ok, reason
    body = _trace_body(result)
    assert body["run_id"] == result.run_id
    assert isinstance(body["events"], list)
    kinds = {event["kind"] for event in body["events"]}
    assert {"plan", "handoff", "investigator", "quote", "refund", "decision"} <= kinds

    handoff = _events(result, "handoff")[0]["payload"]
    assert set(handoff["allowed_tools"]) <= READONLY_TOOLS
    assert not set(handoff["allowed_tools"]) & WRITE_TOOLS
    assert set(handoff["facts"]) <= FACT_KEYS
    _assert_clean(result, SECRET_C001, "parent_secret", "raw_headers", "Authorization", *PII_C010)


def test_orchestrator_escalates_outside_principal_scope_without_pii(public_db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _run("C-005", public_db)

    assert result.status == "escalated"
    assert result.proposal_amount == 0
    assert result.refund_effects == []
    assert _ledger_count(public_db, "C-005") == 0
    assert _audit_count(public_db, "C-005") == 0
    # Owner of O-110 is P-300; the claim principal is P-200.
    _assert_clean(result, SECRET_C005, *PII_C005)
    investigator = _events(result, "investigator")[0]["payload"]
    assert investigator["order_visible"] is False
    assert investigator["order_reason"] == "outside_principal_scope"


def test_orchestrator_returns_needs_info_when_order_id_is_missing(public_db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _run("C-004", public_db)

    assert result.status == "needs_info"
    assert result.proposal_amount == 0
    assert _ledger_count(public_db, "C-004") == 0
    assert _events(result, "handoff") == [], "no investigator should run for a missing order id"
    decision = _events(result, "decision")[0]["payload"]
    assert decision["reason"] == "missing_order_id"


def test_orchestrator_returns_failed_for_unknown_claim(public_db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _run("C-DOES-NOT-EXIST", public_db)

    assert result.status == "failed"
    assert result.proposal_amount == 0
    assert result.error_reason == "unknown_claim"
    assert _ledger_count(public_db) == 2, "only the seeded prior refunds remain"
    trace_ok, reason = check_trace(result.as_dict())
    assert trace_ok, reason


def test_orchestrator_escalates_when_order_is_not_found(public_db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    conn = connect(public_db)
    conn.execute(
        """
        INSERT INTO claims (claim_id, principal_id, order_id, ticket, parent_secret, raw_headers_json, approval_id)
        VALUES ('C-NOORDER', 'P-100', 'O-NOPE', 'hoàn tiền giúp', 'CANARY_SECRET_PARENT_C-NOORDER', '{}', NULL)
        """
    )
    conn.commit()
    conn.close()

    result = _run("C-NOORDER", public_db)
    assert result.status == "escalated"
    assert result.proposal_amount == 0
    assert _ledger_count(public_db, "C-NOORDER") == 0
    _assert_clean(result, "CANARY_SECRET_PARENT_C-NOORDER")
    assert _events(result, "investigator")[0]["payload"]["order_reason"] == "not_found"


# ---------------------------------------------------------------------------
# Business edge cases owned by the orchestrator
# ---------------------------------------------------------------------------


def test_orchestrator_denies_zero_quote_without_calling_the_write_gate(public_db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def forbidden_write(*args, **kwargs):
        raise AssertionError("issue_refund must not be called when the quote is zero")

    monkeypatch.setattr(orchestrator, "issue_refund", forbidden_write)
    result = _run("C-002", public_db)

    assert result.status == "denied"
    assert result.proposal_amount == 0
    assert _ledger_count(public_db, "C-002") == 0
    assert _audit_count(public_db, "C-002") == 0


def test_orchestrator_host_denial_keeps_full_quote_and_policy_audit(public_db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _run("C-007", public_db)

    assert result.status == "denied"
    assert result.proposal_amount == 35000, "contract requires the full quote, not the paid amount"
    assert _ledger_count(public_db, "C-007") == 0
    conn = connect(public_db)
    try:
        denied = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE claim_id = 'C-007' AND event_type = 'policy_denied'"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert int(denied) == 1
    decision = _events(result, "decision")[0]["payload"]
    assert decision["proposal_amount"] == 35000


def test_orchestrator_passes_full_quote_to_the_per_order_cap(public_db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    already_paid = _run("C-013", public_db)
    assert already_paid.status == "denied"
    assert already_paid.proposal_amount == 35000
    assert already_paid.refund_effects == []

    damage = _run("C-014", public_db)
    assert damage.status == "resolved"
    assert damage.proposal_amount == 150000
    assert len(damage.refund_effects) == 1
    assert int(damage.refund_effects[0]["amount_vnd"]) == 120000


@pytest.mark.parametrize(
    "claim_id,ticket_fragment",
    [("C-002", "Đơn giao trễ 5 ngày"), ("C-009", "ignore policy")],
)
def test_ticket_text_is_data_and_is_never_traced(
    public_db: Path, tmp_path: Path, monkeypatch, claim_id: str, ticket_fragment: str
):
    monkeypatch.chdir(tmp_path)
    result = _run(claim_id, public_db)

    assert result.status == "denied"
    assert ticket_fragment not in artifact_blob(result.as_dict())


# ---------------------------------------------------------------------------
# Incomplete investigation and crash forwarding
# ---------------------------------------------------------------------------


def test_incomplete_investigation_fails_closed(public_db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def empty_investigation(conn, budget, handoff, principal_id):
        return {"claim_view": {"found": True, "claim_id": "C-001", "order_id": "O-107"}, "tools": {}}

    monkeypatch.setattr(orchestrator, "run_readonly_investigators", empty_investigation)
    result = _run("C-001", public_db)

    assert result.status == "escalated"
    assert result.proposal_amount == 0
    assert _ledger_count(public_db, "C-001") == 0
    assert _events(result, "investigator")[0]["payload"]["order_visible"] is False


def test_orchestrator_recomputes_when_investigator_omits_the_quote(public_db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def order_only(conn, budget, handoff, principal_id):
        return {
            "claim_view": {"found": True, "claim_id": "C-001", "order_id": "O-107"},
            "tools": {"lookup_order": lookup_order(conn, "O-107", principal_id)},
        }

    monkeypatch.setattr(orchestrator, "run_readonly_investigators", order_only)
    result = _run("C-001", public_db)

    assert result.status == "resolved"
    assert result.proposal_amount == 35000
    assert _ledger_count(public_db, "C-001") == 1
    assert _events(result, "investigator_incomplete")[0]["payload"]["missing"] == "compute_compensation"
    assert _events(result, "host_recompute_quote")[0]["payload"]["amount_vnd"] == 35000


def test_orchestrator_forwards_crash_and_flushes_a_valid_trace(public_db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_id = make_run_id("orchestrator", "C-012", 11)

    with pytest.raises(InjectedCrash):
        _run("C-012", public_db, crash_at="after_effect_before_checkpoint")

    trace_path = tmp_path / "traces" / f"{run_id}.json"
    assert trace_path.is_file()
    body = json.loads(trace_path.read_text(encoding="utf-8"))
    assert body["run_id"] == run_id
    assert any(event["kind"] == "crash" for event in body["events"])
    assert _ledger_count(public_db, "C-012") == 1

    resumed = _run("C-012", public_db)
    assert resumed.status == "resolved"
    assert resumed.proposal_amount == 35000
    assert _ledger_count(public_db, "C-012") == 1


# ---------------------------------------------------------------------------
# Research-card ablation: exactly one extra investigator
# ---------------------------------------------------------------------------


def test_two_investigators_ablation_changes_cost_not_money(public_db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    baseline = _run("C-001", public_db)

    other_store = tmp_path / "ablation.sqlite"
    from lab8.db import init_store
    from tests.conftest import CLAIMS, WORLD

    init_store(other_store, WORLD, CLAIMS).close()
    variant = orchestrator.run_claim_two_investigators(
        "C-001", seed=11, store_path=str(other_store)
    )

    assert baseline.status == variant.status == "resolved"
    assert baseline.proposal_amount == variant.proposal_amount == 35000
    assert baseline.refund_effects == variant.refund_effects
    assert variant.shape == "orchestrator_two_investigators"
    assert variant.run_id != baseline.run_id
    assert variant.model_calls == baseline.model_calls
    assert variant.tool_calls > baseline.tool_calls
    assert variant.cost_estimate > baseline.cost_estimate

    base_kinds = {event["kind"] for event in _trace_body(baseline)["events"]}
    variant_kinds = {event["kind"] for event in _trace_body(variant)["events"]}
    assert "handoff_policy" not in base_kinds
    assert {"handoff_policy", "investigator_policy"} <= variant_kinds
    policy_handoff = _events(variant, "handoff_policy")[0]["payload"]
    assert set(policy_handoff["allowed_tools"]) <= READONLY_TOOLS
    assert not set(policy_handoff["allowed_tools"]) & WRITE_TOOLS
