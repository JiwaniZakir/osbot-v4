"""Tests for TraceWriter atomic appends and malformed-line tolerance."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from osbot.state.traces import TraceWriter
from osbot.types import Correction, Trace

if TYPE_CHECKING:
    from pathlib import Path


def _trace(number: int = 1) -> Trace:
    return Trace(
        ts="2026-04-17T00:00:00Z",
        repo="owner/repo",
        issue_number=number,
        phase="contribute",
        outcome="submitted",
        pr_number=100 + number,
    )


def _correction() -> Correction:
    return Correction(ts="2026-04-17T00:00:00Z", type="alert", message="hi")


async def test_write_trace_appends_single_line(tmp_path: Path) -> None:
    writer = TraceWriter(traces_path=tmp_path / "t.jsonl", corrections_path=tmp_path / "c.jsonl")
    await writer.write_trace(_trace(1))
    await writer.write_trace(_trace(2))
    lines = (tmp_path / "t.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["issue_number"] == 1
    assert json.loads(lines[1])["issue_number"] == 2


async def test_write_trace_creates_parent_dir(tmp_path: Path) -> None:
    writer = TraceWriter(
        traces_path=tmp_path / "nested" / "dir" / "t.jsonl",
        corrections_path=tmp_path / "c.jsonl",
    )
    await writer.write_trace(_trace(1))
    assert (tmp_path / "nested" / "dir" / "t.jsonl").exists()


async def test_read_recent_traces_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    writer = TraceWriter(traces_path=path, corrections_path=tmp_path / "c.jsonl")
    await writer.write_trace(_trace(1))
    # Simulate a legacy corrupt line wedged between valid records.
    with path.open("a") as f:
        f.write("this is not json\n")
        f.write('{"ts":"x","partial":\n')
    await writer.write_trace(_trace(2))

    traces = await writer.read_recent_traces(100)
    assert [t.issue_number for t in traces] == [1, 2]


async def test_read_recent_traces_missing_file_returns_empty(tmp_path: Path) -> None:
    writer = TraceWriter(traces_path=tmp_path / "nope.jsonl", corrections_path=tmp_path / "c.jsonl")
    assert await writer.read_recent_traces(10) == []


async def test_write_correction_atomic(tmp_path: Path) -> None:
    writer = TraceWriter(traces_path=tmp_path / "t.jsonl", corrections_path=tmp_path / "c.jsonl")
    await writer.write_correction(_correction())
    lines = (tmp_path / "c.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["type"] == "alert"


async def test_read_recent_corrections_skips_malformed(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    writer = TraceWriter(traces_path=tmp_path / "t.jsonl", corrections_path=path)
    await writer.write_correction(_correction())
    with path.open("a") as f:
        f.write("garbage\n")
    await writer.write_correction(_correction())

    corrections = await writer.read_recent_corrections(100)
    assert len(corrections) == 2
