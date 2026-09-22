import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_lazyvim_configuration_in_headless_neovim(project):
    executable = shutil.which("nvim")
    if executable is None:
        pytest.skip("Neovim is not installed")
    version = subprocess.check_output([executable, "--version"], text=True).splitlines()[0]
    minor = int(version.split("v")[1].split(".")[1])
    if minor < 11:
        pytest.skip("Neovim 0.11+ is required")
    root, write = project
    write("pyproject.toml", "[project]\nname = 'test-project'\nversion = '0.1.0'\n")
    write(
        "models.py",
        "class Model:\n    def __init__(self, width: int, *, bias: bool = True): pass\n",
    )
    write("config.yaml", "model:\n  _target_: models.Model\n  \n")
    write("base.yaml", "model:\n  _target_: models.Model\n  width: 4\n")
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            executable,
            "--headless",
            "-u",
            "NONE",
            "-l",
            str(repo / "tests/neovim.lua"),
            str(root),
            sys.executable,
            str(repo / "examples/lazyvim.lua"),
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
