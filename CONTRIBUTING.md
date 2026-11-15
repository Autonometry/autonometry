# Contributing to Autonometry

Thank you for helping. Autonometry measures how much of a robot's work its AI does and how much
people do, so correctness and honesty about uncertainty matter more than features. This page says
how to set up, what every change must meet, and how to propose a change to the specification.

- **General questions:** write to aaryan.solanki@thestatsigma.com.
- **Bugs and ideas:** open an [issue](https://github.com/Autonometry/autonometry/issues/new/choose)
  with the matching template.
- **Security problems:** never in an issue, a pull request or by email. Use GitHub private
  vulnerability reporting as [SECURITY.md](SECURITY.md) says.
- **Conduct:** everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).

Never put real robot logs, keys, tokens or personal data in an issue, a pull request or a test
fixture. Test data is synthetic, and actor ids are pseudonyms.

## Setting up

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). On Windows, run the shell
scripts in Git Bash.

```
git clone https://github.com/Autonometry/autonometry.git
cd autonometry
uv sync --all-extras --dev
uv run autonometry --help
uv run autonometry demo --out "$(mktemp -d)/demo"
```

The recorder and the live connectors run on Linux and macOS; everything else, and the test suite,
also runs on Windows (tests that need POSIX sockets are skipped there).

| Command | What it checks |
|---|---|
| `uv run ruff check . && uv run ruff format --check .` | lint and formatting |
| `uv run mypy src` | strict typing of the package |
| `uv run pytest` | fast tests, with coverage of `src/autonometry` at 90 % or more |
| `uv run pytest -m slow --no-cov` | slow statistical checks (coverage of the intervals, memory) |
| `uv run --no-sync pytest -m dds --no-cov tests/dds` | the Unitree G1 watcher over Cyclone DDS 0.10.5, built as in [docs/connectors/unitree-g1.md](docs/connectors/unitree-g1.md) |
| `uv build --out-dir dist && scripts/smoke_wheel.sh dist` | the wheel installed outside the checkout: `--version`, `spec show`, `demo` |

CI runs all of them on Linux (x86-64 and ARM64), macOS and Windows, plus the TurtleBot 4 checks in
a ROS 2 Jazzy container, the lowest allowed dependency versions, `pip-audit` and `zizmor`.

## The rules the code follows

These override convenience:

1. **Observer only.** Nothing we ship sits in a robot's control loop, slows it or stops it. A call
   from robot code into the client library returns at once and never raises.
2. **Fail visible.** Missing data becomes `unknown` time with bounds, never AI time.
3. **Fail closed.** A verifier that cannot prove a log intact says so.
4. **Deterministic.** The same log and parameters give the same numbers and the same JSON report
   (apart from its generated-at time): seeded resampling, sorted output, no network and no AI
   models in measurement or reporting.
5. **Privacy.** Control metadata only: no video, audio or real names; actor ids are pseudonyms.

The specification in `spec/<version>/` is the source of truth: `definitions.md` holds the rules,
`rules.toml` the tables that code reads (never hard-code them). If code and spec disagree, the spec
wins. `research/` is design-phase material, for reference only: never import from it.

Code is small typed functions with docstrings that state units and the spec section they
implement. Measurement is pure functions; input and output happen only at the edges (command line,
recorder, connectors). All external input is validated at the boundary, and bad input gives a flag
or a one-line error, never a traceback. No `eval`, `exec`, `pickle`, `shell=True` or home-made
cryptography. A new dependency needs a reason in the pull request.

## Definition of done

A pull request is ready when:

- `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest` pass,
  and coverage of `src/autonometry` stays at 90 % or more;
- every spec rule it touches has a test whose name cites the section, for example
  `test_human_pause_is_shared__defs_6_8` (security findings: `__s55`);
- parsers, the hash chain and the timeline keep their property tests (hypothesis): random or
  corrupted input never crashes, seal-then-verify always passes, any change to a sealed log fails
  verification;
- fixed input logs with expected outputs live under `tests/golden/`, and slow statistical checks
  under `tests/slow/` behind the `slow` marker;
- [CHANGELOG.md](CHANGELOG.md) (Keep a Changelog, under `[Unreleased]`) and the docs are updated
  when behaviour changes;
- the description says what was built, how it was tested (commands and results), open questions,
  and anything skipped.

## Commits and pull requests

One topic per branch and pull request, in small commits with
[Conventional Commits](https://www.conventionalcommits.org/) messages: `feat:`, `fix:`, `test:`,
`docs:`, `ci:`, `refactor:`, `chore:`, with a scope where it helps (`fix(g1): ...`). Write the
subject as what the change does, in the present tense. Pull requests go to `main`; CI must be
green before review.

## Proposing a change to the specification

A released spec version never changes; changes go into the next version. They are collected in
[docs/spec-v0.4-proposals.md](docs/spec-v0.4-proposals.md) until v0.4 is written.

1. Open an issue with the **Spec change** template: the section, the case the current rules get
   wrong or leave open, and an example log if you can.
2. Once the issue is discussed, open a pull request that adds a numbered section to
   `docs/spec-v0.4-proposals.md` with **Problem** (what goes wrong today, with the section),
   **Proposal** (the exact text to add or replace in `definitions.md`, and any table in
   `rules.toml` or field in a schema), and **v0.1 meanwhile** (what the software does until the
   new version, if anything).
3. Do not edit files under `spec/` in that pull request. If the software implements the proposal
   ahead of the spec, the code says so where it goes beyond the current version, and the tests cite
   the proposal.
