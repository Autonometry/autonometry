# Releasing

Releases go to PyPI from GitHub Actions with [Trusted Publishing](https://docs.pypi.org/trusted-publishers/): no API token exists anywhere. The workflow is `.github/workflows/release.yml`; it runs on tags matching `v*`.

## One-time setup

1. **GitHub environment.** In the repository: *Settings → Environments → New environment*, name it exactly `pypi`. Add required reviewers so that a person approves each publish.
2. **Pending trusted publisher on PyPI** (the project does not exist on PyPI yet). Sign in at <https://pypi.org>, open *Your account → Publishing* (<https://pypi.org/manage/account/publishing/>), and under *Add a new pending publisher* choose **GitHub** and enter:
   - PyPI project name: `autonometry`
   - Owner: `autonometry`
   - Repository name: `autonometry`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

   The first successful publish creates the project and turns the pending publisher into a normal one. A pending publisher does not reserve the name, so publish soon after adding it.
3. Optional: repeat on <https://test.pypi.org> if you want a dry run (this needs a copy of the workflow that targets TestPyPI; the shipped workflow publishes to PyPI only).

## Cutting a release

1. Start from an up-to-date `main` with CI green.
2. Choose the version `X.Y.Z` and edit `__version__` in `src/autonometry/__init__.py` (the only place; the build reads it from there).
3. In `CHANGELOG.md`, rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`, add a fresh empty `[Unreleased]` section, and update the comparison links if present.
4. Run the full check locally:
   ```
   uv sync --all-extras --dev --locked
   uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
   ```
5. Commit (`chore: release X.Y.Z`), open a pull request, merge it.
6. Tag the merged commit and push the tag:
   ```
   git checkout main && git pull
   git tag -s vX.Y.Z -m "autonometry X.Y.Z"
   git push origin vX.Y.Z
   ```
   The tag must equal `v` plus `__version__`; the workflow fails otherwise.
7. Open the *Release* run under *Actions*. The `build` job tests and builds; the `publish` job waits for approval of the `pypi` environment. Approve it.
8. Check <https://pypi.org/project/autonometry/> shows the new version with attestations ("Verified details" on the file page), then install it in a clean environment: `uvx autonometry@X.Y.Z --version`.
9. Create the GitHub release from the tag and paste the changelog section.

A published version cannot be replaced. If something is wrong, yank it on PyPI and release the next version.
