#!/usr/bin/env python3
"""Fail when a native-client pytest report contains skipped tests."""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path


def skipped_cases(report: Path) -> list[str]:
    root = ET.parse(report).getroot()
    skipped: list[str] = []
    for case in root.iter("testcase"):
        skip = case.find("skipped")
        if skip is None:
            continue
        identity = "::".join(
            part for part in (case.get("classname", ""), case.get("name", "")) if part
        )
        reason = skip.get("message") or (skip.text or "").strip() or "no reason recorded"
        skipped.append(f"{identity}: {reason}")
    return skipped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()

    skipped = skipped_cases(args.report)
    if not skipped:
        return 0

    print("Native-client gate skipped tests; refusing a partial pass:")
    for case in skipped:
        print(f"- {case}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
