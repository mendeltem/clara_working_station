"""Real model, real server, real tool call. The claim that matters.

Runs only when CLARA_TEST_URL points at a running OpenAI-compatible server
(llama-server, Ollama, llama-cpp-python) with a model loaded, e.g.

    CLARA_TEST_URL=http://127.0.0.1:8000/v1 pytest tests/test_real_model.py -s

Skipped otherwise, and a skip is reported as such, never as a pass. Each test
prints the wall time and the model id so the result can be quoted.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

URL = os.environ.get("CLARA_TEST_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set CLARA_TEST_URL to a running model server")
ROOT = Path(__file__).resolve().parent.parent


def run_clara(args, cwd, clara_home, timeout=1200):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["CLARA_HOME"] = str(clara_home)
    t0 = time.time()
    r = subprocess.run([sys.executable, "-m", "clara.cli", "--url", URL, *args], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=timeout)
    r.seconds = time.time() - t0
    return r


@pytest.mark.parametrize("tool_mode", ["prompt", "native"])
def test_model_reads_a_file_through_a_tool_call(project, clara_home, tool_mode):
    r = run_clara(["--tool-mode", tool_mode, "--max-turns", "6", "-p",
                   "Read the file hello.txt and tell me the secret word."], project, clara_home)
    print(f"\n[{tool_mode}] {r.seconds:.0f} s exit {r.returncode}\nstderr: {r.stderr.strip()[-400:]}\nstdout: {r.stdout.strip()[-300:]}")
    assert r.returncode == 0, r.stderr
    assert "read_file(hello.txt)" in r.stderr, "the model did not call read_file"
    assert "PELICAN" in r.stdout


def test_model_writes_a_file_in_accept_edits_mode(project, clara_home):
    r = run_clara(["--mode", "accept-edits", "--max-turns", "8", "-p",
                   "Create a file named note.txt containing exactly the single line: hello from clara"],
                  project, clara_home)
    print(f"\n[write] {r.seconds:.0f} s exit {r.returncode}\nstderr: {r.stderr.strip()[-400:]}\nstdout: {r.stdout.strip()[-300:]}")
    assert r.returncode == 0, r.stderr
    assert "write_file(note.txt)" in r.stderr
    assert (project / "note.txt").read_text().strip() == "hello from clara"


def test_headless_default_mode_refuses_writes_without_a_user(project, clara_home):
    r = run_clara(["--max-turns", "4", "-p", "Create a file named blocked.txt with the text x"], project, clara_home)
    print(f"\n[blocked] {r.seconds:.0f} s exit {r.returncode}\nstderr: {r.stderr.strip()[-400:]}")
    assert not (project / "blocked.txt").exists()
    assert "needs approval and no user is available" in r.stderr or "write_file" not in r.stderr
