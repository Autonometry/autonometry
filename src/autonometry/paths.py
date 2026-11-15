"""File names as reports and JSON outputs write them (CLAUDE.md product rule 4).

The same log and parameters must give the same report on every system, so a path is always
written with forward slashes, relative where it was given relative: ``logs\r1\\events.jsonl``
given on Windows and ``logs/r1/events.jsonl`` given on Linux both read ``logs/r1/events.jsonl``.
Windows accepts forward slashes, so the name still opens the file there.
"""

import os
from pathlib import Path, PurePath


def path_text(path: str | os.PathLike[str]) -> str:
    """``path`` with forward slashes (:meth:`PurePath.as_posix`), never made absolute. A pure
    path keeps its own flavour: ``PureWindowsPath("a\\b")`` gives ``a/b`` on any system."""
    pure = path if isinstance(path, PurePath) else Path(os.fspath(path))
    return pure.as_posix()
