"""`setup-manager rollout` against "remote" PCs simulated with fake ssh/scp/sudo and real agents."""
import asyncio
import io
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import AGENT, FAKES, TOKEN, agent_url, build_sysfs, insecure_ctx, start_agent, stop_agent
from setup_manager.config import ConfigStore
from setup_manager.fleet import Fleet
from setup_manager.rollout import Options, Rollout, bundled_version


@pytest.fixture
def remote(tmp_path, agent, manager_config, monkeypatch):
    """The fixture agent plays a remote PC "127.0.0.1"; a second agent plays "localhost"."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ssh").symlink_to(FAKES / "fake_ssh.py")
    (bindir / "scp").symlink_to(FAKES / "fake_scp.py")
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.log"))
    second_dir = tmp_path / "pc2"
    second_dir.mkdir()
    second = start_agent(second_dir, build_sysfs(second_dir / "sys"))
    agents = {}
    for host, info in (("127.0.0.1", agent), ("localhost", second)):
        pidfile = info["config"].parent / "agent.pid"
        pidfile.write_text(str(info["proc"].pid))
        agents[host] = {"config": str(info["config"]), "pidfile": str(pidfile), "script": str(AGENT),
                        "sysfs": str(info["sysfs"])}
    monkeypatch.setenv("FAKE_AGENTS", json.dumps(agents))
    data = json.loads(manager_config.read_text())
    data["setups"].append({"id": "bench2", "name": "Bench 2", "host": "localhost", "port": second["port"],
                           "token": TOKEN})
    data["agent_defaults"] = {"token": "old-shared-token"}
    manager_config.write_text(json.dumps(data))
    yield {"agents": agents, "first": agent, "second": second, "log": tmp_path / "ssh.log"}
    for a in agents.values():  # the agents that the fake install started
        try:
            os.kill(int(Path(a["pidfile"]).read_text()), 15)
        except (OSError, ValueError):
            pass
    stop_agent(second)


def opts(**kw):
    out = io.StringIO()
    base = dict(ssh_user="dev", yes=True, out=out, ask=lambda p: "", ask_secret=lambda p: "pw")
    base.update(kw)
    return Options(**base), out


def agent_accepts(info, token):
    req = urllib.request.Request(agent_url(info, "/api/status"), headers={"Authorization": f"Bearer {token}"})
    try:
        urllib.request.urlopen(req, timeout=5, context=insecure_ctx()).read()
        return True
    except urllib.error.HTTPError as e:
        assert e.code == 401
        return False


def test_full_rollout(manager_config, remote, monkeypatch):
    monkeypatch.setenv("FAKE_SUDO_PASSWORD", "pw")

    async def attach():
        async with Fleet(ConfigStore(manager_config)) as f:
            await f.attach("bench", "1-2")
    asyncio.run(attach())

    o, out = opts()
    rc = Rollout(ConfigStore(manager_config), o).run()
    text = out.getvalue()
    assert rc == 0, text
    cfg = ConfigStore(manager_config)
    b1, b2 = cfg.get_setup("bench"), cfg.get_setup("bench2")
    # A new, unique, random token on each PC; the old one does not work anymore.
    assert b1["token"] != TOKEN and b2["token"] != TOKEN and b1["token"] != b2["token"]
    assert len(b1["token"]) >= 40
    assert agent_accepts(remote["first"], b1["token"]) and not agent_accepts(remote["first"], TOKEN)
    assert agent_accepts(remote["second"], b2["token"])
    # Pinned certificates (read over "SSH"), the SSH target is remembered, encryption is required now.
    assert len(b1["fingerprint"]) == 64 and b1["transport"] == "tls" and b1["ssh"] == "dev@127.0.0.1"
    assert cfg.data()["require_encryption"] is True
    assert cfg.data()["agent_defaults"]["token"] == ""
    assert cfg.data()["rollout_pending"] == {}
    # The device that was attached here is attached again (with the new token, through the tunnel).
    assert "attached again: 1-2" in text

    async def check():
        async with Fleet(ConfigStore(manager_config)) as f:
            ports = await f.local_ports()
            assert [(p["setup"], p["busid"], p["encrypted"]) for p in ports] == [("bench", "1-2", True)]
            await f.detach("bench", "1-2")
    asyncio.run(check())
    assert "2 of 2 setup PCs updated" in text and bundled_version() in text
    log = remote["log"].read_text()
    assert "sudo -S -p ''" in log and "--install-deps" in log


def test_failed_install_continues_with_the_same_token(manager_config, remote, monkeypatch):
    monkeypatch.setenv("FAKE_INSTALL_FAIL", "1")
    o, out = opts(only=["bench"])
    assert Rollout(ConfigStore(manager_config), o).run() == 2
    cfg = ConfigStore(manager_config)
    pending = cfg.data()["rollout_pending"]["bench"]
    assert cfg.get_setup("bench")["token"] == TOKEN  # nothing lost: the agent still has the old token
    assert agent_accepts(remote["first"], TOKEN)
    assert "Run the same command again" in out.getvalue()
    assert cfg.data()["require_encryption"] is False
    monkeypatch.delenv("FAKE_INSTALL_FAIL")
    o, out = opts(only=["bench"])
    assert Rollout(ConfigStore(manager_config), o).run() == 0, out.getvalue()
    assert ConfigStore(manager_config).get_setup("bench")["token"] == pending
    # Only one of the two setups was updated: require encryption stays off.
    assert ConfigStore(manager_config).data()["require_encryption"] is False


def test_wrong_sudo_password(manager_config, remote, monkeypatch):
    monkeypatch.setenv("FAKE_SUDO_PASSWORD", "pw")
    o, out = opts(only=["bench"], ask_secret=lambda p: "wrong")
    assert Rollout(ConfigStore(manager_config), o).run() == 2
    assert "incorrect password" in out.getvalue()


def test_dry_run_changes_nothing(manager_config, remote):
    before = manager_config.read_text()
    o, out = opts(dry_run=True)
    assert Rollout(ConfigStore(manager_config), o).run() == 0
    text = out.getvalue()
    assert "dev@127.0.0.1" in text and "dev@localhost" in text and "dry run" in text
    assert manager_config.read_text() == before
    assert not remote["log"].exists()


def test_unreachable_pc(manager_config, remote, monkeypatch):
    data = json.loads(manager_config.read_text())
    data["setups"][1]["ssh"] = "dev@10.255.255.1"
    manager_config.write_text(json.dumps(data))
    o, out = opts(only=["bench2"])
    assert Rollout(ConfigStore(manager_config), o).run() == 2
    assert "SSH to dev@10.255.255.1 failed" in out.getvalue()


def test_only_one_setup_keeps_the_shared_token_and_keep_attached(manager_config, remote, monkeypatch):
    monkeypatch.setenv("FAKE_SUDO_PASSWORD", "pw")

    async def attach():
        async with Fleet(ConfigStore(manager_config)) as f:
            await f.attach("bench", "1-2")
            await f.set_sticky("bench", "1-2", True)
    asyncio.run(attach())

    o, out = opts(only=["bench"])
    assert Rollout(ConfigStore(manager_config), o).run() == 0, out.getvalue()
    cfg = ConfigStore(manager_config)
    # bench2 still uses the shared default token: it must stay until every setup has its own.
    assert cfg.data()["agent_defaults"]["token"] == "old-shared-token"
    assert cfg.data()["require_encryption"] is False
    # The device was detached for the update and attached again: "keep attached" stays on.
    assert "attached again: 1-2" in out.getvalue()
    assert [(x["setup"], x["busid"]) for x in cfg.sticky()] == [("bench", "1-2")]

    async def cleanup():
        async with Fleet(ConfigStore(manager_config)) as f:
            await f.detach("bench", "1-2")
    asyncio.run(cleanup())


def test_ssh_port_is_saved_per_setup(manager_config, remote, monkeypatch):
    monkeypatch.setenv("FAKE_SUDO_PASSWORD", "pw")
    data = json.loads(manager_config.read_text())
    data["setups"][0]["ssh_port"] = 2222
    manager_config.write_text(json.dumps(data))
    o, out = opts(only=["bench"])
    assert Rollout(ConfigStore(manager_config), o).run() == 0, out.getvalue()
    log = remote["log"].read_text()
    assert "-p 2222" in log
    assert ConfigStore(manager_config).get_setup("bench")["ssh_port"] == 2222
