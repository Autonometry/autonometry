"""Security review M10 (docs/security-review-2026-11.md): HTML and SVG injection into the report
(definitions.md 11). No string from a log, a registry, a plan, a file name, ``--title`` or
``--about`` may reach the page as markup: the page has no script, no event handler and no link
that a log's author chose."""

import json
import re
import shutil
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autonometry import cli
from report_helpers import LOGS, METRICS_LOGS, REGISTRIES

runner = CliRunner()
PAYLOAD = '"><script>alert(1)</script><img src=x onerror=alert(2)><svg onload=alert(3)>'
#: Elements that run code or load something; the page has none of them.
ACTIVE_TAGS = frozenset({"script", "img", "iframe", "object", "embed", "a", "link", "base", "form"})


class _Markup(HTMLParser):
    """Every element and attribute of a page, as a browser's parser sees them."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: set[str] = set()
        self.attributes: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)
        self.attributes += [(name, value or "") for name, value in attrs]


def _assert_inert(page: str) -> None:
    markup = _Markup()
    markup.feed(page)
    assert not markup.tags & ACTIVE_TAGS, markup.tags & ACTIVE_TAGS
    assert not [name for name, _ in markup.attributes if name.startswith("on")]
    assert not [v for _, v in markup.attributes if "javascript:" in v.lower() or "alert" in v]


def _hostile_set(folder: Path, registries: bool = True) -> list[str]:
    """The metrics golden logs with the payload in extension fields of their events and, with
    ``registries``, of their registries and plan (which then no longer match their hashes), and,
    where the file system allows it, in a log's file name."""
    for name in REGISTRIES:
        document = json.loads((METRICS_LOGS / name).read_text(encoding="utf-8"))
        if registries:
            document["x_note"] = PAYLOAD
        (folder / name).write_text(json.dumps(document), encoding="utf-8")
    logs = []
    for index, name in enumerate(LOGS):
        events = [json.loads(line) for line in (METRICS_LOGS / name).read_text().splitlines()]
        for event in events:
            event["x_note"] = PAYLOAD
        target = name
        if index == 0 and sys.platform != "win32":
            target = f"{PAYLOAD.replace('/', '_')}.jsonl"
        (folder / target).write_text("".join(json.dumps(e) + "\n" for e in events))
        logs.append(target)
    return logs


def test_no_input_string_reaches_the_report_as_markup__defs_11(tmp_path: Path) -> None:
    logs = _hostile_set(tmp_path)
    about = tmp_path / "about.txt"
    about.write_text(f"Prepared for {PAYLOAD}\n", encoding="utf-8")
    out = tmp_path / "report"
    result = runner.invoke(
        cli.app,
        [
            "report",
            *(str(tmp_path / log) for log in logs),
            "--title",
            PAYLOAD,
            "--about",
            str(about),
            "--n-boot",
            "20",
            "--out",
            str(out),
        ],
    )
    assert "unexpected failure" not in result.output, result.output
    page = (out / "report.html").read_text(encoding="utf-8")
    _assert_inert(page)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page  # the title and about, escaped
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["title"] == PAYLOAD


def test_the_figures_hold_no_text_from_the_input__defs_11(tmp_path: Path) -> None:
    """The only markup the page trusts is the SVG drawn from numbers and fixed labels: its text
    is drawn as paths, so no input string, however crafted, appears in it."""
    logs = _hostile_set(tmp_path, registries=False)
    out = tmp_path / "report"
    runner.invoke(
        cli.app,
        ["report", *(str(tmp_path / log) for log in logs), "--n-boot", "20", "--out", str(out)],
    )
    page = (out / "report.html").read_text(encoding="utf-8")
    figures = re.findall(r"<svg.*?</svg>", page, re.S)
    assert figures
    for svg in figures:
        assert "alert" not in svg
        assert "<text" not in svg
    _assert_inert(page)


@pytest.mark.parametrize("title", ["a\x1b[31mb", "line\nbreak", "x" * 1000])
def test_a_title_with_control_characters_or_too_long_is_refused__defs_11(
    tmp_path: Path, title: str
) -> None:
    shutil.copyfile(METRICS_LOGS / LOGS[0], tmp_path / LOGS[0])
    result = runner.invoke(
        cli.app, ["report", str(tmp_path / LOGS[0]), "--title", title, "--out", str(tmp_path / "o")]
    )
    assert result.exit_code == 2
    assert not (tmp_path / "o" / "report.html").exists()
