"""Runs the jsdom behaviour test of the phone page when node and jsdom are available.

    cd tests && npm install        # once, installs jsdom from tests/package.json

Skipped (and reported as skipped) otherwise; the API behind the page is tested
in test_server_api.py without node.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_page_in_jsdom():
    if not (HERE / "node_modules" / "jsdom").exists():
        pytest.skip("jsdom not installed (cd tests && npm install)")
    r = subprocess.run(["node", str(HERE / "app_page_test.mjs")], capture_output=True, text=True, timeout=120, cwd=HERE)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all checks passed" in r.stdout
