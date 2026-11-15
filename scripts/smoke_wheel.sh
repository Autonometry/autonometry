#!/usr/bin/env bash
# Smoke test of built distributions (RELEASING.md): check the sdist and wheel in DIST_DIR with
# `twine check --strict` (the README must render on PyPI), install the wheel with the report
# extra into a fresh virtual environment outside the source tree, and run the installed command:
# --version (must match the wheel), spec show, and the demo, which needs no robot and no network.
# Runs in Git Bash on Windows too. Needs uv.
#
# Usage: scripts/smoke_wheel.sh DIST_DIR
set -euo pipefail

twine="twine==7.0.0"
python="${SMOKE_PYTHON:-3.12}"

# A path native Windows programs (uv, python) understand; unchanged elsewhere.
native() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s\n' "$1"; fi
}

dist="$(cd "${1:?usage: scripts/smoke_wheel.sh DIST_DIR}" && pwd)"
shopt -s nullglob
wheels=("$dist"/*.whl)
sdists=("$dist"/*.tar.gz)
if [ "${#wheels[@]}" -ne 1 ] || [ "${#sdists[@]}" -ne 1 ]; then
  echo "expected exactly one wheel and one sdist in $dist" >&2
  exit 1
fi
wheel="$(native "${wheels[0]}")"
sdist="$(native "${sdists[0]}")"
version="$(basename "$wheel" | cut -d- -f2)"

uvx --from "$twine" twine check --strict "$wheel" "$sdist"

work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/autonometry-smoke.XXXXXX")"
venv="$(native "$work/venv")"
uv venv --no-project --python "$python" "$venv"
uv pip install --python "$venv" "${wheel}[report]"
if [ -x "$work/venv/bin/autonometry" ]; then
  autonometry="$work/venv/bin/autonometry"
else
  autonometry="$work/venv/Scripts/autonometry.exe"
fi

# Outside the source tree, so nothing can be imported from a checkout.
cd "$work"
reported="$("$autonometry" --version)"
if [ "$reported" != "autonometry $version" ]; then
  echo "installed command says '$reported', the wheel is $version" >&2
  exit 1
fi
echo "$reported"
spec="$("$autonometry" spec list | tail -n 1)"
"$autonometry" spec show "$spec"
"$autonometry" demo --out demo
for file in report.html report.json verification.json; do
  test -s "demo/$file" || { echo "the demo did not write $file" >&2; exit 1; }
done
echo "smoke test passed: autonometry $version, spec $spec"
