"""Smoke install the built RoastPilot wheel in a clean virtual environment."""

import argparse
import os
import shutil
import subprocess
import venv
from pathlib import Path

EXPECTED_DEFAULT_CONFIG = "mock disabled int8"


def main() -> int:
    """Run the built-wheel install smoke check."""
    parser = argparse.ArgumentParser(
        description="Install the built coffee-roaster-mcp wheel and run CLI/config smokes."
    )
    parser.add_argument(
        "--dist-dir",
        default="dist",
        type=Path,
        help="Directory containing built distribution artifacts.",
    )
    parser.add_argument(
        "--venv-path",
        default=Path("/tmp/coffee-roaster-mcp-wheel-smoke"),
        type=Path,
        help="Virtual environment path to recreate for the smoke install.",
    )
    args = parser.parse_args()

    dist_dir = args.dist_dir.resolve()
    venv_path = args.venv_path.resolve()
    wheel = _find_single_wheel(dist_dir)
    _recreate_venv(venv_path)

    python = _venv_bin(venv_path) / "python"
    coffee_roaster_mcp = _venv_bin(venv_path) / "coffee-roaster-mcp"

    _run([python, "-m", "pip", "install", "--upgrade", "pip"])
    _run([python, "-m", "pip", "install", str(wheel)])
    _run([coffee_roaster_mcp, "--help"])
    _run([coffee_roaster_mcp, "--version"])
    _assert_installed_default_config(python)

    return 0


def _find_single_wheel(dist_dir: Path) -> Path:
    wheels = sorted(dist_dir.glob("coffee_roaster_mcp-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"Expected exactly one built wheel in {dist_dir}, found {len(wheels)}")
    return wheels[0]


def _recreate_venv(venv_path: Path) -> None:
    shutil.rmtree(venv_path, ignore_errors=True)
    venv.create(venv_path, with_pip=True)


def _venv_bin(venv_path: Path) -> Path:
    if os.name == "nt":
        return venv_path / "Scripts"
    return venv_path / "bin"


def _run(command: list[Path | str]) -> None:
    subprocess.run([str(part) for part in command], check=True)


def _assert_installed_default_config(python: Path) -> None:
    code = f"""
import os
import tempfile

from coffee_roaster_mcp.config import load_config

expected = {EXPECTED_DEFAULT_CONFIG!r}

with tempfile.TemporaryDirectory() as tmpdir:
    os.chdir(tmpdir)
    config = load_config(environ={{}})
    output = (
        f"{{config.roaster.driver}} "
        f"{{config.first_crack.mode}} "
        f"{{config.first_crack.precision}}"
    )

if output != expected:
    raise SystemExit(f"Expected {{expected!r}}, got {{output!r}}")

print(output)
"""
    _run([python, "-c", code])


if __name__ == "__main__":
    raise SystemExit(main())
