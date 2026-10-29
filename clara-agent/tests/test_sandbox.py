"""Landlock: the kernel, not Python, refuses writes and TCP connects outside the lists.

Runs in a child process (the lock cannot be lifted once applied). Skipped where
Landlock is unavailable; a skip is reported, never counted as a pass.
"""
import pytest

from clara import sandbox
from clara.sandboxcheck import check_results

pytestmark = pytest.mark.skipif(sandbox.abi_version() < 1, reason="Landlock not available on this kernel")


def test_every_sandbox_check_passes():
    res = check_results()
    assert res["applied"], res.get("reason")
    failed = {k: v for k, v in res["checks"].items() if not v["passed"]}
    assert not failed, failed
    assert "write inside allowed folder" in res["checks"]
    assert res["checks"]["create file in forbidden folder"]["outcome"] == "denied"
    assert res["checks"]["child shell writes in forbidden folder"]["outcome"] == "denied"
    assert res["checks"]["overwrite ~/.clara/config.toml"]["outcome"] == "denied"
    assert res["checks"]["plant a skill in ~/.clara/skills"]["outcome"] == "denied"
    assert res["checks"]["append a memory note in ~/.clara"]["outcome"] == "ok"


def test_tcp_lock_present_on_abi_4_or_newer():
    res = check_results()
    if res["abi"] >= 4:
        assert res["net_locked"]
        assert res["checks"]["tcp connect to other port"]["outcome"] == "denied"
        assert res["checks"]["child curl to other port"]["outcome"] == "denied"
        assert res["checks"]["tcp connect to allowed port"]["outcome"] == "ok"
    else:
        pytest.skip(f"Landlock ABI {res['abi']} has no TCP rules (needs kernel 6.7)")


def test_default_write_paths_include_project_home_and_tmp(tmp_path):
    paths = sandbox.default_write_paths(tmp_path / "p", tmp_path / "h", extra=[str(tmp_path / "x")])
    assert str((tmp_path / "p").resolve()) in paths and "/tmp" in paths and str((tmp_path / "x").resolve()) in paths
