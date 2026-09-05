"""Unit tests for the pure decision core of ollama_serve.py."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ollama_serve as os_mod

LOCAL = os_mod.LOCAL_TARGET


# ── parse_tcp_forward ────────────────────────────────────────────────────────


def test_parse_reads_the_configured_forward():
    status = '{"TCP": {"11434": {"TCPForward": "127.0.0.1:11434"}}}'
    assert os_mod.parse_tcp_forward(status) == LOCAL


def test_parse_ignores_other_ports():
    """A node serving something else must read as unconfigured for ours."""
    status = '{"TCP": {"8443": {"TCPForward": "127.0.0.1:8443"}}}'
    assert os_mod.parse_tcp_forward(status) is None


def test_parse_reports_a_forward_pointing_somewhere_else():
    status = '{"TCP": {"11434": {"TCPForward": "127.0.0.1:9999"}}}'
    assert os_mod.parse_tcp_forward(status) == "127.0.0.1:9999"


@pytest.mark.parametrize("text", ["", "   ", "No serve config", "null", "[]", "{}"])
def test_parse_treats_unusable_output_as_unconfigured(text):
    assert os_mod.parse_tcp_forward(text) is None


# ── plan ─────────────────────────────────────────────────────────────────────


def test_plan_enables_when_wanted_and_absent():
    assert os_mod.plan(current_target=None, want_serve=True) == "enable"


def test_plan_is_quiet_when_already_correct():
    assert os_mod.plan(current_target=LOCAL, want_serve=True) == "in-sync"


def test_plan_repoints_a_forward_aimed_elsewhere():
    assert os_mod.plan(current_target="127.0.0.1:1", want_serve=True) == "enable"


def test_plan_tears_down_when_no_longer_wanted():
    assert os_mod.plan(current_target=LOCAL, want_serve=False) == "disable"


def test_plan_is_quiet_when_unwanted_and_absent():
    assert os_mod.plan(current_target=None, want_serve=False) == "in-sync"


# ── load_want_serve ──────────────────────────────────────────────────────────


def test_want_serve_reads_the_hosts_flag():
    cfg = {"hosts": {"host-c": {"ollama_serve": True}}}
    assert os_mod.load_want_serve(cfg, "host-c") is True


def test_want_serve_defaults_to_false_for_a_host_without_the_key():
    cfg = {"hosts": {"host-d": {"ci_slots": 1}}}
    assert os_mod.load_want_serve(cfg, "host-d") is False


def test_want_serve_defaults_to_false_for_an_unknown_host():
    assert os_mod.load_want_serve({"hosts": {}}, "nobody") is False
    assert os_mod.load_want_serve({}, "nobody") is False


def test_want_serve_rejects_a_non_boolean():
    cfg = {"hosts": {"host-b": {"ollama_serve": "yes"}}}
    with pytest.raises(SystemExit, match="ollama_serve"):
        os_mod.load_want_serve(cfg, "host-b")
