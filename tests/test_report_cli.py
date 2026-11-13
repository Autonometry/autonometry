"""``autonometry report`` and the golden reports (definitions.md 11)."""

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from json_helpers import close
from report_helpers import (
    ACTOR_IDS,
    ANCHORS,
    GENERATED_AT,
    GOLDEN_RUNS,
    LOGS,
    METRICS_LOGS,
    REGISTRIES,
    REPORT,
    SEALED,
    VKEY,
    run_report,
    sealed_golden_files,
)

SECTION_IDS = (
    "verdict",
    "trust",
    "census",
    "exclusions",
    "metrics",
    "statements",
    "invariants",
    "sessions",
    "parameters",
    "provenance",
)


def _report(folder: Path, out: Path, *args: str) -> tuple[int, dict[str, Any], str, str]:
    """Run the report; return exit code, JSON, HTML and output (empty when nothing was written)."""
    code, output = run_report(folder, args, out)
    json_path, html_path = out / "report.json", out / "report.html"
    body = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else {}
    page = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
    return code, body, page, output


def _copy_sealed(target: Path) -> Path:
    shutil.copytree(SEALED, target)
    return target


@pytest.mark.parametrize("name", sorted(GOLDEN_RUNS))
def test_golden_report__defs_11(name: str, tmp_path: Path) -> None:
    """(a) the metrics logs unsealed: L1, unverified; (b) sealed with the test key, checkpoints
    and an anchor file: L2, verified. Both are incomplete (real unknown share 5.7 %): exit 1."""
    folder, args = GOLDEN_RUNS[name]
    code, body, _, _ = _report(folder, tmp_path, *args)
    expected = json.loads((REPORT / f"{name}.expected.json").read_text(encoding="utf-8"))
    assert close(expected, body) == []
    assert code == 1
    assert body["provenance"]["generated_at"] == GENERATED_AT


def test_golden_levels__defs_11_2(tmp_path: Path) -> None:
    unsealed = _report(METRICS_LOGS, tmp_path / "a", *GOLDEN_RUNS["unsealed"][1])[1]
    sealed = _report(SEALED, tmp_path / "b", *GOLDEN_RUNS["sealed"][1])[1]
    assert (unsealed["verdict"]["status"], unsealed["verdict"]["level"]) == (
        "not_verified",
        "L1",
    )
    assert (sealed["verdict"]["status"], sealed["verdict"]["level"]) == ("verified", "L2")
    assert sealed["verdict"]["reasons"] == ["incomplete"]
    assert sealed["invariants"]["chain"]["status"] == "pass"
    assert unsealed["invariants"]["chain"]["status"] == "not_checked"
    # Sealing changes nothing measured.
    assert close(unsealed["metrics"], sealed["metrics"]) == []


def test_sealed_golden_files_come_from_the_seed_key() -> None:
    """The committed sealed set is exactly what the fixed-seed key gives (tests/golden/report)."""
    for name, data in sealed_golden_files().items():
        assert (SEALED / name).read_bytes() == data, name


def test_edited_sealed_event_fails_and_stays_in_metrics__defs_11_2(tmp_path: Path) -> None:
    folder = _copy_sealed(tmp_path / "logs")
    log = folder / "s3.jsonl"
    lines = log.read_bytes().splitlines(keepends=True)
    lines[3] = lines[3].replace(b'"seq":3', b'"seq":3,"x_note":"edited"', 1)
    log.write_bytes(b"".join(lines))
    code, body, page, output = _report(folder, tmp_path / "out", *GOLDEN_RUNS["sealed"][1])
    assert code == 1
    assert body["verdict"]["status"] == "failed"
    assert "trust status failed" in body["verdict"]["reasons"]
    s3 = next(s for s in body["trust"]["sessions"] if s["session_id"] == "s3")
    assert s3["status"] == "failed"
    assert "hash_mismatch" in {i["code"] for i in s3["evidence"]}
    assert {"robot_id": "r02", "session_id": "s3"} in body["metrics"]["groups"]["real"]["sessions"]
    assert body["exclusions"] == []
    assert body["invariants"]["chain"]["status"] == "fail"
    assert "report: failed" in output
    top = page[: page.index('id="trust"')]
    assert "FAILED" in top


def _recorder_logs(folder: Path, counters: list[int]) -> list[str]:
    """Copies of the simulated golden session s4 under recorder-form ids with ``counters``."""
    folder.mkdir()
    for name in REGISTRIES:
        shutil.copy(METRICS_LOGS / name, folder / name)
    raw = (METRICS_LOGS / "s4.jsonl").read_bytes()
    names = []
    for counter in counters:
        session_id = f"{counter:06d}-20261112T080000Z-ab{counter:02x}"
        name = f"{session_id}.jsonl"
        (folder / name).write_bytes(
            raw.replace(b'"session_id":"s4"', f'"session_id":"{session_id}"'.encode())
        )
        names.append(name)
    return names


def test_census_reports_missing_counter__defs_11_3(tmp_path: Path) -> None:
    logs = _recorder_logs(tmp_path / "logs", [1, 2, 4])
    code, body, page, _ = _report(tmp_path / "logs", tmp_path / "out", *logs, "--n-boot", "50")
    robot = body["census"]["robots"][0]
    assert robot["missing_ranges"] == [["000003", "000003"]]
    assert robot["missing_count"] == 1
    assert (robot["lowest"], robot["highest"]) == ("000001", "000004")
    assert body["census"]["status"] == "fail"
    assert body["invariants"]["census"]["status"] == "fail"
    assert "census: 1 session counter(s) missing" in body["verdict"]["reasons"]
    assert "000003" in page
    assert code == 1


def _seal_into(folder: Path, logs: list[str]) -> list[str]:
    """Seal ``logs`` in place with the test key and write an anchor file and the vkey."""
    from report_helpers import seal_log  # noqa: PLC0415 -- test helper

    anchors = b""
    for name in logs:
        sealed, checkpoints = seal_log((folder / name).read_bytes(), name)
        (folder / name).write_bytes(sealed)
        (folder / name.replace(".jsonl", ".checkpoints.jsonl")).write_bytes(checkpoints)
        anchors += checkpoints
    (folder / ANCHORS).write_bytes(anchors)
    (folder / VKEY).write_bytes((SEALED / VKEY).read_bytes())
    return ["--vkey", VKEY, "--anchor", ANCHORS]


def test_clean_report_exits_0__defs_11(tmp_path: Path) -> None:
    """Verified at L2, complete, no census gap, nothing left out."""
    logs = _recorder_logs(tmp_path / "logs", [1, 2])
    trust = _seal_into(tmp_path / "logs", logs)
    code, body, _, output = _report(
        tmp_path / "logs", tmp_path / "out", *logs, *trust, "--n-boot", "50"
    )
    assert body["verdict"]["clean"] is True, body["verdict"]["reasons"]
    assert body["census"]["status"] == "pass"
    assert code == 0
    assert "not clean" not in output


def test_registry_mismatch_is_left_out_and_exit_1__defs_11_4(tmp_path: Path) -> None:
    folder = tmp_path / "logs"
    shutil.copytree(METRICS_LOGS, folder)
    (folder / "tasks.json").write_text('{"tasks": [{"task_definition_id": "other"}]}', "utf-8")
    code, body, _, _ = _report(folder, tmp_path / "out", *LOGS, "--n-boot", "50")
    assert code == 1
    assert len(body["exclusions"]) == 4
    assert all("tasks.json does not match" in e["reasons"][0] for e in body["exclusions"])
    assert "4 session(s) left out of the metrics" in body["verdict"]["reasons"]
    assert body["metrics"]["groups"] == {}
    assert body["verdict"]["completeness"] == "incomplete"


def test_html_has_every_section_and_no_external_resource__defs_11(tmp_path: Path) -> None:
    _, _, page, _ = _report(SEALED, tmp_path, *GOLDEN_RUNS["sealed"][1])
    for section in SECTION_IDS:
        assert f'id="{section}"' in page, section
    assert page.count("<svg") == 4
    assert "<script" not in page.lower()
    assert "@import" not in page
    assert "@font-face" not in page
    for attribute in re.findall(r'\b(?:src|href)\s*=\s*"([^"]*)"', page):
        assert attribute.startswith("#"), attribute
    assert "url(#" in page or "url(" not in page
    assert all(ref.startswith("#") for ref in re.findall(r"url\(([^)]*)\)", page))
    assert "<!DOCTYPE svg" not in page
    assert "<?xml" not in page
    ids = re.findall(r'\bid="([^"]+)"', page)
    assert len(ids) == len(set(ids))


def test_title_and_about_are_escaped__defs_11(tmp_path: Path) -> None:
    about = tmp_path / "about.txt"
    about.write_text("Site <script>alert(\"a\")</script> & 'quotes'\nline two", "utf-8")
    title = 'Fleet <script>alert("t")</script> "q" \'s\''
    code, body, page, _ = _report(
        METRICS_LOGS,
        tmp_path / "out",
        *LOGS,
        "--n-boot",
        "50",
        "--title",
        title,
        "--about",
        str(about),
    )
    assert code == 1
    assert body["title"] == title
    assert body["about"].startswith("Site <script>")
    assert "<script" not in page
    assert "&lt;script&gt;alert(&#34;t&#34;)&lt;/script&gt; &#34;q&#34; &#39;s&#39;" in page
    assert "&lt;script&gt;alert(&#34;a&#34;)&lt;/script&gt; &amp; &#39;quotes&#39;" in page
    assert any(f["role"] == "about" for f in body["provenance"]["input_files"])


def test_report_is_byte_identical_twice__defs_11(tmp_path: Path) -> None:
    args = GOLDEN_RUNS["sealed"][1]
    _report(SEALED, tmp_path / "a", *args)
    _report(SEALED, tmp_path / "b", *args)
    for name in ("report.json", "report.html"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_no_actor_ids_in_the_report__defs_11(tmp_path: Path) -> None:
    _report(SEALED, tmp_path, *GOLDEN_RUNS["sealed"][1])
    for name in ("report.json", "report.html"):
        text = (tmp_path / name).read_text(encoding="utf-8")
        assert not [actor for actor in ACTOR_IDS if actor in text]


def test_provenance_hashes_every_input_file__defs_11_1(tmp_path: Path) -> None:
    import hashlib  # noqa: PLC0415

    _, body, _, _ = _report(SEALED, tmp_path, *GOLDEN_RUNS["sealed"][1])
    files = {(f["role"], f["path"]): f["sha256"] for f in body["provenance"]["input_files"]}
    assert set(files) == {
        ("anchor", ANCHORS),
        ("vkey", VKEY),
        *(("log", name) for name in LOGS),
        *(("checkpoints", name.replace(".jsonl", ".checkpoints.jsonl")) for name in LOGS),
        ("registry_devices", "devices.json"),
        ("registry_tasks", "tasks.json"),
        ("registry_plan", "plan.json"),
    }
    for (_, path), digest in files.items():
        assert hashlib.sha256((SEALED / path).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize(
    ("setup", "args"),
    [
        ("about_utf8", ["--about", "about.txt"]),
        ("about_control", ["--about", "about.txt"]),
        ("about_large", ["--about", "about.txt"]),
        ("vkey", ["--vkey", "bad.vkey"]),
        ("anchor", ["--anchor", "missing.jsonl"]),
        ("title", ["--title", "bad\x07title"]),
        ("tsa_root", ["--tsa-root", "bad.pem"]),
    ],
)
def test_input_errors_exit_2(setup: str, args: list[str], tmp_path: Path) -> None:
    folder = tmp_path / "logs"
    shutil.copytree(METRICS_LOGS, folder)
    about = folder / "about.txt"
    if setup == "about_utf8":
        about.write_bytes(b"\xff\xfe bad")
    elif setup == "about_control":
        about.write_bytes(b"bell \x07")
    elif setup == "about_large":
        about.write_bytes(b"a" * 70_000)
    (folder / "bad.vkey").write_text("not a vkey\n", "utf-8")
    (folder / "bad.pem").write_text("not a certificate\n", "utf-8")
    code, body, _, _ = _report(folder, tmp_path / "out", "s1.jsonl", "--n-boot", "50", *args)
    assert code == 2
    assert body == {}


def test_unwritable_out_exits_2(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", "utf-8")
    code, _, _, _ = _report(METRICS_LOGS, blocker / "out", "s1.jsonl", "--n-boot", "50")
    assert code == 2


def test_report_without_extra_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "autonometry.report", None)
    code, body, _, _ = _report(METRICS_LOGS, tmp_path, "s1.jsonl")
    assert code == 2
    assert body == {}
