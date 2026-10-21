"""External edit diagnostics respect the effective operator profile, without disabling parsers."""
from unittest.mock import Mock

import pytest

from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from tools.file_operations import ShellFileOperations
from tools.file_operations_common import ExecuteResult


@pytest.fixture
def ops(monkeypatch):
    obj = ShellFileOperations(Mock(cwd="/workspace"))
    monkeypatch.setattr(obj, "_lsp_local_only", lambda: False)
    monkeypatch.setattr(obj, "_exec", Mock(return_value=ExecuteResult(stdout="", exit_code=0)))
    monkeypatch.setattr(obj, "_has_command", Mock(return_value=True))
    return obj


@pytest.mark.parametrize("extension", ["ts", "js", "rs", "go"])
def test_edit_only_skips_shell_checks(ops, monkeypatch, extension):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"file_external_diagnostics": False})
    result = ops._check_lint("/workspace/file." + extension, content="example")
    assert result.skipped and not result.success
    assert "disabled" in result.message.lower()
    ops._exec.assert_not_called()
    ops._has_command.assert_not_called()


@pytest.mark.parametrize("value", [None, "false", "true", 0, 1, [], {}])
def test_invalid_policy_never_enables_commands(ops, monkeypatch, value):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"file_external_diagnostics": value})
    assert ops._check_lint("/workspace/a.js", content="anything").skipped
    ops._exec.assert_not_called()


def test_unreadable_policy_skips_external_checks(ops, monkeypatch):
    def broken():
        raise OSError("config unavailable")
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", broken)
    assert ops._check_lint("/workspace/a.js", content="anything").skipped
    ops._exec.assert_not_called()


@pytest.mark.parametrize("config", [{}, {"file_external_diagnostics": True}])
def test_ordinary_sessions_keep_shell_checks(ops, monkeypatch, config):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    assert ops._check_lint("/workspace/a.js", content="anything").success
    ops._exec.assert_called_once()
    assert "node --check" in ops._exec.call_args.args[0]


@pytest.mark.parametrize("path,content,success", [("x.py", "x = 1", True), ("x.py", "if (", False), ("x.json", '{"a":1}', True), ("x.json", "{", False), ("x.toml", "a=1", True)])
def test_inprocess_syntax_checks_remain(ops, monkeypatch, path, content, success):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"file_external_diagnostics": False})
    result = ops._check_lint(path, content=content)
    assert result.success is success
    assert not result.skipped
    ops._exec.assert_not_called()


def test_disabled_policy_never_acquires_lsp_service(ops, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"file_external_diagnostics": False})
    monkeypatch.setattr(ops, "_lsp_local_only", lambda: True)
    service = Mock()
    monkeypatch.setattr("agent.lsp.get_service", service)
    assert not ops._lsp_handles_extension(".ts")
    ops._snapshot_lsp_baseline("/workspace/a.ts")
    assert ops._maybe_lsp_diagnostics("/workspace/a.ts") == ""
    service.assert_not_called()


def test_cached_operations_follow_effective_profile(ops, tmp_path, monkeypatch):
    launch = tmp_path / "launch"; launch.mkdir()
    worker = tmp_path / "worker"; worker.mkdir()
    (launch / "config.yaml").write_text("file_external_diagnostics: true\n")
    (worker / "config.yaml").write_text("file_external_diagnostics: false\n")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    for home, skipped in [(launch, False), (worker, True), (launch, False), (worker, True)]:
        token = set_hermes_home_override(home)
        try:
            result = ops._check_lint("/workspace/a.js", content="example")
            assert result.skipped is skipped
        finally:
            reset_hermes_home_override(token)
    assert ops._exec.call_count == 2


@pytest.mark.platforms("posix")
def test_real_write_and_patch_keep_integrity_and_json_refusal(tmp_path, monkeypatch):
    from tests.tools.test_file_operations import make_real_subprocess_env
    from tools.registry import registry
    from tools import file_tools
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"file_external_diagnostics": False})
    obj = ShellFileOperations(make_real_subprocess_env(str(tmp_path)))
    monkeypatch.setattr(obj, "_lsp_local_only", lambda: True)
    # Any accidental external diagnostics are inert sentinels, not project execution.
    lint = Mock(return_value=ExecuteResult(stdout="", exit_code=0))
    monkeypatch.setattr(obj, "_run_managed_node_linter", lint)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": obj)
    path = tmp_path / "a.js"
    write = registry.dispatch("write_file", {"path": str(path), "content": "const x = 1;\n"}, task_id="diagnostic-test")
    import json
    assert not json.loads(write).get("error"), write
    assert path.read_text() == "const x = 1;\n"
    patched = registry.dispatch("patch", {"path": str(path), "old_string": "x = 1", "new_string": "x = 2"}, task_id="diagnostic-test")
    assert not json.loads(patched).get("error"), patched
    assert path.read_text() == "const x = 2;\n"
    lint.assert_not_called()
    invalid = tmp_path / "bad.json"
    assert obj.write_file(str(invalid), "{").error
    assert not invalid.exists()


def test_failed_config_fallback_does_not_enable_diagnostics(ops, monkeypatch):
    from hermes_cli.config_read_errors import FailedConfigRead
    fallback = FailedConfigRead({"file_external_diagnostics": True}, error=ValueError("invalid"))
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: fallback)
    assert ops._check_lint("/workspace/a.js", content="example").skipped
    ops._exec.assert_not_called()
