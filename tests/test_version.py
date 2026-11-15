from importlib import metadata

import autonometry


def test_version_string() -> None:
    assert autonometry.__version__ == "0.1.0"


def test_installed_metadata_matches_module_version() -> None:
    assert metadata.version("autonometry") == autonometry.__version__
