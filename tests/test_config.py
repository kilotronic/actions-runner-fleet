"""Config path resolution and runners.toml parsing."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fleet_config


def test_explicit_path_wins():
    path = fleet_config.resolve_config_path(explicit="/tmp/fleet.toml")
    assert path == Path("/tmp/fleet.toml")


def test_env_var_beats_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_RUNNER_CONFIG", str(tmp_path / "from-env.toml"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = fleet_config.resolve_config_path()
    assert path == tmp_path / "from-env.toml"


def test_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.delenv("ACTIONS_RUNNER_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = fleet_config.resolve_config_path()
    assert path == tmp_path / "xdg" / "actions-runner" / "runners.toml"


def test_default_is_home_config(tmp_path, monkeypatch):
    monkeypatch.delenv("ACTIONS_RUNNER_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    path = fleet_config.resolve_config_path(home=tmp_path)
    assert path == tmp_path / ".config" / "actions-runner" / "runners.toml"


def test_parse_host_counts_and_ci_slots(tmp_path):
    p = tmp_path / "runners.toml"
    p.write_text(
        '[hosts.box]\nci_slots = 2\n"acme/app" = 2\n"acme/docs" = 1\n',
        encoding="utf-8",
    )
    host = fleet_config.load_host(p, "box")
    assert host.counts == {"acme/app": 2, "acme/docs": 1}
    assert host.ci_slots == 2
    assert host.container_runtime is None
    assert host.slot_gated_repos == ()


def test_slot_gated_repos_from_fleet_table(tmp_path):
    p = tmp_path / "runners.toml"
    p.write_text(
        '[fleet]\nslot_gated_repos = ["app"]\n\n'
        '[hosts.box]\nci_slots = 1\n"acme/app" = 4\n',
        encoding="utf-8",
    )
    host = fleet_config.load_host(p, "box")
    assert host.slot_gated_repos == ("app",)


def test_container_runtime_orbstack(tmp_path):
    p = tmp_path / "runners.toml"
    p.write_text(
        '[hosts.box]\ncontainer_runtime = "orbstack"\n"acme/app" = 1\n',
        encoding="utf-8",
    )
    host = fleet_config.load_host(p, "box")
    assert host.container_runtime == "orbstack"


def test_unknown_container_runtime_exits(tmp_path):
    p = tmp_path / "runners.toml"
    p.write_text(
        '[hosts.box]\ncontainer_runtime = "colima"\n"acme/app" = 1\n',
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="container_runtime"):
        fleet_config.load_host(p, "box")


def test_missing_host_exits(tmp_path):
    p = tmp_path / "runners.toml"
    p.write_text('[hosts.other]\n"acme/app" = 1\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="no entry for host"):
        fleet_config.load_host(p, "box")


def test_cli_prints_container_runtime(tmp_path, monkeypatch):
    p = tmp_path / "runners.toml"
    p.write_text(
        '[hosts.box]\ncontainer_runtime = "orbstack"\n"acme/app" = 1\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ACTIONS_RUNNER_CONFIG", str(p))
    monkeypatch.setattr(fleet_config.socket, "gethostname", lambda: "box.local")
    assert fleet_config.container_runtime_for_this_host() == "orbstack"


def test_cli_prints_empty_when_unset(tmp_path, monkeypatch):
    p = tmp_path / "runners.toml"
    p.write_text('[hosts.box]\n"acme/app" = 1\n', encoding="utf-8")
    monkeypatch.setenv("ACTIONS_RUNNER_CONFIG", str(p))
    assert fleet_config.container_runtime_for_this_host(host="box") == ""
