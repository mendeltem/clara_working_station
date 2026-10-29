"""Command line, prompt size, skills, scheduler."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from clara.config import load_settings
from clara.prompts import CORE, build_system_prompt
from clara.scheduler import parse_interval, run_due, save_tasks
from clara.skills import discover_skills
from clara.tools.base import default_registry

ROOT = Path(__file__).resolve().parent.parent


def run_cli(args, cwd, env_extra=None, stdin=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "clara.cli", *args], cwd=cwd, env=env, capture_output=True,
                          text=True, input=stdin, timeout=120)


def test_version_and_help(project, clara_home):
    assert "clara 0." in run_cli(["--version"], project).stdout
    assert "sandbox-check" in run_cli(["--help"], project).stdout


def test_one_shot_mock(project, clara_home):
    r = run_cli(["--mock", "-p", "show me the project"], project, {"CLARA_HOME": str(clara_home)})
    assert r.returncode == 0 and "demonstration" in r.stdout and "list_files" in r.stderr
    assert (clara_home / "sessions").glob("*.jsonl")


def test_repl_mock_with_piped_input(project, clara_home):
    r = run_cli(["--mock", "--no-record"], project, {"CLARA_HOME": str(clara_home)},
                stdin="/status\n/tools\nlist the files\n/exit\n")
    assert r.returncode == 0 and "Clara 0." in r.stdout and "read_file" in r.stdout and "demonstration" in r.stdout
    assert not list((clara_home / "sessions").glob("*.jsonl"))


def test_doctor_without_server_reports_the_problem(project, clara_home):
    r = run_cli(["doctor", "--url", "http://127.0.0.1:9/v1"], project, {"CLARA_HOME": str(clara_home)})
    assert r.returncode == 1 and "cannot reach" in r.stdout and "problems found" in r.stdout


def test_doctor_against_fake_server(project, clara_home, fake_llama):
    r = run_cli(["doctor", "--url", fake_llama.base_url], project, {"CLARA_HOME": str(clara_home)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "llama-server" in r.stdout and "8192 tokens" in r.stdout and "result      ok" in r.stdout


def test_doctor_probe_measures_tool_calling(project, clara_home, fake_llama):
    fake_llama.script = [{"tool_calls": [{"name": "list_files", "arguments": {}}]}]
    r = run_cli(["doctor", "--probe", "--url", fake_llama.base_url], project, {"CLARA_HOME": str(clara_home)})
    assert "tool call received as native tool_calls" in r.stdout and "measured via /tokenize" in r.stdout


def test_audit_and_sessions_commands(project, clara_home):
    run_cli(["--mock", "-p", "x"], project, {"CLARA_HOME": str(clara_home)})
    r = run_cli(["sessions"], project, {"CLARA_HOME": str(clara_home)})
    assert "msgs" in r.stdout
    r = run_cli(["audit"], project, {"CLARA_HOME": str(clara_home)})
    assert r.returncode == 0


def test_system_prompt_stays_small(project, clara_home):
    prompt = build_system_prompt(project, clara_home, [], default_registry().schemas(), prompt_mode_tools=False)
    words = len(prompt.split())
    assert words < 200, f"core prompt grew to {words} words"
    full = build_system_prompt(project, clara_home, [], default_registry().schemas(), prompt_mode_tools=True)
    # Estimate (3.6 chars per token). The exact count is measured on a real server by `clara doctor --probe`.
    assert len(full) / 3.6 < 1500, f"prompt-mode system prompt estimated at {len(full) / 3.6:.0f} tokens"
    assert len(json.dumps(default_registry().schemas())) / 3.6 < 1200


def test_project_instructions_memory_and_skills_enter_the_prompt(project, clara_home):
    (project / "CLARA.md").write_text("Always run pytest -q before answering.\n")
    (project / ".clara").mkdir()
    (project / ".clara" / "memory.md").write_text("- 2026-10-25: tests are in tests/\n")
    sk = clara_home / "skills" / "release"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: release\ndescription: how to cut a release\n---\n# Release\nsteps\n")
    skills = discover_skills([clara_home / "skills"])
    assert skills == [{"name": "release", "description": "how to cut a release", "path": str(sk / "SKILL.md")}]
    prompt = build_system_prompt(project, clara_home, skills, None, False)
    assert "Always run pytest -q" in prompt and "tests are in tests/" in prompt and "release: how to cut a release" in prompt
    assert prompt.startswith(CORE)


def test_scheduler(clara_home):
    assert parse_interval("30m") == 1800 and parse_interval("2h") == 7200 and parse_interval("1d") == 86400
    settings = load_settings()
    save_tasks(settings.schedules_file, [{"id": "a", "prompt": "p", "interval": 60, "mode": "default", "next_run": time.time() - 1},
                                         {"id": "b", "prompt": "q", "interval": 60, "mode": "default", "next_run": time.time() + 3600}])
    ran = []
    out = run_due(settings, runner=lambda s, t: ran.append(t["id"]))
    assert ran == ["a"] and [t["id"] for t in out] == ["a"]
    assert run_due(settings, runner=lambda s, t: ran.append(t["id"])) == []
