"""Files of a GitHub release (RELEASING.md): the release notes and the assets to attach.

Usage: python scripts/release_assets.py VERSION DIST_DIR OUT_DIR

Writes into OUT_DIR, which must not exist yet:

- ``notes.md``: the CHANGELOG.md section ``## [VERSION]`` without its heading;
- every file of DIST_DIR (the sdist and the wheel that went to PyPI);
- ``spec-v<spec>.zip``: the newest ``spec/v<spec>/`` folder, files in sorted order with a fixed
  date, so the same folder always gives the same bytes;
- ``SHA256SUMS``: the SHA-256 of every asset, in the format ``sha256sum --check`` reads.

Standard library only, so it runs on a bare runner. Exit status 1 with one line on stderr
when the version has no CHANGELOG section, the section is empty or DIST_DIR has no files.
"""

import hashlib
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: The date stamped on every entry of the spec archive (zip cannot store an earlier one).
ZIP_DATE = (1980, 1, 1, 0, 0, 0)


class ReleaseError(Exception):
    """The release cannot be assembled; the message is one line for the user."""


def changelog_section(changelog: str, version: str) -> str:
    """The body of the section ``## [version]`` of a Keep a Changelog file, stripped."""
    heading = re.compile(rf"^## \[{re.escape(version)}\](?: - \d{{4}}-\d{{2}}-\d{{2}})?\s*$")
    lines = changelog.splitlines()
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        raise ReleaseError(f"CHANGELOG.md has no section [{version}]")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    body = "\n".join(lines[start + 1 : end])
    body = re.sub(r"(?m)^\[[^\]]+\]: \S+\s*$", "", body).strip()
    if not body:
        raise ReleaseError(f"the CHANGELOG.md section [{version}] is empty")
    return body + "\n"


def newest_spec(spec_root: Path) -> Path:
    """The ``spec/v<major>.<minor>`` folder with the highest version."""
    folders = [p for p in spec_root.iterdir() if p.is_dir() and re.fullmatch(r"v\d+\.\d+", p.name)]
    if not folders:
        raise ReleaseError(f"no spec version folder in {spec_root}")
    return max(folders, key=lambda p: tuple(int(n) for n in p.name[1:].split(".")))


def write_spec_zip(folder: Path, out: Path) -> None:
    """Zip ``folder`` as ``<folder name>/<file>`` entries, sorted, each with :data:`ZIP_DATE`."""
    with zipfile.ZipFile(out, "x", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            name = f"{folder.name}/{path.relative_to(folder).as_posix()}"
            info = zipfile.ZipInfo(name, ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


def sha256sums(paths: list[Path]) -> str:
    """``<hex>  <name>`` per file, sorted by name."""
    return "".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
        for p in sorted(paths, key=lambda p: p.name)
    )


def assemble(version: str, dist: Path, out: Path, root: Path = ROOT) -> list[Path]:
    """Write the notes and assets of release ``version`` into the new folder ``out``; returns
    the assets (every file but ``notes.md``)."""
    notes = changelog_section((root / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    files = sorted(p for p in dist.iterdir() if p.is_file())
    if not files:
        raise ReleaseError(f"{dist} holds no distribution files")
    spec = newest_spec(root / "spec")
    out.mkdir(parents=True)
    (out / "notes.md").write_text(notes, encoding="utf-8", newline="\n")
    assets = [Path(shutil.copy2(p, out / p.name)) for p in files]
    archive = out / f"spec-{spec.name}.zip"
    write_spec_zip(spec, archive)
    assets.append(archive)
    sums = out / "SHA256SUMS"
    sums.write_text(sha256sums(assets), encoding="ascii", newline="\n")
    return [*assets, sums]


def main(argv: list[str]) -> int:
    """Command line entry point; see the module docstring."""
    if len(argv) != 3:  # noqa: PLR2004 -- VERSION DIST_DIR OUT_DIR
        print(__doc__.splitlines()[2], file=sys.stderr)
        return 2
    version, dist, out = argv[0], Path(argv[1]), Path(argv[2])
    try:
        assets = assemble(version, dist, out)
    except (ReleaseError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path in assets:
        print(path.name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
