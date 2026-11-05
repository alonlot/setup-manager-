"""The command line: argument parsing and error reporting (no agent needed)."""
import json

import pytest

from setup_manager import cli


def run_cli(monkeypatch, argv, record=None):
    """Run main() with the command function replaced; returns (namespace or None, exit code)."""
    got = {}
    if record:
        monkeypatch.setattr(cli, record, lambda a: got.update(vars(a)))
    try:
        cli.main(argv)
    except SystemExit as e:
        return got, e.code
    return got, 0


@pytest.mark.parametrize("argv", [
    ["exec", "--timeout", "5", "bench", "uptime"],
    ["exec", "bench", "--timeout", "5", "uptime"],
])
def test_exec_timeout_before_or_after_the_setup(tmp_path, monkeypatch, argv):
    got, rc = run_cli(monkeypatch, ["--config", str(tmp_path / "c.json")] + argv, "cmd_exec")
    assert rc == 0 and got["timeout"] == 5.0 and got["command"] == ["uptime"]


def test_exec_double_dash_and_missing_command(tmp_path, monkeypatch, capsys):
    cfg = ["--config", str(tmp_path / "c.json")]
    got, rc = run_cli(monkeypatch, cfg + ["exec", "all", "--", "uptime", "-a"], "cmd_exec")
    assert rc == 0 and got["command"] == ["uptime", "-a"] and got["timeout"] == 60
    got, rc = run_cli(monkeypatch, cfg + ["exec", "all", "--timeout", "5"], "cmd_exec")
    assert rc == 2 and "exec needs a command" in capsys.readouterr().err


def test_flash_env_needs_name_equals_value(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"setups": [{"id": "b", "host": "127.0.0.1", "port": 1, "token": "x"}]}))
    _, rc = run_cli(monkeypatch, ["--config", str(cfg), "flash", "b", "1-1", "fw.bin", "--env", "FOO"])
    assert rc == 2 and "NAME=VALUE" in capsys.readouterr().err


def test_errors_are_one_line_not_a_traceback(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"setups": [{"id": "b", "host": "127.0.0.1", "port": 1, "token": "x"}]}))
    _, rc = run_cli(monkeypatch, ["--config", str(cfg), "remove", "nope"])
    assert rc == 1 and capsys.readouterr().err.startswith("error: ")
    _, rc = run_cli(monkeypatch, ["--config", str(cfg), "identify", "b", "1-1", "--script", str(tmp_path / "no.sh")])
    assert rc == 1 and capsys.readouterr().err.startswith("error: ")
    # An agent that cannot be reached: "error: cannot reach ...", not an AgentError traceback.
    _, rc = run_cli(monkeypatch, ["--config", str(cfg), "usb", "b"])
    err = capsys.readouterr().err
    assert rc == 1 and err.startswith("error: ") and "cannot reach" in err


def test_config_help_names_the_default(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "setup-manager/config.json" in out and "None" not in out.split("--config")[1].split("\n")[0]


@pytest.mark.parametrize("flag", ["--open-usbip", "--usbip-filter", "--tunnel-only"])
def test_deploy_passes_the_usbip_mode(tmp_path, monkeypatch, flag):
    got, rc = run_cli(monkeypatch, ["--config", str(tmp_path / "c.json"), "deploy", "dev@pc", flag], "cmd_deploy")
    assert rc == 0 and got["usbip_mode"] == flag


def test_deploy_usbip_modes_exclude_each_other(tmp_path, monkeypatch):
    _, rc = run_cli(monkeypatch, ["--config", str(tmp_path / "c.json"), "deploy", "dev@pc", "--open-usbip",
                                  "--tunnel-only"], "cmd_deploy")
    assert rc == 2


def test_the_agent_default_leaves_usbip_open():
    from setup_manager.agent import setup_agent as sa
    assert sa.DEFAULTS["usbip_network"] == "open"
