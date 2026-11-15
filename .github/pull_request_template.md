## What and why

<!-- What this changes and why. Link the issue it closes. -->

## Spec sections touched

<!-- For example definitions.md 6.12, or "none". Spec changes are proposed in docs/spec-v0.4-proposals.md, not in spec/. -->

## How it was tested

<!-- Commands and results. -->

## Checklist

- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest` pass; coverage stays at 90 % or more
- [ ] Every spec rule touched has a test whose name cites its section (`..._defs_6_8`)
- [ ] CHANGELOG.md (`[Unreleased]`) and the docs are updated where behaviour changed
- [ ] No real robot logs, keys, tokens or personal data in code, tests or this description
- [ ] Nothing new sits in a robot's control loop; missing data stays `unknown`

## Open questions
