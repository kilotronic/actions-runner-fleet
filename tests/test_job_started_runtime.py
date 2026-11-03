"""job-started.sh's CONTAINER_RUNTIME override, which label-only pool runners
set to `none` in their .env so a light job skips the OrbStack check even on a
host whose runners.toml opts into it."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")


def _orb_calls(tmp_path, container_runtime):
    base = tmp_path / "actions-runner"
    hooks = base / "hooks"
    tools = tmp_path / "kit"
    hooks.mkdir(parents=True)
    tools.mkdir()
    shutil.copy(REPO / "hooks" / "job-started.sh", hooks / "job-started.sh")
    calls = tmp_path / "orb-calls"
    (hooks / "ensure-orbstack.sh").write_text(f'#!{BASH}\necho "$@" >> "{calls}"\n')
    (hooks / "ensure-orbstack.sh").chmod(0o755)
    # The host's config opts into OrbStack.
    (tools / "fleet_config.py").write_text('print("orbstack")\n')
    (base / ".repo-path").write_text(str(tools))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("dirname", "python3", "mkdir"):
        (bindir / tool).symlink_to(shutil.which(tool))
    env = {"PATH": str(bindir)}
    if container_runtime is not None:
        env["CONTAINER_RUNTIME"] = container_runtime
    subprocess.run(
        [BASH, str(hooks / "job-started.sh")],
        env=env,
        capture_output=True,
        check=False,
        timeout=30,
    )
    return calls.read_text() if calls.exists() else ""


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_the_host_config_opts_a_runner_into_the_orbstack_check(tmp_path):
    assert "--full" in _orb_calls(tmp_path, None)


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_container_runtime_none_skips_it(tmp_path):
    assert _orb_calls(tmp_path, "none") == ""
