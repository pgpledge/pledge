import subprocess
import sys
from pathlib import Path

EXAMPLE_DIR = Path(__file__).resolve().parent


def test_example_project():
    # Django settings are per process and python/tests configures its own.
    # The file name keeps `pytest example` from collecting this wrapper too.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(EXAMPLE_DIR)],
        cwd=EXAMPLE_DIR.parent,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
