"""Bounded single-agent ReAct; structured tools quote, the host alone writes.

MockLLM reads the ticket as untrusted text. Its output never supplies an amount,
approval, principal, or tool argument. Trace records decisions, not raw payloads.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.mock_llm import complete
from lab8.runtime import ToolRejected, run_react
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.trace import Trace


_OBSERVATIONS = {
    "get_claim_view": "claim",
    "lookup_order": "order",
    "compute_compensation": "quote",
}


def _next_action(claim_id: str, state: dict[str, Any]) -> dict[str, Any]:
    if "claim" not in state:
        return {"tool": "get_claim_view", "args": {"claim_id": claim_id}}
    claim = state["claim"]
    if not claim.get("found") or not claim.get("order_id"):
        return {"final": {}}
    if "order" not in state:
        return {"tool": "lookup_order", "args": {
            "order_id": claim["order_id"], "principal_id": claim["principal_id"],
        }}
    order_view = state["order"]
    if not order_view.get("visible"):
        return {"final": {}}
    if "quote" not in state:
        return {"tool": "compute_compensation", "args": {"order": order_view["order"]}}
    return {"final": {}}


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
    trace = Trace(make_run_id("single", claim_id, seed))
    state: dict[str, Any] = {}
    step = 0
    proposal = 0

    def decide(observation: dict[str, Any]) -> dict[str, Any]:
        nonlocal step
        last_tool = observation.get("last_tool")
        if last_tool:
            if last_tool not in _OBSERVATIONS:
                raise ToolRejected("unexpected observation")
            result = observation["last_result"]
            if not isinstance(result, dict):
                raise ValueError("invalid tool result")
            state[_OBSERVATIONS[last_tool]] = result
            summary: dict[str, Any] = {"tool": last_tool}
            if last_tool == "get_claim_view":
                summary.update(found=bool(result.get("found")), has_order=bool(result.get("order_id")))
            elif last_tool == "lookup_order":
                summary["visible"] = bool(result.get("visible"))
            else:
                amount = result.get("amount_vnd")
                if type(amount) is not int or amount < 0:
                    raise ValueError("invalid quote")
                summary["amount_vnd"] = amount
            trace.add("observation", summary)

        action = _next_action(claim_id, state)
        name = action.get("tool")
        if "final" not in action and name not in _OBSERVATIONS:
            raise ToolRejected("single agent only has scoped read tools")
        purpose = "draft" if "final" in action else "plan"
        prompt = json.dumps({"observed": sorted(state), "next": name or "final"})
        if last_tool == "get_claim_view" and name:
            purpose = "parse"
            prompt = str(state["claim"].get("ticket") or "")
        complete(budget, seed=seed, purpose=purpose, prompt=prompt)
        step += 1
        trace.add("react_action", {"step": step, "action": name or "final", "model_purpose": purpose})
        return action

    try:
        outcome = run_react(
            conn, budget, observation={}, decide=decide, max_steps=8, worker=True,
        )
        claim = state.get("claim") or {}
        order_view = state.get("order") or {}
        if outcome["stopped"] != "final":
            status, reason = "failed", "step_cap"
        elif not claim.get("found"):
            status, reason = "failed", "unknown_claim"
        elif not claim.get("order_id"):
            status, reason = "needs_info", "missing_order_id"
        elif not order_view.get("visible"):
            status, reason = "escalated", "order_not_visible"
        else:
            # Use the code tool's observation, never a number from the model's final.
            proposal = state["quote"]["amount_vnd"]
            if proposal <= 0:
                status, reason = "denied", "no_entitlement"
            else:
                trace.add("host_request", {"proposal_amount": proposal})
                trace.write()
                refund = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
                trace.add("host_result", {"ok": bool(refund["ok"]), "conflict": bool(refund["conflict"])})
                if refund["ok"]:
                    status, reason = "resolved", None
                elif refund["conflict"]:
                    status, reason = "failed", "amount_conflict"
                else:
                    status, reason = "denied", "host_denied"
    except InjectedCrash:
        raise
    except Exception as exc:
        conn.rollback()
        status, reason = "failed", "processing_error"
        # Exception messages can contain ticket text or a SQLite path.
        trace.add("error", {"type": type(exc).__name__})

    trace.add("decision", {"status": status, "proposal_amount": proposal})
    return finish(
        conn, shape="single", claim_id=claim_id, seed=seed, status=status,
        proposal_amount=proposal, budget=budget, trace=trace, error_reason=reason,
    )
