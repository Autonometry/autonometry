"""``scripts/release_assets.py``: the notes and assets of a GitHub release (RELEASING.md)."""

import hashlib
import importlib.util
import shutil
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

import autonometry

ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_assets", ROOT / "scripts" / "release_assets.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_assets"] = module
    spec.loader.exec_module(module)
    return module


ra = _load()

CHANGELOG = """# Changelog

## [Unreleased]

## [1.2.0] - 2026-12-01

### Added

- A thing.

## [1.1.0] - 2026-11-01

- Older.

[1.2.0]: https://example.org/compare/v1.1.0...v1.2.0
"""


def _root(tmp_path: Path, changelog: str = CHANGELOG) -> Path:
    root = tmp_path / "repo"
    (root / "spec").mkdir(parents=True)
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    shutil.copytree(ROOT / "spec" / "v0.3", root / "spec" / "v0.3")
    (root / "spec" / "v0.2").mkdir()
    return root


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "autonometry-1.2.0-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "autonometry-1.2.0.tar.gz").write_bytes(b"sdist")
    return dist


def test_the_notes_are_the_changelog_section_of_the_version() -> None:
    assert ra.changelog_section(CHANGELOG, "1.2.0") == "### Added\n\n- A thing.\n"
    assert ra.changelog_section(CHANGELOG, "1.1.0") == "- Older.\n"


@pytest.mark.parametrize("version", ["1.3.0", "1.2", "Unreleased"])
def test_a_version_without_a_dated_section_is_refused(version: str) -> None:
    text = CHANGELOG.replace("## [Unreleased]", "## [Unreleased] - x")
    with pytest.raises(ra.ReleaseError, match="no section"):
        ra.changelog_section(text, version)


def test_an_empty_section_is_refused() -> None:
    with pytest.raises(ra.ReleaseError, match="empty"):
        ra.changelog_section("## [2.0.0] - 2027-01-01\n\n## [1.0.0]\n- x\n", "2.0.0")


def test_the_changelog_has_a_section_for_the_package_version() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert ra.changelog_section(changelog, autonometry.__version__).strip()


def test_assets_are_the_dist_files_the_newest_spec_and_their_sums(tmp_path: Path) -> None:
    root, dist, out = _root(tmp_path), _dist(tmp_path), tmp_path / "release"
    assets = ra.assemble("1.2.0", dist, out, root)
    assert [p.name for p in assets] == [
        "autonometry-1.2.0-py3-none-any.whl",
        "autonometry-1.2.0.tar.gz",
        "spec-v0.3.zip",
        "SHA256SUMS",
    ]
    assert (out / "notes.md").read_text(encoding="utf-8") == "### Added\n\n- A thing.\n"
    with zipfile.ZipFile(out / "spec-v0.3.zip") as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert "v0.3/definitions.md" in names
        assert (
            archive.read("v0.3/rules.toml") == (ROOT / "spec" / "v0.3" / "rules.toml").read_bytes()
        )
    sums = (out / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    assert len(sums) == 3
    for line in sums:
        digest, name = line.split("  ")
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest


def test_the_spec_archive_is_the_same_bytes_every_time(tmp_path: Path) -> None:
    root, dist = _root(tmp_path), _dist(tmp_path)
    ra.assemble("1.2.0", dist, tmp_path / "a", root)
    ra.assemble("1.2.0", dist, tmp_path / "b", root)
    assert (tmp_path / "a" / "spec-v0.3.zip").read_bytes() == (
        tmp_path / "b" / "spec-v0.3.zip"
    ).read_bytes()


def test_an_existing_output_folder_is_never_reused(tmp_path: Path) -> None:
    root, dist, out = _root(tmp_path), _dist(tmp_path), tmp_path / "release"
    out.mkdir()
    assert ra.main(["1.2.0", str(dist), str(out)]) == 1
    with pytest.raises(FileExistsError):
        ra.assemble("1.2.0", dist, out, root)


def test_an_empty_dist_folder_is_refused(tmp_path: Path) -> None:
    (tmp_path / "dist").mkdir()
    with pytest.raises(ra.ReleaseError, match="no distribution files"):
        ra.assemble("1.2.0", tmp_path / "dist", tmp_path / "out", _root(tmp_path))


def test_bad_usage_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert ra.main(["1.2.0"]) == 2
    assert "Usage:" in capsys.readouterr().err
