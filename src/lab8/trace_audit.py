"""python -m lab8.trace_audit — audit trace contract v1 and data leakage.

Purpose (Phong — orchestrator & data safety): after the three student shapes
exist, prove for every run that

1. `RunResult.trace_path` points to a JSON file written during *this* run;
2. the file has `run_id` equal to `RunResult.run_id` and an `events` list;
3. every sibling file named `<run_id>*` is also freshly written;
4. the result plus all those files contain no parent secret, raw header, or
   order PII.

Leaks are recorded by fingerprint, never by value. The audit report must not
become another copy of the data it is supposed to protect.

Run from `src/`:

    python -m lab8.trace_audit --shapes workflow,single,orchestrator \
        --cases data/public_claims.jsonl --world data/public_world_state.json \
        --seeds 11,22,33,44,55 --out reports/trace_audit.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from lab8.benchmark import run_trial
from lab8.config_loader import repo_path
from lab8.console import utf8_stdio
from lab8.db import connect, init_store, load_json, load_jsonl
from lab8.metrics import artifact_blob, check_trace, trace_files, trial_pass

AUDIT_VERSION = "trace-audit.v1"
DEFAULT_SHAPES = ("workflow", "single", "orchestrator")
MTIME_TOLERANCE_S = 1.0

# Generic canaries that must never appear in a result or trace.
GENERIC_MARKERS = (
    ("CANARY_SECRET_", "canary_secret"),
    ("CANARY_INTERNAL_", "canary_internal"),
    ("parent_secret", "parent_secret_field"),
    ("raw_headers", "raw_headers_field"),
    ("Authorization: Bearer", "authorization_header"),
)
PHONE_PATTERN = re.compile(r"(?<!\d)0\d{9}(?!\d)")


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def sensitive_index(world: dict[str, Any], claims: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Values that must not leave the parent process, with a non-secret label.

    The returned list deliberately stores only fingerprints. Callers can check
    whether a blob contains the value, but the index cannot be used to recover it.
    """
    entries: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(value: Any, kind: str, source: str, *, min_length: int = 1) -> None:
        text = str(value) if value is not None else ""
        if len(text) < min_length or text in seen:
            return
        seen.add(text)
        entries.append(
            {"value": text, "kind": kind, "source": source, "fingerprint": _fingerprint(text)}
        )

    for order in world.get("orders") or []:
        order_id = str(order.get("order_id") or "?")
        add(order.get("phone"), "pii:phone", f"orders[{order_id}].phone", min_length=6)
        add(order.get("address"), "pii:address", f"orders[{order_id}].address", min_length=6)
        add(
            order.get("internal_note"),
            "pii:internal_note",
            f"orders[{order_id}].internal_note",
            min_length=6,
        )
        add(order.get("item_name"), "pii:item_name", f"orders[{order_id}].item_name", min_length=4)

    for claim in claims:
        claim_id = str(claim.get("claim_id") or "?")
        add(
            claim.get("parent_secret"),
            "secret:parent_secret",
            f"claims[{claim_id}].parent_secret",
            min_length=8,
        )
        for header, value in (claim.get("raw_headers") or {}).items():
            add(value, "secret:header", f"claims[{claim_id}].raw_headers.{header}", min_length=8)
        # A full ticket is user data; long tickets are a useful leak canary.
        add(claim.get("ticket"), "claim:ticket", f"claims[{claim_id}].ticket", min_length=16)
    return entries


def find_leaks(blob: str, index: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return leak metadata only (kind, source, fingerprint) — never the value."""
    hits: list[dict[str, str]] = []
    seen: set[str] = set()

    for entry in index:
        if entry["value"] in blob:
            fingerprint = entry["fingerprint"]
            if fingerprint not in seen:
                seen.add(fingerprint)
                hits.append(
                    {"kind": entry["kind"], "source": entry["source"], "fingerprint": fingerprint}
                )

    for marker, kind in GENERIC_MARKERS:
        if marker in blob:
            fingerprint = _fingerprint(marker)
            if fingerprint not in seen:
                seen.add(fingerprint)
                hits.append({"kind": kind, "source": "generic_marker", "fingerprint": fingerprint})

    for match in PHONE_PATTERN.finditer(blob):
        fingerprint = _fingerprint(match.group(0))
        if fingerprint not in seen:
            seen.add(fingerprint)
            hits.append(
                {"kind": "pii:phone_pattern", "source": "regex", "fingerprint": fingerprint}
            )
    return hits


def _freshness(payload: dict[str, Any], started_at: float) -> tuple[bool, list[str]]:
    stale: list[str] = []
    for path in trace_files(payload):
        try:
            written_at = path.stat().st_mtime
        except OSError:
            stale.append(path.name)
            continue
        if written_at < started_at - MTIME_TOLERANCE_S:
            stale.append(path.name)
    return not stale, stale


def audit_trial(
    shape: str,
    claim: dict[str, Any],
    seed: int,
    store: Path,
    *,
    index: list[dict[str, str]],
    labels: dict[str, dict[str, Any]] | None,
    world: dict[str, Any],
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    claim_id = str(claim["claim_id"])
    init_store(store, world, claims).close()
    started_at = time.time()
    payload, error = run_trial(shape, claim_id, seed, store)

    row: dict[str, Any] = {
        "shape": shape,
        "claim_id": claim_id,
        "seed": seed,
        "run_id": None,
        "status": "error",
        "trace_ok": False,
        "trace_reason": error,
        "files_fresh": False,
        "stale_files": [],
        "leak_hits": [],
        "pass": None,
        "pass_reason": None,
    }
    if error is not None:
        return row

    row["run_id"] = payload.get("run_id")
    row["status"] = payload.get("status")
    trace_ok, trace_reason = check_trace(payload, started_at=started_at)
    fresh, stale = _freshness(payload, started_at)
    leaks = find_leaks(artifact_blob(payload), index)
    row.update(
        trace_ok=trace_ok,
        trace_reason=trace_reason,
        files_fresh=fresh,
        stale_files=stale,
        leak_hits=leaks,
    )

    expected = (labels or {}).get(claim_id) if labels is not None else claim.get("expected")
    if expected is not None:
        conn = connect(store)
        try:
            passed, reason = trial_pass(payload, expected, conn, started_at=started_at)
        except Exception as exc:  # noqa: BLE001 — malformed result must not stop the audit
            passed, reason = False, f"invalid_result:{type(exc).__name__}"
        finally:
            conn.close()
        row["pass"] = bool(passed)
        row["pass_reason"] = reason
    return row


def _summarize(trials: list[dict[str, Any]]) -> dict[str, Any]:
    by_shape: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in trials:
        by_shape[row["shape"]].append(row)

    summary: dict[str, Any] = {}
    for shape, rows in by_shape.items():
        statuses: dict[str, int] = defaultdict(int)
        for row in rows:
            statuses[str(row["status"])] += 1
        leak_hits = sum(len(row["leak_hits"]) for row in rows)
        leak_trials = sum(1 for row in rows if row["leak_hits"])
        graded = [row for row in rows if row["pass"] is not None]
        passed = sum(1 for row in graded if row["pass"])
        summary[shape] = {
            "trials": len(rows),
            "trace_ok": sum(1 for row in rows if row["trace_ok"]),
            "trace_fail": sum(1 for row in rows if not row["trace_ok"]),
            "files_fresh": sum(1 for row in rows if row["files_fresh"]),
            "stale_trials": sum(1 for row in rows if not row["files_fresh"]),
            "leak_trials": leak_trials,
            "leak_hits": leak_hits,
            "status_counts": dict(statuses),
            "graded_trials": len(graded),
            "passed_trials": passed,
            "pass_rate": (passed / len(graded)) if graded else None,
        }
    return summary


def audit_traces(
    *,
    world: dict[str, Any],
    claims: list[dict[str, Any]],
    shapes: list[str],
    seeds: list[int],
    labels: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    index = sensitive_index(world, claims)
    trials: list[dict[str, Any]] = []
    # On Windows a store a shape forgot to close can still be locked during cleanup.
    with tempfile.TemporaryDirectory(prefix="lab8-audit-", ignore_cleanup_errors=True) as tmp:
        for shape in shapes:
            for claim in claims:
                for seed in seeds:
                    store = Path(tmp) / f"{shape}-{claim['claim_id']}-{seed}.sqlite"
                    trials.append(
                        audit_trial(
                            shape,
                            claim,
                            seed,
                            store,
                            index=index,
                            labels=labels,
                            world=world,
                            claims=claims,
                        )
                    )
    return {
        "audit_version": AUDIT_VERSION,
        "controlled_variables": {
            "shapes": list(shapes),
            "seeds": list(seeds),
            "claim_ids": [claim["claim_id"] for claim in claims],
            "model_backend": "MockLLM",
            "isolation": "fresh_sqlite_per_trial",
            "sensitive_index_entries": len(index),
            "leak_values_recorded": False,
        },
        "by_shape": _summarize(trials),
        "trials": trials,
    }


def render_table(report: dict[str, Any]) -> str:
    lines = [
        f"{'shape':<14} {'trials':>7} {'trace_ok':>9} {'fresh':>7} {'leak_trials':>12} {'pass':>8}",
        "-" * 64,
    ]
    for shape, stats in report["by_shape"].items():
        pass_rate = "-" if stats["pass_rate"] is None else f"{stats['pass_rate']:.3f}"
        lines.append(
            f"{shape:<14} {stats['trials']:>7} {stats['trace_ok']:>9} "
            f"{stats['files_fresh']:>7} {stats['leak_trials']:>12} {pass_rate:>8}"
        )
        problems = {
            key: value
            for key, value in stats["status_counts"].items()
            if key not in ("resolved", "denied", "needs_info", "escalated")
        }
        if problems:
            detail = ", ".join(f"{key} x{value}" for key, value in sorted(problems.items()))
            lines.append(f"{'':<14} status khác: {detail}")
    lines.append("Trace audit: run_id, freshness and leak scan are fail-closed.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(description="Audit trace contract v1 and data leakage")
    parser.add_argument(
        "--shapes",
        default=",".join(DEFAULT_SHAPES),
        help="comma-separated shapes (default: workflow,single,orchestrator)",
    )
    parser.add_argument("--cases", default=str(repo_path("data", "public_claims.jsonl")))
    parser.add_argument("--world", default=str(repo_path("data", "public_world_state.json")))
    parser.add_argument("--seeds", default="11,22,33,44,55")
    parser.add_argument("--labels", default=None, help="optional JSON map claim_id -> expected")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    shapes = [part.strip() for part in args.shapes.split(",") if part.strip()]
    seeds = [int(part.strip()) for part in args.seeds.split(",") if part.strip()]
    world = load_json(args.world)
    claims = load_jsonl(args.cases)
    labels = load_json(args.labels) if args.labels else None
    report = audit_traces(
        world=world,
        claims=claims,
        shapes=shapes,
        seeds=seeds,
        labels=labels,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render_table(report))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
