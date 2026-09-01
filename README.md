# actions-runner-fleet

A kit of related tools for running **persistent** GitHub Actions self-hosted
runners on mixed Mac and Linux workstations — machines that sleep, share a
desktop, and already have Docker or OrbStack.

This is not Actions Runner Controller, and it is not an ephemeral VM scaler.
It converges a host's local runner set to a TOML inventory, with idle-guarded
scale-down, load shedding, and (optionally) OrbStack recovery across sleep.

## Config

Inventory lives **outside** this repo:

```
$ACTIONS_RUNNER_CONFIG
$XDG_CONFIG_HOME/actions-runner/runners.toml
~/.config/actions-runner/runners.toml
```

See [`runners.toml.example`](runners.toml.example). Override per command with
`./apply.py --config PATH` or `APPLY_HOST=<name>`.

```toml
[fleet]
slot_gated_repos = ["app"]   # cap these repos' runner count at ci_slots

[hosts.ci-mac]
ci_slots = 2
container_runtime = "orbstack"  # opt-in host-lifetime OrbStack ensure
"acme/app" = 2
```

`ollama_serve = true` on a host publishes that box's local ollama
(127.0.0.1:11434) to the tailnet via `tailscale serve`. Dropping the key tears
the forwarder down. ollama's API is unauthenticated; restrict who can connect
with a tailnet ACL.

## Tools

| Tool                              | What it does                                                                 |
| --------------------------------- | ---------------------------------------------------------------------------- |
| `apply.py`                        | Converge local runners to the TOML (add / re-register / idle-guarded remove) |
| `load-watchdog.py`                | Pause idle listeners when per-core load is high; resume when it drops        |
| `maintenance-timer.py`            | Every 2h: `update-host.sh` so idle hosts still self-heal                     |
| `runner_timers.py`                | Shared launchd / systemd `--user` installer                                  |
| `orbstack-watchdog.py`            | Installed only when `container_runtime = "orbstack"`                         |
| `ollama_serve.py`                 | Opt-in: publish local ollama to the tailnet; tear down when the flag is off  |
| `hooks/ensure-orbstack.sh`        | Four-state OrbStack recovery (healthy / down / slow / wedged)                |
| `install.sh` / `install-linux.sh` | Register runners, install load + maintenance timers                          |

OrbStack ensure is a **host** concern: the job-started hook runs before GitHub
sets up `jobs.<name>.container`, and the watchdog runs when no job is queued.
It is not a GitHub Action.

In-job services (Postgres, admission semaphores) belong in the workflow, not
here.

## Quick start

1. Copy `runners.toml.example` to `~/.config/actions-runner/runners.toml` and
   edit host keys (`hostname -s`) and `owner/repo` counts.
2. `gh auth login` (on macOS over SSH, store the token in `hosts.yml`, not the
   login keychain).
3. `./install.sh owner/repo` or `./install-linux.sh owner/repo`
4. `./apply.py --dry-run` then `./apply.py`

Disable auto-update with `touch ~/actions-runner/.no-auto-update`. Disable
destructive removals with `touch ~/actions-runner/.no-auto-prune`.

## License

MIT — Kilotronic LLC
