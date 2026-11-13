"""The core package never imports the report extra's numeric packages (CLAUDE.md Stack: core is
installed on robots and stays light); only ``autonometry.stats`` and ``autonometry.report`` may."""

import ast
import subprocess
import sys
from pathlib import Path

import autonometry

HEAVY = ("numpy", "scipy", "statsmodels", "pandas", "matplotlib", "jinja2")
EXTRA_PACKAGES = ("stats", "report")
PACKAGE = Path(autonometry.__file__).parent


def _imported(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_no_core_module_imports_a_heavy_package() -> None:
    core = [
        p
        for p in PACKAGE.rglob("*.py")
        if not set(EXTRA_PACKAGES) & set(p.relative_to(PACKAGE).parts)
    ]
    assert len(core) > 20
    offenders = {str(p.relative_to(PACKAGE)): _imported(p) & set(HEAVY) for p in core}
    assert {name: found for name, found in offenders.items() if found} == {}


def test_running_the_core_loads_no_heavy_package() -> None:
    code = (
        "import sys\n"
        "import autonometry.cli, autonometry.timeline, autonometry.reader, autonometry.verify\n"
        "from typer.testing import CliRunner\n"
        "CliRunner().invoke(autonometry.cli.app, ['timeline', '--help'])\n"
        f"print(sorted(m for m in {HEAVY!r} if m in sys.modules))\n"
    )
    result = subprocess.run(  # noqa: S603 -- fixed arguments, no input
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"
