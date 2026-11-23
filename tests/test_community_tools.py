"""Offline installation contracts for native community channel dependencies."""

import re
from unittest.mock import Mock

import pytest

from agent_reach.runtime import tools
from agent_reach.runtime.result import NEED_SETUP, ReachError


def _private_python(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "tool_dir", lambda name: tmp_path / name)
    python = tmp_path / "aiotieba" / "venv" / ("Scripts/python.exe" if tools._WIN else "bin/python")
    return python


def test_library_without_commands_is_not_present_just_because_all_empty_is_true(tmp_path, monkeypatch):
    python = _private_python(tmp_path, monkeypatch)
    tool = tools.TOOLS["aiotieba"]
    assert tool.commands == ()
    assert tools.present("aiotieba") is False
    assert tools._private_install_ok(tool) is False
    python.parent.mkdir(parents=True)
    python.write_text("interpreter placeholder")
    assert tools.python_for_tool("aiotieba") == str(python)
    # A partly created venv must not count as an installed API library.
    assert tools.present("aiotieba") is False
    assert tools._private_install_ok(tool) is False


def test_library_requires_both_python_and_matching_distribution_metadata(tmp_path, monkeypatch):
    python = _private_python(tmp_path, monkeypatch)
    site = python.parent.parent / ("Lib/site-packages" if tools._WIN else "lib/python3.13/site-packages")
    (site / "aiotieba-4.8.4.dist-info").mkdir(parents=True)
    assert tools.installed_version(tools.TOOLS["aiotieba"]) == "4.8.4"
    assert tools.present("aiotieba") is False
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("interpreter placeholder")
    assert tools.present("aiotieba") is True
    assert tools._private_install_ok(tools.TOOLS["aiotieba"]) is True


def test_python_for_tool_rejects_unknown_and_non_python_tools(tmp_path, monkeypatch):
    _private_python(tmp_path, monkeypatch)
    assert tools.python_for_tool("missing") is None
    assert tools.python_for_tool("opencli") is None
    assert tools.python_for_tool("aiotieba") is None


def test_python_310_reports_setup_requirement_before_creating_or_installing(tmp_path, monkeypatch):
    _private_python(tmp_path, monkeypatch)
    run = Mock(side_effect=AssertionError("must reject before launching installer"))
    monkeypatch.setattr(tools, "_run", run)
    with monkeypatch.context() as context:
        context.setattr(tools.sys, "version_info", (3, 10, 14))
        with pytest.raises(ReachError) as exc:
            tools._install_pip(tools.TOOLS["aiotieba"])
    assert exc.value.code == NEED_SETUP
    assert "3.11" in exc.value.message
    assert not (tmp_path / "aiotieba").exists()
    run.assert_not_called()


def test_community_install_sources_are_reproducible():
    assert tools.install_spec(tools.TOOLS["aiotieba"]) == "aiotieba==4.8.4"
    coolapk = tools.install_spec(tools.TOOLS["coolapk-mcp"])
    assert re.fullmatch(
        r"coolapk-mcp @ https://github\.com/Lniosy/coolapk-mcp/archive/[0-9a-f]{40}\.zip",
        coolapk,
    )
