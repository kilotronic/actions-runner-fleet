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

### Job sounds

A host can chime when a job starts and ends. Put the files in a `sounds/`
directory next to `runners.toml`:

```
~/.config/actions-runner/sounds/job-start.aiff   # played when a job starts
~/.config/actions-runner/sounds/job-end.aiff     # played when a job ends
```

The same `sounds/` directory is found under `$XDG_CONFIG_HOME/actions-runner/`,
or beside `$ACTIONS_RUNNER_CONFIG`, following the inventory lookup above.
**Nothing plays by default**, and this repo ships no sounds: an absent file is
silence, and each event is independent. Keep the files with your inventory.

Players are `afplay` on macOS, and `pw-play`, `paplay` or `ffplay` on Linux, in
that order. `aplay` is never used — it cannot decode AIFF and plays files it
does not recognise as raw noise. On Linux the runner's systemd `--user` service
needs a reachable PipeWire or PulseAudio session (a lingering user with
`XDG_RUNTIME_DIR` set); without one the player fails quietly and the job is
unaffected.

The player is launched outside the runner's orphan-process cleanup. Without
that, a sound started from the job-completed hook is killed within milliseconds,
before it plays.

## Tools

| Tool                              | What it does                                                                  |
| --------------------------------- | ----------------------------------------------------------------------------- |
| `apply.py`                        | Converge local runners to the TOML (add / re-register / idle-guarded remove), then restart hung listeners |
| `load-watchdog.py`                | Pause idle listeners when per-core load is high; resume when it drops         |
| `lid-watchdog.py`                 | Pause idle listeners on a laptop whose lid is closed (macOS; no-op elsewhere) |
| `maintenance-timer.py`            | Every 2h: `update-host.sh` so idle hosts still self-heal                      |
| `runner_timers.py`                | Shared launchd / systemd `--user` installer                                   |
| `orbstack-watchdog.py`            | Installed only when `container_runtime = "orbstack"`                          |
| `ollama_serve.py`                 | Opt-in: publish local ollama to the tailnet; tear down when the flag is off   |
| `hooks/ensure-orbstack.sh`        | Four-state OrbStack recovery (healthy / down / slow / wedged)                 |
| `install.sh` / `install-linux.sh` | Register runners, install load / lid / maintenance timers                     |
| `status.sh`                       | Report runners, container runtime and CI Postgres for one repo                |
| `runner-status.30s.py`            | SwiftBar menu bar plugin showing live fleet status (macOS)                    |
| `install-menubar.sh`              | Install the SwiftBar plugin above                                             |
| `exclude-ci-paths.sh`             | Keep Spotlight / Time Machine / Photos off the CI work trees (macOS)          |
| `reclaim-ci-disk.sh`              | Thin Time Machine local snapshots when a CI host runs low on disk (macOS)     |

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
destructive removals with `touch ~/actions-runner/.no-auto-prune`. Disable the
hung-listener sweep with `touch ~/actions-runner/.no-runner-health`.

## Hung listeners

Every restart mechanism here keys off process *exit*: the launchd plist's
`KeepAlive`/`SuccessfulExit`, and the systemd unit's `Restart=`. A listener that
**hangs instead of exiting** therefore defeats all of them. This is not
hypothetical — a runner took a job, lost its worker, and sat with that job still
assigned for ten days: `run.sh` alive, PID present in `launchctl list`, and
`offline` on GitHub the whole time. Nothing noticed, and the host quietly ran at
half its configured capacity for a week and a half.

`apply.py` sweeps for exactly that mismatch after converging each repo: a runner
**registered-but-offline on GitHub while its service is running locally and it
holds no `Runner.Worker`**. Four guards keep it from bouncing anything healthy —
a busy runner is working (an offline status against a live worker is a
GitHub-side blip); a runner the load or lid watchdog paused is offline on
purpose; a stopped service is somebody's deliberate act; and an *unregistered*
dir is `decide()`'s re-register case, not this one.

Detection is debounced on **elapsed time**, not on a count of passes: a runner
must still be hung `HUNG_GRACE_SECONDS` (2h, one maintenance tick) after it was
first flagged. A count would mean nothing here, because `apply.py` runs on every
job-completed hook as well as the timer — "seen twice in a row" can be two
passes seconds apart on a busy host. The suspect clock lives in
`~/actions-runner/runner-health.state`, is cleared the moment a runner comes
back online, and is dropped when a runner dir disappears (dir names get reused,
and an inherited timestamp would skip the grace window). A restarted runner is
dropped from the state too, bounding this to at most one restart per runner per
grace period: a runner that genuinely cannot be revived bounces every 2h and
says so in `update.log` rather than spinning in a relaunch loop.

Worst-case recovery is two grace periods (~4h) from the moment a listener
wedges. Listener log mtime was evaluated as a cheaper, network-free signal and
rejected: a listener rotates to a fresh `_diag/Runner_*.log` on every restart
(and `apply.py` restarts runners routinely), so a stale log tracks restarts
rather than health — and the wedged listener above still logged sporadically
across its ten days.

## License

MIT — Kilotronic LLC
