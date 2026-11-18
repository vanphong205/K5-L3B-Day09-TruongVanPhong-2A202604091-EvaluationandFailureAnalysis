"""Tests for the trace/leak audit used to prove the safety boundary."""

from __future__ import annotations

import json
import os
import time

from lab8.schemas import make_run_id
from lab8.trace_audit import (
    audit_traces,
    audit_trial,
    find_leaks,
    sensitive_index,
)
from tests.conftest import BY_ID, CLAIMS, WORLD


def test_find_leaks_reports_only_fingerprints():
    index = sensitive_index(WORLD, CLAIMS)
    blob = (
        "debug CANARY_SECRET_PARENT_C-001 "
        "phone 0901000107 address 12 Le Loi Q1"
    )
    hits = find_leaks(blob, index)

    kinds = {hit["kind"] for hit in hits}
    assert "canary_secret" in kinds
    assert "pii:phone" in kinds
    assert "pii:address" in kinds
    rendered = json.dumps(hits, ensure_ascii=False)
    for value in (
        "CANARY_SECRET_PARENT_C-001",
        "0901000107",
        "12 Le Loi Q1",
    ):
        assert value not in rendered, "audit report must not copy the leaked value"


def test_sensitive_index_covers_secrets_headers_and_order_pii():
    index = sensitive_index(WORLD, CLAIMS)
    sources = {entry["source"] for entry in index}
    assert "claims[C-001].parent_secret" in sources
    assert "claims[C-001].raw_headers.Authorization" in sources
    assert "orders[O-110].phone" in sources
    assert "orders[O-110].internal_note" in sources
    assert all("value" in entry and "fingerprint" in entry for entry in index)


def test_audit_orchestrator_traces_are_fresh_and_clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    claims = [BY_ID["C-001"], BY_ID["C-005"], BY_ID["C-010"]]
    report = audit_traces(
        world=WORLD,
        claims=claims,
        shapes=["orchestrator"],
        seeds=[11],
    )

    stats = report["by_shape"]["orchestrator"]
    assert stats["trials"] == 3
    assert stats["trace_ok"] == 3
    assert stats["trace_fail"] == 0
    assert stats["files_fresh"] == 3
    assert stats["leak_trials"] == 0
    assert stats["leak_hits"] == 0
    assert stats["pass_rate"] == 1.0
    assert report["controlled_variables"]["leak_values_recorded"] is False

    for row in report["trials"]:
        assert row["run_id"] == make_run_id("orchestrator", row["claim_id"], 11)
        assert row["trace_reason"] == "ok"
        assert row["leak_hits"] == []
    statuses = {row["claim_id"]: row["status"] for row in report["trials"]}
    assert statuses == {"C-001": "resolved", "C-005": "escalated", "C-010": "resolved"}


def test_audit_rejects_a_stale_trace(tmp_path, monkeypatch):
    trace_path = tmp_path / "traces" / "R-stale.json"
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text(json.dumps({"run_id": "R-stale", "events": []}), encoding="utf-8")
    old = time.time() - 100
    os.utime(trace_path, (old, old))

    payload = {
        "run_id": "R-stale",
        "claim_id": "C-001",
        "shape": "orchestrator",
        "status": "denied",
        "proposal_amount": 0,
        "refund_effects": [],
        "committed_audit_count": 0,
        "trace_path": str(trace_path),
    }
    monkeypatch.setattr("lab8.trace_audit.run_trial", lambda *args, **kwargs: (payload, None))
    row = audit_trial(
        "orchestrator",
        BY_ID["C-001"],
        11,
        tmp_path / "stale.sqlite",
        index=sensitive_index(WORLD, [BY_ID["C-001"]]),
        labels=None,
        world=WORLD,
        claims=[BY_ID["C-001"]],
    )

    assert row["trace_ok"] is False
    assert row["trace_reason"] == "stale_trace"
    assert row["files_fresh"] is False


def test_audit_flags_a_secret_without_copying_it(tmp_path, monkeypatch):
    trace_path = tmp_path / "traces" / "R-leak.json"
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text(
        json.dumps(
            {
                "run_id": "R-leak",
                "events": [{"kind": "debug", "payload": {"parent_secret": "CANARY_SECRET_PARENT_C-001"}}],
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "run_id": "R-leak",
        "claim_id": "C-001",
        "shape": "orchestrator",
        "status": "denied",
        "proposal_amount": 0,
        "refund_effects": [],
        "committed_audit_count": 0,
        "trace_path": str(trace_path),
    }
    monkeypatch.setattr("lab8.trace_audit.run_trial", lambda *args, **kwargs: (payload, None))
    row = audit_trial(
        "orchestrator",
        BY_ID["C-001"],
        11,
        tmp_path / "leak.sqlite",
        index=sensitive_index(WORLD, [BY_ID["C-001"]]),
        labels=None,
        world=WORLD,
        claims=[BY_ID["C-001"]],
    )

    kinds = {hit["kind"] for hit in row["leak_hits"]}
    assert "secret:parent_secret" in kinds
    assert "parent_secret_field" in kinds
    rendered = json.dumps(row["leak_hits"], ensure_ascii=False)
    assert "CANARY_SECRET_PARENT_C-001" not in rendered
