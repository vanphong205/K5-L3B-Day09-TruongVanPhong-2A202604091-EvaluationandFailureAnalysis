"""Shape C — orchestrator plus read-only investigators. Student work.

1. Build a handoff with `lab8.handoff.make_handoff`. Allowed tools are read-only.
   Never put parent_secret, raw headers, or the full claim row into facts.
2. `lab8.runtime.run_readonly_investigators` runs those tools.
3. The orchestrator quotes from the investigator's order view and calls
   `durability.issue_refund`. Investigators have no write registry.
   Pass the full quote; the host subtracts earlier refunds on the same order.
   `lookup_refund_history` is a read-only tool you may hand to an investigator.
4. Trace the sanitized handoff, not the parent context.

An LLM summary of the parent context is not a sanitizer. Use the allowlist.

Trace contract v1 (docs/contracts.md): return a trace_path to a JSON file written in this run,
{"run_id": <same run_id>, "events": [...]}, on every return path including early
ones. `lab8.trace.Trace` does this; your own tracer is fine if it matches.

Implementation notes (Phong — orchestrator & data safety)
--------------------------------------------------------
The orchestrator is the only actor allowed to call the host write gate. The
investigator receives a sanitized handoff (allowlist + read-only tools) and
never sees the parent context, the ticket, `parent_secret`, or raw headers.
Money is always computed by `lab8.tools.compute_compensation`; the model only
plans/drafts text through MockLLM.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.handoff import FACT_KEYS, READONLY_TOOLS, make_handoff
from lab8.mock_llm import complete
from lab8.runtime import run_readonly_investigators
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish, safe_dump
from lab8.tools import compute_compensation, get_claim_view, tool_cost
from lab8.trace import Trace

SHAPE = "orchestrator"
INVESTIGATOR_TASK = "verify_claim_entitlement"

# Handoff allowlist for the investigator: read tools only. The host write gate
# (`issue_refund`) and `append_audit` are deliberately absent.
INVESTIGATOR_TOOLS = [
    "get_claim_view",
    "lookup_order",
    "lookup_policy",
    "lookup_refund_history",
    "compute_compensation",
]

if not set(INVESTIGATOR_TOOLS) <= READONLY_TOOLS:  # pragma: no cover - import-time guard
    raise RuntimeError("orchestrator tried to hand a non-read-only tool to an investigator")


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


def run_claim_two_investigators(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    """Research-card ablation: exactly one extra investigator, nothing else.

    The extra investigator only reads the current policy. Claims, seeds, model,
    price list, evaluator and the host write gate stay identical to `run_claim`.
    Register it for a comparison with `--extra-shape`; it is not one of the four
    official shapes.
    """
    conn = connect(store_path)
    try:
        return _run(
            conn,
            claim_id,
            seed=seed,
            crash_at=crash_at,
            shape="orchestrator_two_investigators",
            extra_investigator=True,
        )
    finally:
        conn.close()


def _run(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    seed: int,
    crash_at: str | None,
    shape: str = SHAPE,
    extra_investigator: bool = False,
) -> RunResult:
    budget = Budget()
    run_id = make_run_id(shape, claim_id, seed)
    trace = Trace(run_id)

    # Step 1 — read the claim through the host allowlist (never SELECT *).
    budget.add_tool(tool_cost())
    claim = get_claim_view(conn, claim_id)
    if not claim.get("found"):
        trace.add("error", {"reason": "unknown_claim"})
        return finish(
            conn,
            shape=shape,
            claim_id=claim_id,
            seed=seed,
            status="failed",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason="unknown_claim",
        )

    # The model may plan wording, but the plan prompt never contains the ticket
    # or the parent context. This keeps the trace and the model call sanitized.
    complete(budget, seed=seed, purpose="plan", prompt="refund_claim_orchestration")
    trace.add(
        "plan",
        {
            "model": "MockLLM",
            "task": INVESTIGATOR_TASK,
            "allowed_tools": list(INVESTIGATOR_TOOLS),
            "principal_scope": "owner_order_only",
        },
    )

    if not claim.get("order_id"):
        trace.add(
            "decision",
            {"status": "needs_info", "reason": "missing_order_id", "proposal_amount": 0},
        )
        _draft(budget, seed=seed, status="needs_info", proposal=0)
        return finish(
            conn,
            shape=shape,
            claim_id=claim_id,
            seed=seed,
            status="needs_info",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason="missing_order_id",
        )

    # Step 2 — sanitized handoff. Only allowlisted facts are forwarded.
    handoff = make_handoff(
        request_id=f"REQ-{claim_id}",
        claim_id=claim_id,
        task=INVESTIGATOR_TASK,
        facts=_allowlisted_facts(claim),
        allowed_tools=INVESTIGATOR_TOOLS,
    )
    trace.add("handoff", safe_dump(handoff))

    # Step 3 — read-only investigators run against the handoff, in owner scope.
    investigation = run_readonly_investigators(
        conn,
        budget,
        handoff,
        principal_id=claim["principal_id"],
    )
    tools = investigation.get("tools") or {}
    order_view = tools.get("lookup_order") or {}
    trace.add("investigator", _summarize_investigation(investigation))

    # Research-card ablation only: one additional read-only investigator, same
    # handoff contract and same owner scope. It cannot write and it cannot
    # change the quote; the comparison isolates investigator count.
    if extra_investigator:
        policy_handoff = make_handoff(
            request_id=f"REQ-{claim_id}-policy",
            claim_id=claim_id,
            task="check_applicable_policy",
            facts={},
            allowed_tools=["lookup_policy"],
        )
        trace.add("handoff_policy", safe_dump(policy_handoff))
        policy_investigation = run_readonly_investigators(
            conn,
            budget,
            policy_handoff,
            principal_id=claim["principal_id"],
        )
        policy_view = (policy_investigation.get("tools") or {}).get("lookup_policy") or {}
        trace.add(
            "investigator_policy",
            {"policy_count": len(policy_view.get("policies") or [])},
        )

    if not order_view.get("visible"):
        reason = str(order_view.get("reason") or "order_not_visible")
        status = "needs_info" if reason == "missing_order_id" else "escalated"
        trace.add(
            "decision",
            {"status": status, "reason": reason, "proposal_amount": 0},
        )
        _draft(budget, seed=seed, status=status, proposal=0)
        return finish(
            conn,
            shape=shape,
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason=reason,
        )

    order = dict(order_view["order"])
    quote_payload = tools.get("compute_compensation")

    # Step 4 — bounded fallback when the investigator returns an order but no
    # quote. Host code recomputes from the sanitized order view; no raw row is
    # read and no model decides the amount.
    if not isinstance(quote_payload, dict) or "amount_vnd" not in quote_payload:
        trace.add("investigator_incomplete", {"missing": "compute_compensation"})
        budget.add_tool(tool_cost())
        quote_payload = compute_compensation(conn, order)
        trace.add("host_recompute_quote", _quote_event(quote_payload))

    proposal = int(quote_payload.get("amount_vnd") or 0)
    trace.add("quote", _quote_event(quote_payload))

    if proposal <= 0:
        trace.add(
            "decision",
            {
                "status": "denied",
                "reason": quote_payload.get("reason"),
                "proposal_amount": 0,
            },
        )
        _draft(budget, seed=seed, status="denied", proposal=0)
        return finish(
            conn,
            shape=shape,
            claim_id=claim_id,
            seed=seed,
            status="denied",
            proposal_amount=0,
            budget=budget,
            trace=trace,
            error_reason=quote_payload.get("reason"),
        )

    # Step 5 — only the orchestrator (host-side code) may call the write gate.
    # The FULL quote is forwarded; the host applies approval, per-order cap and
    # idempotency inside one transaction.
    try:
        outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
    except InjectedCrash:
        # Flush the trace before letting the injected crash propagate. Catching
        # it here would hide the crash window the durability test needs.
        trace.add("crash", {"at": crash_at})
        trace.write()
        raise

    if not outcome.get("ok"):
        status = "failed" if outcome.get("conflict") else "denied"
        trace.add(
            "decision",
            {
                "status": status,
                "reason": outcome.get("reason"),
                "proposal_amount": proposal,
            },
        )
        _draft(budget, seed=seed, status=status, proposal=proposal)
        return finish(
            conn,
            shape=shape,
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=proposal,
            budget=budget,
            trace=trace,
            error_reason=outcome.get("reason"),
        )

    effect = outcome.get("effect") or {}
    trace.add(
        "refund",
        {
            "ok": True,
            "reason": outcome.get("reason"),
            "amount_vnd": int(effect.get("amount_vnd") or 0),
        },
    )
    trace.add(
        "decision",
        {"status": "resolved", "reason": outcome.get("reason"), "proposal_amount": proposal},
    )
    _draft(budget, seed=seed, status="resolved", proposal=proposal)
    return finish(
        conn,
        shape=shape,
        claim_id=claim_id,
        seed=seed,
        status="resolved",
        proposal_amount=proposal,
        budget=budget,
        trace=trace,
        error_reason=None,
    )


def _allowlisted_facts(claim: dict[str, Any]) -> dict[str, Any]:
    """Copy only handoff facts that are already on the allowlist.

    `get_claim_view` returns claim_id/order_id/ticket as well; ticket is user
    text and is never forwarded to a worker. `make_handoff` applies the same
    filter again, so this is defence in depth rather than the only guard.
    """
    return {key: claim[key] for key in FACT_KEYS if key in claim}


def _summarize_investigation(investigation: dict[str, Any]) -> dict[str, Any]:
    """Trace-safe summary: sanitized views only, never the raw claim row."""
    tools = investigation.get("tools") or {}
    order_view = tools.get("lookup_order") or {}
    quote_payload = tools.get("compute_compensation") or {}
    history = tools.get("lookup_refund_history") or {}
    policy = tools.get("lookup_policy") or {}
    summary: dict[str, Any] = {
        "claim_view": investigation.get("claim_view") or {},
        "order_visible": bool(order_view.get("visible")),
        "order_reason": order_view.get("reason"),
    }
    if order_view.get("visible"):
        # lookup_order already returns only ORDER_FIELDS (no phone/address/note/item).
        summary["order"] = order_view.get("order")
    if isinstance(quote_payload, dict) and "amount_vnd" in quote_payload:
        summary["quote"] = _quote_event(quote_payload)
    if history.get("visible"):
        summary["refund_history"] = {
            "refund_count": int(history.get("refund_count") or 0),
            "refunded_vnd": int(history.get("refunded_vnd") or 0),
        }
    if policy.get("policies") is not None:
        summary["policy_count"] = len(policy.get("policies") or [])
    return summary


def _quote_event(quote_payload: dict[str, Any]) -> dict[str, Any]:
    """Quote fields safe for the trace (no ticket, no order PII)."""
    return {
        "amount_vnd": int(quote_payload.get("amount_vnd") or 0),
        "reason": quote_payload.get("reason"),
        "clause_ids": list(quote_payload.get("clause_ids") or []),
        "parts": dict(quote_payload.get("parts") or {}),
    }


def _draft(budget: Budget, *, seed: int, status: str, proposal: int) -> None:
    """Draft reply text with MockLLM. Amount is never chosen by the model."""
    complete(budget, seed=seed, purpose="draft", prompt=f"status={status};proposal={int(proposal)}")
