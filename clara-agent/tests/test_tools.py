"""The tools behave as documented: real files, real shell, real timeouts."""
import os
import time

import pytest

from clara.tools.base import ToolContext, default_registry
from clara.tools.shell import looks_networked


@pytest.fixture
def ctx(project, clara_home):
    return ToolContext(project_root=project, home=clara_home, session_id="t1")


def run(name, args, ctx):
    return default_registry().get(name).run(args, ctx)


def test_read_file_numbers_lines_and_pages(ctx):
    r = run("read_file", {"path": "hello.txt"}, ctx)
    assert not r.is_error and r.text.startswith("1\tThe secret word is PELICAN.")
    r = run("read_file", {"path": "hello.txt", "offset": 2, "limit": 1}, ctx)
    assert r.text == "2\tSecond line."


def test_read_missing_and_binary(ctx, project):
    assert run("read_file", {"path": "nope.txt"}, ctx).is_error
    (project / "blob.bin").write_bytes(bytes(range(256)) * 10)
    assert "binary" in run("read_file", {"path": "blob.bin"}, ctx).text


def test_write_creates_dirs_and_backs_up_existing(ctx, project, clara_home):
    r = run("write_file", {"path": "new/dir/file.txt", "content": "abc"}, ctx)
    assert not r.is_error and (project / "new/dir/file.txt").read_text() == "abc"
    run("write_file", {"path": "hello.txt", "content": "changed"}, ctx)
    backups = list((clara_home / "backups" / "t1").glob("*hello.txt"))
    assert len(backups) == 1 and backups[0].read_text().startswith("The secret word")
    run("write_file", {"path": "hello.txt", "content": "changed again"}, ctx)
    assert len(list((clara_home / "backups" / "t1").glob("*hello.txt"))) == 1   # once per session


def test_edit_requires_unique_match(ctx, project):
    r = run("edit_file", {"path": "src/app.py", "old_text": "return a + b", "new_text": "return a - b"}, ctx)
    assert not r.is_error and "return a - b" in (project / "src/app.py").read_text()
    assert run("edit_file", {"path": "src/app.py", "old_text": "missing", "new_text": "x"}, ctx).is_error
    (project / "dup.txt").write_text("x\nx\n")
    assert run("edit_file", {"path": "dup.txt", "old_text": "x", "new_text": "y"}, ctx).is_error
    r = run("edit_file", {"path": "dup.txt", "old_text": "x", "new_text": "y", "replace_all": True}, ctx)
    assert not r.is_error and (project / "dup.txt").read_text() == "y\ny\n"


def test_list_and_search(ctx):
    r = run("list_files", {"pattern": "**/*.py"}, ctx)
    assert "src/app.py" in r.text and "hello.txt" not in r.text
    r = run("search", {"pattern": r"def \w+", "glob": "*.py"}, ctx)
    assert "src/app.py:1: def add(a, b):" in r.text
    assert run("search", {"pattern": "(", "glob": "*.py"}, ctx).is_error


def test_bash_runs_in_project_and_reports_exit_code(ctx):
    r = run("bash", {"command": "pwd && echo hi && exit 3"}, ctx)
    assert r.is_error and r.data["exit_code"] == 3 and "hi" in r.text and str(ctx.project_root) in r.text


def test_bash_does_not_hang_on_stdin(ctx):
    t0 = time.time()
    r = run("bash", {"command": "cat"}, ctx)        # would wait for input with an open stdin
    assert time.time() - t0 < 5 and r.data["exit_code"] == 0


def test_bash_timeout_kills_the_process_group(ctx):
    t0 = time.time()
    r = run("bash", {"command": "sleep 30 & sleep 30", "timeout": 1}, ctx)
    assert r.is_error and "killed" in r.text and time.time() - t0 < 10


def test_network_command_detection():
    assert looks_networked("curl http://x")
    assert looks_networked("pip install requests")
    assert looks_networked("git push origin main")
    assert looks_networked("cd a && wget x")
    assert not looks_networked("git status")
    assert not looks_networked("pytest -q")
    assert not looks_networked("echo curling")


def test_remember_and_todo(ctx, project):
    r = run("remember", {"note": "tests live in tests/"}, ctx)
    assert "memory.md" in r.text and "tests live in tests/" in (project / ".clara/memory.md").read_text()
    r = run("todo", {"items": [{"text": "a", "status": "done"}, {"text": "b", "status": "pending"}]}, ctx)
    assert r.text == "[x] a\n[ ] b" and ctx.todo_state[1]["text"] == "b"


def test_list_files_default_pattern_finds_root_files(tmp_path):
    from clara.tools.base import ToolContext, default_registry
    (tmp_path / "top.py").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.py").write_text("x")
    tool = default_registry().get("list_files")
    ctx = ToolContext(project_root=tmp_path, home=tmp_path / "home", session_id="t2")
    for pattern in (None, "**/*.py"):
        out = tool.run({} if pattern is None else {"pattern": pattern}, ctx).text
        assert "top.py" in out and "deep.py" in out
