from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "assert_junit_no_skips.py"


def _run(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    report = tmp_path / "report.xml"
    report.write_text(body)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(report)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_accepts_report_without_skips(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        (
            '<testsuites><testsuite><testcase classname="native" name="loads" />'
            "</testsuite></testsuites>"
        ),
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_rejects_report_with_skip_and_prints_reason(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        (
            '<testsuites><testsuite><testcase classname="native" name="loads">'
            '<skipped message="exact binary unavailable" />'
            "</testcase></testsuite></testsuites>"
        ),
    )

    assert result.returncode == 1
    assert "native::loads" in result.stdout
    assert "exact binary unavailable" in result.stdout
