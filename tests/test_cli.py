import pytest
from typer.testing import CliRunner

from autonometry import __version__, cli, spec

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"autonometry {__version__}"


def test_spec_list() -> None:
    result = runner.invoke(cli.app, ["spec", "list"])
    assert result.exit_code == 0
    assert result.output.split() == ["0.2"]


def test_spec_show_prints_names_with_hashes__defs_8() -> None:
    result = runner.invoke(cli.app, ["spec", "show", "0.2"])
    assert result.exit_code == 0
    lines = [line.split("  ") for line in result.output.strip().splitlines()]
    assert [name for _, name in lines] == ["definitions.md", "event.schema.json", "rules.toml"]
    assert all(len(digest) == 64 for digest, _ in lines)


def test_spec_show_unknown_version_is_one_line_exit_1() -> None:
    result = runner.invoke(cli.app, ["spec", "show", "9.9"])
    assert result.exit_code == 1
    assert result.output.strip().splitlines() == [
        "error: unknown spec version '9.9'; available: 0.2"
    ]


def test_spec_show_path_like_version_is_exit_1() -> None:
    result = runner.invoke(cli.app, ["spec", "show", "../.."])
    assert result.exit_code == 1
    assert result.output.startswith("error: invalid spec version")


def test_spec_list_failure_is_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> list[str]:
        raise spec.SpecError("specification files not found")

    monkeypatch.setattr(cli, "available_versions", broken)
    result = runner.invoke(cli.app, ["spec", "list"])
    assert result.exit_code == 1
    assert result.output.strip() == "error: specification files not found"


@pytest.mark.parametrize("args", [["spec", "show"], ["nonsense"], ["--no-such-flag"]])
def test_bad_usage_is_exit_2(args: list[str]) -> None:
    assert runner.invoke(cli.app, args).exit_code == 2


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(cli.app, [])
    assert "spec" in result.output


def test_main_turns_unexpected_failure_into_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> None:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    assert capsys.readouterr().err.strip() == "error: unexpected failure (RuntimeError)"


def test_main_passes_through_normal_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["autonometry", "--version"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
