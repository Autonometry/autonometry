"""LeRobot demo (README.md beside this file): download a public DAgger dataset's metadata and its
first data files from the Hugging Face Hub, import it with ``autonometry lerobot import`` and
write a report.

Standard library only for the download (urllib): no Hugging Face client is installed. The
dataset revision is pinned, every path the Hub lists is checked before it is used, the total size
is printed first and the demo stops when it is above ``--max-mb``. Needs the ``lerobot`` and
``report`` extras: ``uv sync --all-extras``, then ``uv run python demo/lerobot/run_demo.py``.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
#: The dataset of dossier 14.6 (research/lerobot-so101), Apache-2.0, and the revision checked.
REPO = "jogarulfo/rollout_dagger_2026-10-19_store_pvc_pieces_bbox_all_v6_20261019_081024"
REVISION = "c4014132f7caa3bc6b0b2dfc488e6e467f2ac990"
HUB = "https://huggingface.co"
#: The dataset's name carries its local start time 08:10:24; the Hub's commit at 06:37:09Z says
#: the host ran on UTC+2. The anchor only places the sessions in time; no metric depends on it.
START = "2026-10-19T06:10:24Z"
#: The dataset names neither its policy nor its teleoperator; config/devices.json declares these.
POLICY = "demo_dagger_policy"
TELEOP = "so101_leader"
DATA_FILES = 3
MAX_MB = 100
TIMEOUT_S = 30
RETRIES = 3
MAX_LISTING_BYTES = 8 << 20
MAX_PAGES = 20

_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_REVISION_RE = re.compile(r"[0-9a-f]{40}")
_META_RE = re.compile(r"meta/(?:[A-Za-z0-9_.-]+/){0,3}[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}")
_DATA_RE = re.compile(r"data/chunk-[0-9]{3,6}/file-[0-9]{3,6}\.parquet")
_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')

logger = logging.getLogger("lerobot_demo")


class DemoError(Exception):
    """The demo cannot go on; the message is one line."""


def _get(url: str, limit: int) -> tuple[bytes, str | None]:
    """GET over HTTPS with a timeout and retries with backoff; the body (at most ``limit``
    bytes) and the URL of the next page, if the server gives one."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise DemoError("only https URLs are fetched")
    request = urllib.request.Request(url, headers={"User-Agent": "autonometry-demo"})  # noqa: S310 -- https only, checked above
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
                body = response.read(limit + 1)
                link = response.headers.get("Link") or ""
            if len(body) > limit:
                raise DemoError(f"{url} returned more than {limit} bytes")
            found = _NEXT_RE.search(link)
            return body, found.group(1) if found else None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt == RETRIES - 1:
                raise DemoError(f"cannot fetch {url}: {exc}") from None
            time.sleep(2**attempt)
    raise DemoError(f"cannot fetch {url}")


def list_files(repo: str, revision: str, folder: str) -> list[tuple[str, int]]:
    """(path, size in bytes) of every file under ``folder`` of a dataset revision on the Hub."""
    url: str | None = (
        f"{HUB}/api/datasets/{repo}/tree/{revision}/{folder}?recursive=true&expand=false"
    )
    files: list[tuple[str, int]] = []
    for _ in range(MAX_PAGES):
        if url is None:
            return sorted(files)
        body, url = _get(url, MAX_LISTING_BYTES)
        entries: Any = json.loads(body)
        if not isinstance(entries, list):
            raise DemoError("the Hub listing is not a list")
        for entry in entries:
            if isinstance(entry, dict) and entry.get("type") == "file":
                path, size = entry.get("path"), entry.get("size")
                if isinstance(path, str) and isinstance(size, int) and size >= 0:
                    files.append((path, size))
    raise DemoError(f"more than {MAX_PAGES} pages listing {folder}")


def choose(
    meta: Sequence[tuple[str, int]], data: Sequence[tuple[str, int]], n: int
) -> list[tuple[str, int]]:
    """``meta/`` and the first ``n`` data parquet files; a path of any other form is refused."""
    for path, _ in meta:
        if not _META_RE.fullmatch(path):
            raise DemoError(f"unexpected path in meta/: {path!r}")
    parquet = sorted((p, s) for p, s in data if _DATA_RE.fullmatch(p))
    return [*sorted(meta), *parquet[:n]]


def download(repo: str, revision: str, files: Sequence[tuple[str, int]], out: Path) -> None:
    """Download each file into ``out``, at its path, checking its size; written whole or not at
    all (temporary file, then rename)."""
    root = out.resolve()
    for path, size in files:
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise DemoError(f"path leaves the download folder: {path!r}")
        if target.is_file() and target.stat().st_size == size:
            continue
        body, _ = _get(f"{HUB}/datasets/{repo}/resolve/{revision}/{path}", size)
        if len(body) != size:
            raise DemoError(f"{path}: expected {size} bytes, got {len(body)}")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.part")
        tmp.write_bytes(body)
        os.replace(tmp, target)


def fetch(repo: str, revision: str, out: Path, n_data: int, max_mb: float) -> int:
    """List, check the total size, download; return the total in bytes."""
    if not _REPO_RE.fullmatch(repo) or not _REVISION_RE.fullmatch(revision):
        raise DemoError("--repo must be <owner>/<name> and --revision a 40-hex commit")
    files = choose(list_files(repo, revision, "meta"), list_files(repo, revision, "data"), n_data)
    total = sum(size for _, size in files)
    print(f"{len(files)} files, {total / 1e6:.1f} MB to download from {repo}@{revision[:12]}")
    if total > max_mb * 1e6:
        raise DemoError(
            f"{total / 1e6:.1f} MB is more than --max-mb {max_mb:g}; nothing downloaded"
        )
    download(repo, revision, files, out)
    return total


def _run_cli(args: Sequence[str], allowed: Sequence[int] = (0,)) -> None:
    """Run an ``autonometry`` command in this process; fail unless its status is ``allowed``."""
    from autonometry.cli import app  # noqa: PLC0415 -- after the download

    try:
        code = app(list(args), standalone_mode=False)
    except SystemExit as exc:
        code = exc.code
    if (code or 0) not in allowed:
        raise DemoError(f"autonometry {args[0]} failed with status {code}")


def main(argv: Sequence[str] | None = None) -> int:
    """Download, import, report. Exit status 0 on success, 1 on any failure."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HERE / "run", help="Folder for everything.")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--revision", default=REVISION)
    parser.add_argument("--data-files", type=int, default=DATA_FILES)
    parser.add_argument("--max-mb", type=float, default=MAX_MB)
    options = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    run: Path = options.out
    dataset, logs, report = run / "dataset", run / "logs", run / "report"
    try:
        fetch(options.repo, options.revision, dataset, options.data_files, options.max_mb)
        _run_cli(
            [
                "lerobot", "import", str(dataset), "--out", str(logs),
                "--registry", str(HERE / "config"), "--start", START,
                "--policy", POLICY, "--teleop", TELEOP,
            ]
        )  # fmt: skip
        events = sorted(str(p) for p in logs.glob("*/*/events.jsonl"))
        # Status 1: written but not clean, which imported logs never are (trust level L1).
        _run_cli(
            ["report", *events, "--out", str(report), "--title", "LeRobot DAgger demo"], (0, 1)
        )
    except DemoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"report: {report / 'report.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
