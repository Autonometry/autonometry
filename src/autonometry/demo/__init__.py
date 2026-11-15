"""``autonometry demo``: the whole chain on a session shipped with the package (docs/cli.md).

The session (``session/events.jsonl``) is synthetic, written the way the recorder writes a log:
one twelve-minute shift of a warehouse robot with six tasks, a remote takeover, an automatic
safety stop and a connector crash that leaves 15 s unknown. ``tests/demo_helpers.py`` rebuilds
it from its scenario. This module holds the data and the words the command prints; the command
itself is in :mod:`autonometry.cli`.
"""

from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any, Final

#: The spec version the shipped session follows.
SPEC_VERSION: Final = "0.3"
ROBOT_ID: Final = "demo_amr_01"
SESSION_ID: Final = "000001-20261110T080000Z-5d1e"
KEY_NAME: Final = f"autonometry/{ROBOT_ID}"
TITLE: Final = "Autonometry demo: one shift of a warehouse robot (synthetic data)"
SUMMARY: Final = "6 tasks, a remote takeover, an automatic safety stop, 15 s unknown"

#: Paths inside the output folder, relative to it, so nothing printed or written depends on
#: where the folder is.
KEY: Final = Path("key") / "demo.key"
VKEY: Final = Path("key") / "demo.key.vkey"
SESSION_DIR: Final = Path("logs") / ROBOT_ID / SESSION_ID
LOG: Final = SESSION_DIR / "events.jsonl"
CHECKPOINTS: Final = SESSION_DIR / "events.checkpoints.jsonl"
ABOUT: Final = Path("about.txt")
VERIFICATION: Final = Path("verification.json")
REPORT_JSON: Final = Path("report.json")
REPORT_HTML: Final = Path("report.html")
#: The registries the session declares, copied beside its log where the report looks for them.
REGISTRIES: Final = ("devices.json", "tasks.json", "plan.json")


def data(name: str) -> bytes:
    """The bytes of one shipped data file: ``events.jsonl``, a registry or ``about.txt``."""
    return resources.files(__name__).joinpath("session", name).read_bytes()


def why_l1(checkpoints: int) -> list[str]:
    """Why the sealed session is trust level L1 and not L2 (definitions.md 10.6), in plain
    words, for a log sealed with ``checkpoints`` signed checkpoints and no anchor."""
    return [
        'Result: meets trust level L1, not L2 (and so not "verified").',
        f"  L1: every event is hash-chained and the {checkpoints} checkpoint(s) are signed by",
        "      the demo key, and they match the events: nothing was changed after sealing,",
        "      as far as the key's holder is concerned.",
        "  Not L2: no checkpoint is anchored. An anchor is a timestamp from a timestamp",
        "      authority (autonometry timestamp, or tsa_url on the recorder) or a copy of a",
        "      checkpoint kept by someone else (--anchor). Without one, whoever holds the key",
        "      could have rewritten and re-sealed the whole log. The demo makes no network",
        "      calls, so it asks no timestamp authority.",
    ]


def headline_lines(report: Mapping[str, Any]) -> list[str]:
    """The verdict box's headline numbers (definitions.md 11), one line per metric and group."""
    lines = []
    for group, metrics in report["verdict"]["headline"].items():
        for name, value in metrics.items():
            estimate, ci = value["estimate"], value["ci"]
            shown = "not computed" if estimate is None else f"{estimate:.3g}"
            if estimate is not None and ci is not None and None not in ci:
                shown += f"  (95 % CI {ci[0]:.3g} to {ci[1]:.3g})"
            lines.append(f"  {group}: {name} = {shown}")
    return lines


def file_lines() -> list[str]:
    """What each file the demo writes is, report.html first: the one to open."""
    top = [
        (REPORT_HTML.as_posix(), "OPEN THIS in a browser: the report for a buyer or auditor"),
        (REPORT_JSON.as_posix(), "the same report as JSON, for tools"),
        (VERIFICATION.as_posix(), "the verifier's result (verify --json --require L1)"),
        (ABOUT.as_posix(), "what happens in the session"),
        (KEY.as_posix(), "throwaway private key (0600, folder 0700): signs only this demo"),
        (VKEY.as_posix(), "its public verifier key, which verify and report trust"),
    ]
    session = [
        (LOG.name, "the sealed log: every change of control, hash-chained"),
        (CHECKPOINTS.name, "its signed Merkle checkpoints"),
        ("devices.json", "device registry: who and what can command the robot"),
        ("tasks.json", "task registry: the tasks declared before the session"),
        ("plan.json", "plan: pre-registered human actions (none here)"),
    ]
    width = max(len(name) for name, _ in top + session) + 4
    lines = [f"  {name:<{width}}{text}" for name, text in top]
    lines.append(f"  {SESSION_DIR.as_posix()}/")
    lines += [f"    {name:<{width - 2}}{text}" for name, text in session]
    return lines


def next_steps() -> list[str]:
    """The commands that redo the demo's checks by hand, run inside its folder."""
    log, vkey = LOG.as_posix(), VKEY.as_posix()
    return [
        f"  autonometry timeline {log}",
        f"  autonometry verify {log} --vkey {vkey} --require L1",
        f"  autonometry report {log} --vkey {vkey} --out my-report",
    ]
