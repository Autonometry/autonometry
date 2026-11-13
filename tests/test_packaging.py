import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC_FILES = ["definitions.md", "event.schema.json", "rules.toml"]


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build sdist and wheel (the wheel is built from the sdist) into a temporary folder."""
    uv = shutil.which("uv")
    assert uv is not None, "uv must be on PATH to build the package"
    out = tmp_path_factory.mktemp("dist")
    # Fixed arguments, nothing from input.
    subprocess.run(  # noqa: S603
        [uv, "build", "--out-dir", str(out), str(REPO)],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return out


def test_wheel_contains_spec_files__defs_8(dist: Path) -> None:
    (wheel,) = dist.glob("autonometry-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for name in SPEC_FILES:
            assert f"autonometry/spec/v0.3/{name}" in names
            assert (
                archive.read(f"autonometry/spec/v0.3/{name}")
                == (REPO / "spec" / "v0.3" / name).read_bytes()
            )
        assert "autonometry/py.typed" in names
        assert "autonometry/report/templates/report.html.j2" in names
        assert not any(name.startswith("research/") for name in names)


def test_sdist_carries_spec_but_not_research(dist: Path) -> None:
    (sdist,) = dist.glob("autonometry-*.tar.gz")
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
    assert any(name.endswith("spec/v0.3/rules.toml") for name in names)
    assert not any("/research/" in name for name in names)


def test_metadata_uses_spdx_licence_and_exact_owner() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == "Apache-2.0"
    assert not any(c.startswith("License ::") for c in project["classifiers"])
    assert all(
        url.startswith("https://github.com/Autonometry/autonometry")
        for url in project["urls"].values()
    )
    for doc in ("SECURITY.md", "RELEASING.md"):
        assert "github.com/autonometry/" not in (REPO / doc).read_text(encoding="utf-8")
    assert "git tag -a" in (REPO / "RELEASING.md").read_text(encoding="utf-8")
