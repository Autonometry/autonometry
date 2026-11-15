# Releasing

Releases go to PyPI from GitHub Actions with [Trusted Publishing](https://docs.pypi.org/trusted-publishers/): no API token exists anywhere. The workflow is `.github/workflows/release.yml`; it runs on tags matching `v*` and has four jobs:

| Job | Runs on | Permissions | What it does |
|---|---|---|---|
| `build` | Ubuntu | `contents: read` | the full check (ruff, mypy, pytest), the tag against `__version__`, then `uv build` into the `dist` artifact |
| `smoke` | Linux, macOS, Windows | `contents: read` | `scripts/smoke_wheel.sh` on the `dist` artifact: `twine check --strict` (the README must render on PyPI), the wheel installed with the `report` extra in a fresh virtual environment outside the checkout, then `autonometry --version` (must equal the wheel's version), `autonometry spec show` and `autonometry demo` |
| `publish` | Ubuntu, `pypi` environment | `id-token: write` | uploads the same `dist` files to PyPI with attestations, after a reviewer approves |
| `github-release` | Ubuntu | `contents: write` | `scripts/release_assets.py`: creates the GitHub release for the tag with the CHANGELOG section as notes, and attaches the sdist, the wheel, `spec-v<spec>.zip` (the newest `spec/` folder) and `SHA256SUMS` |

Each job gets only the permissions in its row; the workflow default is `contents: read`. CI runs the same smoke test on every pull request (job `wheel`), and `zizmor` checks both workflows.

## One-time setup

1. **GitHub environment.** In the repository: *Settings → Environments → New environment*, name it exactly `pypi`. Add required reviewers so that a person approves each publish.
2. **Pending trusted publisher on PyPI** (the project does not exist on PyPI yet). Sign in at <https://pypi.org>, open *Your account → Publishing* (<https://pypi.org/manage/account/publishing/>), and under *Add a new pending publisher* choose **GitHub** and enter:
   - PyPI project name: `autonometry`
   - Owner: `Autonometry` (exact case: trusted publishing compares it)
   - Repository name: `autonometry`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

   The first successful publish creates the project and turns the pending publisher into a normal one. A pending publisher does not reserve the name, so publish soon after adding it.
3. Optional: repeat on <https://test.pypi.org> if you want a dry run (this needs a copy of the workflow that targets TestPyPI; the shipped workflow publishes to PyPI only).

## Cutting a release

1. Start from an up-to-date `main` with CI green.
2. Choose the version `X.Y.Z` and edit `__version__` in `src/autonometry/__init__.py` (the only place; the build reads it from there).
3. In `CHANGELOG.md`, rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`, add a fresh empty `[Unreleased]` section, and update the comparison links if present.
4. Run the full check and the smoke test locally (the smoke test needs Bash; Git Bash on Windows):
   ```
   uv sync --all-extras --dev --locked
   uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
   rm -rf dist && uv build --out-dir dist && scripts/smoke_wheel.sh dist
   ```
   A test checks that `CHANGELOG.md` has a section for `__version__`; without one, the release job could not write its notes.
5. Commit (`chore: release X.Y.Z`), open a pull request, merge it.
6. Tag the merged commit and push the tag:
   ```
   git checkout main && git pull
   git tag -a vX.Y.Z -m "autonometry X.Y.Z"
   git push origin vX.Y.Z
   ```
   The tag must equal `v` plus `__version__`; the workflow fails otherwise.
7. Open the *Release* run under *Actions*. `build` tests and builds, `smoke` installs and runs the wheel on three systems, and `publish` waits for approval of the `pypi` environment. Approve it. `github-release` then creates the GitHub release by itself.
8. Check <https://pypi.org/project/autonometry/> shows the new version with attestations ("Verified details" on the file page), then install it in a clean environment: `uvx autonometry@X.Y.Z --version`, and run `uvx --from 'autonometry[report]==X.Y.Z' autonometry demo --out demo`.
9. Check the release under *Releases*: its notes are the changelog section, and `sha256sum --check SHA256SUMS` passes on the downloaded files. If `github-release` failed after PyPI accepted the upload, re-run only that job; it refuses to run when a release for the tag already exists (delete the partial release first).

A published version cannot be replaced. If something is wrong, yank it on PyPI and release the next version.
