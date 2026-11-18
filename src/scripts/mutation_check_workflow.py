"""Mutation check cho shape workflow: test riêng có bắt được các lỗi nghiệp vụ hay không.

Chạy trong src/:

    python scripts/mutation_check_workflow.py

Script chép src/ ra thư mục tạm, cố ý làm hỏng lab8/shapes/workflow.py trong bản chép
theo từng mutant, rồi chạy tests/test_workflow_rules.py trên bản chép đó. File trong repo
không bị sửa. Mutant "bị bắt" khi có ít nhất một test fail. Exit code 1 nếu bản gốc fail
hoặc có mutant sống sót.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
TARGET = Path("lab8/shapes/workflow.py")
TESTS = "tests/test_workflow_rules.py"

# (tên, đoạn gốc trong workflow.py, đoạn thay thế)
MUTANTS = [
    (
        "tu_tru_phan_da_hoan",
        "outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)",
        "from lab8.tools import lookup_refund_history as _h\n"
        "        outcome = issue_refund(conn, claim_id, proposal - _h(conn, claim['order_id'], "
        "claim['principal_id'])['refunded_vnd'], crash_at=crash_at)",
    ),
    (
        "tin_so_ngay_tre_trong_ticket",
        "    quote = compute_compensation(conn, order)",
        "    from lab8.mock_llm import parse_delay_days\n"
        "    _d = parse_delay_days(claim['ticket'])\n"
        "    if _d is not None:\n"
        "        order = {**order, 'delay_days': _d}\n"
        "    quote = compute_compensation(conn, order)",
    ),
    (
        "nuot_injected_crash",
        "        trace.write()\n        raise",
        "        return done('failed', proposal, 'crash')",
    ),
    (
        "ghi_claim_view_vao_trace",
        'trace.add("claim", {"found": True, "order_id": claim["order_id"]})',
        'trace.add("claim", dict(claim))',
    ),
    (
        "goi_host_khi_quote_bang_0",
        "    if proposal <= 0:\n        return done(\"denied\", 0, quote[\"reason\"])",
        "    if False:\n        return done(\"denied\", 0, quote[\"reason\"])",
    ),
]


def _copy_src(dest: Path) -> None:
    ignore = shutil.ignore_patterns(".venv", "traces", "reports", "__pycache__", "*.sqlite*", ".pytest_cache")
    shutil.copytree(SRC, dest, ignore=ignore)


def _run_tests(root: Path) -> tuple[int, int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", TESTS],
        cwd=root,
        capture_output=True,
        text=True,
    )
    last = (proc.stdout.strip().splitlines() or [""])[-1]
    failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", last)) else 0
    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", last)) else 0
    return failed, passed, last


def main() -> int:
    original = (SRC / TARGET).read_text(encoding="utf-8")
    ok = True
    with tempfile.TemporaryDirectory(prefix="lab8-mutation-") as tmp:
        root = Path(tmp) / "src"
        _copy_src(root)
        failed, passed, line = _run_tests(root)
        print(f"{'ban_goc':<32} {line}")
        if failed or not passed:
            print("bản gốc phải pass hết trước khi đo mutant")
            return 1
        for name, old, new in MUTANTS:
            if original.count(old) != 1:
                print(f"{name:<32} KHÔNG ÁP ĐƯỢC: workflow.py đã đổi, cập nhật đoạn gốc trong script")
                ok = False
                continue
            (root / TARGET).write_text(original.replace(old, new), encoding="utf-8")
            failed, _, line = _run_tests(root)
            verdict = "BẮT ĐƯỢC" if failed else "SỐNG SÓT"
            ok = ok and failed > 0
            print(f"{name:<32} {verdict:<9} {line}")
        (root / TARGET).write_text(original, encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
