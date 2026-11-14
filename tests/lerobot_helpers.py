"""Shared helpers for the LeRobot connector tests. Not collected by pytest (no ``test_``)."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from autonometry.connectors import lerobot
from autonometry.connectors.tb4 import devices_from_registry
from autonometry.spec import load_rules

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "connectors" / "lerobot"
GOLDEN = Path(__file__).resolve().parent / "golden" / "lerobot"
RULES = load_rules("0.3")
DEVICES = devices_from_registry(json.loads((CONFIG / "devices.json").read_text(encoding="utf-8")))
POLICY = "outputs/train/act_so101/checkpoints/last/pretrained_model"
#: The local wall-clock time LeRobot prints on the first line of every test log.
T0 = datetime(2026, 11, 14, 10, 0, 0, tzinfo=UTC)
INFO_FPS = 30


def line(message: str, at: float = 0.0, location: str = "gies/dagger.py:659") -> str:
    """A log line in LeRobot's ``init_logging`` format, printed ``at`` seconds after T0."""
    stamp = (T0 + timedelta(seconds=at)).strftime("%Y-%m-%d %H:%M:%S")
    return f"INFO {stamp} {location[-15:]:>15} {message}"


def src(at: float = 0.0) -> str:
    """The ``x_src_ts`` of a line printed ``at`` seconds after T0."""
    return (T0 + timedelta(seconds=at)).strftime("%Y-%m-%d %H:%M:%S")


def settings(**options: Any) -> lerobot.Settings:
    """Live settings from the spec's parameters and the example SO-101 registry."""
    values: dict[str, Any] = {
        "devices": DEVICES,
        "idle_threshold_s": RULES.params.idle_threshold_s,
        "task_definition_id": "pick_place_cube",
    }
    values.update(options)
    return lerobot.Settings(**values)


def engine(**options: Any) -> lerobot.LeRobotEngine:
    return lerobot.LeRobotEngine(settings(**options))


def feed(eng: lerobot.LeRobotEngine, lines: Sequence[tuple[float, str]]) -> list[lerobot.Message]:
    """Observe each (t, line) in order; return every message, recognised lines only."""
    out: list[lerobot.Message] = []
    for t, text in lines:
        parsed = lerobot.parse_line(text)
        if parsed is not None:
            out += eng.observe(t, parsed)
    return out


SETUP = (
    "Building rollout context...",
    f"Loading policy from '{POLICY}'...",
    "Connecting robot (so101_follower)...",
    "Connecting teleoperator (so101_leader)...",
    "Dataset ready: op/rollout_test_20261114_100000 (0 existing episodes)",
)


def setup_lines(at: float = 0.0, teleop: str | None = "so101_leader") -> list[tuple[float, str]]:
    """The rollout's setup lines, all printed at ``at``."""
    lines = [m for m in SETUP if teleop is not None or "teleoperator" not in m]
    lines = [m.replace("so101_leader", teleop) if teleop else m for m in lines]
    return [(at, line(m, at)) for m in lines]


# -- datasets -----------------------------------------------------------------------------------


def info(fps: float = INFO_FPS, **overrides: Any) -> dict[str, Any]:
    """A ``meta/info.json`` of a DAgger dataset (LeRobotDataset v3.0)."""
    document: dict[str, Any] = {
        "codebase_version": "v3.0",
        "fps": fps,
        "robot_type": "so_follower",
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "features": {
            "intervention": {"dtype": "bool", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
        },
    }
    document.update(overrides)
    return document


def write_dataset(  # noqa: PLR0913 -- the parts of a dataset a test varies
    root: Path,
    episodes: Sequence[tuple[int, str, Sequence[bool | None]]],
    *,
    files: Sequence[int] | None = None,
    write_files: Sequence[int] | None = None,
    as_list: bool = False,
    lengths: dict[int, int] | None = None,
    frames: dict[int, Sequence[int]] | None = None,
    info_doc: dict[str, Any] | None = None,
) -> Path:
    """Write a tiny LeRobotDataset v3.0 with pyarrow: ``episodes`` are (index, task, labels).
    ``files[k]`` is episode k's data file index (default 0); only ``write_files`` are written
    (default all). ``as_list`` stores ``intervention`` as a one-element list, like shape [1]."""
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    files = list(files) if files is not None else [0] * len(episodes)
    lengths = lengths or {}
    frames = frames or {}
    (root / "meta" / "episodes" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (root / "meta" / "info.json").write_text(json.dumps(info_doc or info()), encoding="utf-8")
    meta = {
        "episode_index": [e[0] for e in episodes],
        "tasks": [[e[1]] for e in episodes],
        "length": [lengths.get(e[0], len(e[2])) for e in episodes],
        "data/chunk_index": [0] * len(episodes),
        "data/file_index": files,
    }
    pq.write_table(pa.table(meta), root / "meta" / "episodes" / "chunk-000" / "file-000.parquet")
    by_file: dict[int, dict[str, list[Any]]] = {}
    for (index, _, labels), file_index in zip(episodes, files, strict=True):
        columns = by_file.setdefault(
            file_index, {"episode_index": [], "frame_index": [], "intervention": []}
        )
        indices = frames.get(index, range(len(labels)))
        columns["episode_index"] += [index] * len(labels)
        columns["frame_index"] += list(indices)
        columns["intervention"] += [[v] for v in labels] if as_list else list(labels)
    (root / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    for file_index, columns in by_file.items():
        if write_files is not None and file_index not in write_files:
            continue
        kind = pa.list_(pa.bool_()) if as_list else pa.bool_()
        table = pa.table(
            {
                "episode_index": pa.array(columns["episode_index"], pa.int64()),
                "frame_index": pa.array(columns["frame_index"], pa.int64()),
                "intervention": pa.array(columns["intervention"], kind),
            }
        )
        pq.write_table(table, root / "data" / "chunk-000" / f"file-{file_index:03d}.parquet")
    return root
