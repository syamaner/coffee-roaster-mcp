from coffee_roaster_mcp import __version__
from coffee_roaster_mcp.cli import build_parser, main


def test_version_is_defined() -> None:
    assert __version__ == "0.1.0"


def test_cli_parser_program_name() -> None:
    parser = build_parser()

    assert parser.prog == "coffee-roaster-mcp"


def test_main_returns_success() -> None:
    assert main() == 0
