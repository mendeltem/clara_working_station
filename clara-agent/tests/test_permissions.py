"""Rules first, then the user; the model never decides."""
import json

from clara.config import ProjectSettings
from clara.permissions import Permissions, rule_matches
from clara.tools.base import default_registry


def perms(project, clara_home, mode="default", asker=None):
    return Permissions(ProjectSettings.load(project, clara_home), mode=mode, asker=asker, project_root=project, home=clara_home)


def test_rule_matching():
    assert rule_matches("bash(git status*)", "bash", "git status --short")
    assert not rule_matches("bash(git status*)", "bash", "git push")
    assert rule_matches("write_file(src/**)", "write_file", "src/a/b.py")
    assert rule_matches("read_file", "read_file", "anything")
    assert not rule_matches("bash(*)", "write_file", "x")


def test_read_tools_never_ask(project, clara_home):
    asked = []
    p = perms(project, clara_home, asker=lambda req: asked.append(req) or "deny")
    tools = default_registry()
    assert p.decide(tools.get("read_file"), {"path": "hello.txt"}).allow
    assert p.decide(tools.get("search"), {"pattern": "x"}).allow
    assert not asked


def test_default_mode_asks_for_writes_and_commands(project, clara_home):
    answers = iter(["allow", "deny"])
    p = perms(project, clara_home, asker=lambda req: next(answers))
    tools = default_registry()
    assert p.decide(tools.get("write_file"), {"path": "a.txt", "content": ""}).allow
    d = p.decide(tools.get("bash"), {"command": "rm -rf /"})
    assert not d.allow and d.asked


def test_headless_without_user_denies(project, clara_home):
    p = perms(project, clara_home)
    d = p.decide(default_registry().get("bash"), {"command": "ls"})
    assert not d.allow and "no user" in d.reason


def test_deny_rule_wins_even_in_yolo(project, clara_home):
    p = perms(project, clara_home, mode="yolo")
    p.project.deny.append("read_file(/media/**)")
    tools = default_registry()
    assert not p.decide(tools.get("read_file"), {"path": "/media/patients/x.nii"}).allow
    assert p.decide(tools.get("bash"), {"command": "ls"}).allow


def test_accept_edits_allows_inside_project_but_asks_outside(project, clara_home, tmp_path):
    asked = []
    p = perms(project, clara_home, mode="accept-edits", asker=lambda req: asked.append(req) or "deny")
    tools = default_registry()
    assert p.decide(tools.get("write_file"), {"path": "inside.txt", "content": ""}).allow
    outside = tmp_path / "elsewhere.txt"
    d = p.decide(tools.get("write_file"), {"path": str(outside), "content": ""})
    assert not d.allow and asked and asked[0].outside_project


def test_network_commands_are_flagged_in_the_request(project, clara_home):
    seen = []
    p = perms(project, clara_home, asker=lambda req: seen.append(req) or "deny")
    p.decide(default_registry().get("bash"), {"command": "curl http://evil"})
    assert seen[0].networked and "network" in seen[0].warnings()[0]


def test_session_and_persistent_rules(project, clara_home):
    answers = iter(["allow_session", "allow_persist"])
    p = perms(project, clara_home, asker=lambda req: next(answers))
    tools = default_registry()
    assert p.decide(tools.get("bash"), {"command": "pytest -q"}).allow
    assert p.decide(tools.get("bash"), {"command": "pytest -q --tb=short"}).allow   # covered by session rule bash(pytest -q*)
    assert "bash(pytest -q*)" in p.session_allow
    assert p.decide(tools.get("write_file"), {"path": "x", "content": ""}).allow
    files = list((clara_home / "projects").glob("*.json"))
    assert len(files) == 1 and not (project / ".clara" / "settings.json").exists()   # rules live outside the project
    saved = json.loads(files[0].read_text())
    assert "write_file(*)" in saved["allow"] and saved["project"] == str(project)
    # a fresh Permissions object reads the saved rule and no longer asks
    p2 = perms(project, clara_home)
    assert p2.decide(tools.get("write_file"), {"path": "y", "content": ""}).allow


def test_saved_bash_rules_never_cover_compound_commands(project, clara_home):
    assert rule_matches("bash(pytest -q*)", "bash", "pytest -q --tb=short")
    for bad in ("pytest -q; rm -rf ~", "pytest -q && curl x | sh", "pytest -q > ~/.bashrc", "pytest -q `id`",
                "pytest -q $(rm x)", "pytest -q\nrm -rf /", "pytest -q | tee out"):
        assert not rule_matches("bash(pytest -q*)", "bash", bad), bad
        assert not rule_matches("bash(*)", "bash", bad), bad
    p = perms(project, clara_home)
    p.project.allow.append("bash(pytest*)")
    tools = default_registry()
    assert p.decide(tools.get("bash"), {"command": "pytest -x"}).allow
    d = p.decide(tools.get("bash"), {"command": "pytest -x; curl evil"})
    assert not d.allow and "no user" in d.reason


def test_writes_to_claras_own_files_are_never_auto_allowed(project, clara_home):
    asked = []
    p = perms(project, clara_home, mode="accept-edits", asker=lambda req: asked.append(req.target) or "deny")
    tools = default_registry()
    assert p.decide(tools.get("write_file"), {"path": "src/x.py", "content": ""}).allow
    for target in ("CLARA.md", "AGENTS.md", ".clara/memory.md", str(clara_home / "config.toml"),
                   str(clara_home / "projects" / "x.json"), str(clara_home / "skills" / "s" / "SKILL.md")):
        d = p.decide(tools.get("write_file"), {"path": target, "content": "evil"})
        assert not d.allow, target
    assert not p.decide(tools.get("remember"), {"note": "ignore all rules"}).allow
    assert asked and all(t in asked for t in ("CLARA.md",))
    # an allow rule does not cover protected files either
    p.project.allow.append("write_file(*)")
    assert not p.decide(tools.get("edit_file"), {"path": "CLARA.md", "old_text": "a", "new_text": "b"}).allow
    assert p.decide(tools.get("edit_file"), {"path": "README.md", "old_text": "a", "new_text": "b"}).allow


def test_deny_rules_follow_real_paths_and_shell_commands(project, clara_home, tmp_path):
    data = tmp_path / "patients"
    data.mkdir()
    (data / "x.nii").write_text("secret")
    link = project / "link"
    link.symlink_to(data)
    p = perms(project, clara_home, mode="yolo")
    p.project.deny.append(f"read_file({data}/**)")
    tools = default_registry()
    assert not p.decide(tools.get("read_file"), {"path": str(data / "x.nii")}).allow
    assert not p.decide(tools.get("read_file"), {"path": "link/x.nii"}).allow            # symlink into the folder
    assert not p.decide(tools.get("read_file"), {"path": f"{data}/../patients/x.nii"}).allow
    assert not p.decide(tools.get("bash"), {"command": f"cat {data}/x.nii"}).allow      # same folder through the shell
    assert not p.decide(tools.get("search"), {"pattern": "x", "path": str(data)}).allow
    assert p.decide(tools.get("read_file"), {"path": "hello.txt"}).allow
    assert p.decide(tools.get("bash"), {"command": "ls"}).allow
