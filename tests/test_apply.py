#!/usr/bin/env python3
"""Unit tests for apply.py's pure convergence core.

Run: python3 -m unittest test_apply

Only decide() is tested — it holds all the convergence policy (classify, target
D healthy registered, re-register dead, install shortfall, surgical idle removal,
blast-radius cap, gh-failure and no-auto-prune skips). The imperative execution
(gh queries, config.sh, service control) is I/O, verified by --dry-run on-host.
"""

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "apply", Path(__file__).resolve().parents[1] / "apply.py"
)
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)

HOST = "j-air"


def reg(n, status="online"):
    """A GitHub runner record for local dir `n` on HOST (name = HOST-n).

    Carries only registration-liveness fields (name, status) — busy/idle
    guarding is decided locally now (runner_fleet.is_busy via decide()'s
    busy_dirs), not from the GH API's own `busy` flag.
    """
    return {"name": f"{HOST}-{n}", "status": status}


class DecideTest(unittest.TestCase):
    def decide(self, desired, local_dirs, gh_runners, **kw):
        return apply.decide(desired, local_dirs, gh_runners, host=HOST, **kw)

    def test_in_sync_is_noop(self):
        p = self.decide(
            2, ["partygame-1", "partygame-2"], [reg("partygame-1"), reg("partygame-2")]
        )
        self.assertEqual(p.to_reregister, [])
        self.assertEqual(p.to_install, 0)
        self.assertEqual(p.to_remove, [])
        self.assertEqual(p.to_cleanup, [])
        self.assertFalse(p.capped)

    def test_dead_runner_is_reregistered(self):
        # local dir exists but no GitHub registration → dead → re-register in place.
        p = self.decide(2, ["partygame-1", "partygame-2"], [reg("partygame-1")])
        self.assertEqual(p.to_reregister, ["partygame-2"])
        self.assertEqual(p.to_install, 0)
        self.assertEqual(p.to_remove, [])

    def test_paused_runner_is_healthy_not_dead(self):
        # A load-watchdog-paused runner is offline but STILL registered → healthy,
        # must NOT be re-registered (that would fight the watchdog).
        p = self.decide(1, ["partygame-1"], [reg("partygame-1", status="offline")])
        self.assertEqual(p.to_reregister, [])
        self.assertEqual(p.to_install, 0)
        self.assertEqual(p.to_remove, [])

    def test_shortfall_no_dead_installs(self):
        p = self.decide(3, ["partygame-1"], [reg("partygame-1")])
        self.assertEqual(p.to_reregister, [])
        self.assertEqual(p.to_install, 2)

    def test_shortfall_with_dead_reregisters_then_installs(self):
        # 1 healthy, 1 dead, want 3 → re-register the dead one, install 1 new.
        p = self.decide(3, ["partygame-1", "partygame-2"], [reg("partygame-1")])
        self.assertEqual(p.to_reregister, ["partygame-2"])
        self.assertEqual(p.to_install, 1)

    def test_excess_idle_is_surgically_removed_highest_index_first(self):
        p = self.decide(
            1,
            ["partygame-1", "partygame-2"],
            [reg("partygame-1"), reg("partygame-2")],
        )
        self.assertEqual(p.to_remove, ["partygame-2"])  # keep -1, drop highest idle
        self.assertEqual(p.to_reregister, [])
        self.assertEqual(p.to_install, 0)

    def test_busy_excess_is_never_removed(self):
        # want 1, have 2 healthy, but -2 is busy (process-based) → remove the
        # idle -1 instead.
        p = self.decide(
            1,
            ["partygame-1", "partygame-2"],
            [reg("partygame-1"), reg("partygame-2")],
            busy_dirs={"partygame-2"},
        )
        self.assertEqual(p.to_remove, ["partygame-1"])

    def test_all_excess_busy_removes_nothing(self):
        p = self.decide(
            1,
            ["partygame-1", "partygame-2"],
            [reg("partygame-1"), reg("partygame-2")],
            busy_dirs={"partygame-1", "partygame-2"},
        )
        self.assertEqual(p.to_remove, [])
        self.assertFalse(p.capped)

    def test_blast_radius_cap_refuses_and_flags(self):
        # want 0, have 5 idle healthy → 5 removals > cap 2 → refuse all, flag capped.
        dirs = [f"partygame-{i}" for i in range(1, 6)]
        p = self.decide(0, dirs, [reg(d) for d in dirs])
        self.assertEqual(p.to_remove, [])
        self.assertTrue(p.capped)

    def test_cap_boundary_two_removals_allowed(self):
        dirs = [f"partygame-{i}" for i in range(1, 4)]  # 3 healthy
        p = self.decide(1, dirs, [reg(d) for d in dirs])  # remove 2
        self.assertEqual(p.to_remove, ["partygame-3", "partygame-2"])
        self.assertFalse(p.capped)

    def test_gh_unavailable_skips_everything(self):
        p = self.decide(3, ["partygame-1"], None)
        self.assertEqual(p.to_reregister, [])
        self.assertEqual(p.to_install, 0)
        self.assertEqual(p.to_remove, [])
        self.assertEqual(p.to_cleanup, [])
        self.assertFalse(p.capped)

    def test_no_auto_prune_keeps_adds_drops_removals(self):
        # allow_remove=False: re-register + install still planned; removals suppressed.
        p = self.decide(
            1,
            ["partygame-1", "partygame-2", "partygame-3"],
            [reg("partygame-1")],  # -2, -3 dead
            allow_remove=False,
        )
        self.assertEqual(p.to_reregister, [])  # 1 healthy already meets D=1
        self.assertEqual(p.to_install, 0)
        self.assertEqual(p.to_remove, [])
        self.assertEqual(
            p.to_cleanup, []
        )  # dead cleanup is a removal → also suppressed

    def test_surplus_dead_dirs_are_cleaned_up(self):
        # 2 healthy meet D=2; a 3rd dir is dead cruft → cleanup, no reregister.
        p = self.decide(
            2,
            ["partygame-1", "partygame-2", "partygame-3"],
            [reg("partygame-1"), reg("partygame-2")],
        )
        self.assertEqual(p.to_reregister, [])
        self.assertEqual(p.to_install, 0)
        self.assertEqual(p.to_remove, [])
        self.assertEqual(p.to_cleanup, ["partygame-3"])

    def test_shortfall_uses_dead_then_cleans_extra_dead(self):
        # want 2, have 1 healthy + 3 dead → reregister 1 dead, clean the other 2.
        p = self.decide(
            2,
            ["partygame-1", "partygame-2", "partygame-3", "partygame-4"],
            [reg("partygame-1")],
        )
        self.assertEqual(p.to_reregister, ["partygame-2"])
        self.assertEqual(p.to_install, 0)
        self.assertEqual(sorted(p.to_cleanup), ["partygame-3", "partygame-4"])


class RemoveRunnerTest(unittest.TestCase):
    """remove_runner's fail-safe: a failed `config.sh remove` must NOT destroy
    local state (that would orphan the GitHub registration and, if a job just
    landed, kill it out from under the runner). Mocks all I/O — no real gh/service
    calls — but uses a real tempdir so the `(d / "config.sh").is_file()` guard
    sees a real file.
    """

    def _make_runner_dir(self, tmpdir, dirname):
        d = Path(tmpdir) / dirname
        d.mkdir(parents=True)
        (d / "config.sh").touch()
        return d

    def test_remove_runner_aborts_when_deregister_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dirname = "partygame-1"
            d = self._make_runner_dir(tmpdir, dirname)
            with (
                mock.patch.object(apply, "RUNNER_BASE", Path(tmpdir)),
                mock.patch.object(apply, "mint_token", return_value="tok"),
                mock.patch.object(apply, "run", return_value=1),
                mock.patch.object(apply, "_svc_stop_remove") as svc_stop_remove,
                mock.patch.object(apply.shutil, "rmtree") as rmtree,
            ):
                # remove_runner prints a diagnostic on this path (expected, useful
                # on-host) — swallow it here so `-v` test output stays pristine.
                with contextlib.redirect_stdout(io.StringIO()):
                    result = apply.remove_runner("owner/repo", dirname)

            self.assertFalse(result)
            svc_stop_remove.assert_not_called()
            rmtree.assert_not_called()
            self.assertTrue(d.exists())

    def test_remove_runner_completes_when_deregister_succeeds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dirname = "partygame-1"
            self._make_runner_dir(tmpdir, dirname)
            with (
                mock.patch.object(apply, "RUNNER_BASE", Path(tmpdir)),
                mock.patch.object(apply, "mint_token", return_value="tok"),
                mock.patch.object(apply, "run", return_value=0),
                mock.patch.object(apply, "_svc_stop_remove") as svc_stop_remove,
                mock.patch.object(apply.shutil, "rmtree") as rmtree,
            ):
                result = apply.remove_runner("owner/repo", dirname)

            self.assertTrue(result)
            svc_stop_remove.assert_called_once()
            rmtree.assert_called_once()


class ReregisterTest(unittest.TestCase):
    """reregister's fail-safe ordering: mint the token BEFORE touching local state,
    so a token failure never strips a dir it then can't re-register (the bug that
    left j-air's dotfiles-jl runners bare). And it must clear `.runner_migrated`,
    else config.sh --replace refuses with "already configured".
    """

    def _make_dir(self, tmpdir, dirname, files):
        d = Path(tmpdir) / dirname
        d.mkdir(parents=True)
        (d / "config.sh").touch()
        for f in files:
            (d / f).touch()
        return d

    def test_reregister_aborts_without_token_and_keeps_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            files = (
                ".runner",
                ".credentials",
                ".credentials_rsaparams",
                ".runner_migrated",
            )
            d = self._make_dir(tmpdir, "dotfiles-jl-1", files)
            with (
                mock.patch.object(apply, "RUNNER_BASE", Path(tmpdir)),
                mock.patch.object(apply, "mint_token", return_value=None),
                mock.patch.object(apply, "run") as run_cmd,
                mock.patch.object(apply, "_svc_restart") as svc_restart,
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = apply.reregister("owner/repo", "dotfiles-jl-1", "j-air")

            self.assertFalse(result)
            run_cmd.assert_not_called()  # never touched config.sh
            svc_restart.assert_not_called()
            for f in files:  # nothing stripped — the dir can still be retried
                self.assertTrue((d / f).exists(), f"{f} was stripped without a token")

    def test_reregister_clears_migrated_marker_and_configures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = self._make_dir(tmpdir, "dotfiles-jl-1", (".runner", ".runner_migrated"))
            with (
                mock.patch.object(apply, "RUNNER_BASE", Path(tmpdir)),
                mock.patch.object(apply, "mint_token", return_value="tok"),
                mock.patch.object(apply, "run", return_value=0) as run_cmd,
                mock.patch.object(
                    apply, "_svc_restart", return_value=True
                ) as svc_restart,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = apply.reregister("owner/repo", "dotfiles-jl-1", "j-air")

            self.assertTrue(result)
            self.assertFalse((d / ".runner_migrated").exists())  # cleared
            self.assertFalse((d / ".runner").exists())
            run_cmd.assert_called_once()  # config.sh --replace ran
            svc_restart.assert_called_once()


class ApplyEnvRestartsTest(unittest.TestCase):
    """The .env-restart path must consult a FRESH busy check, not the run-start
    snapshot (main()'s `busy_dirs`): install_runners() for an earlier repo can
    take minutes, long enough for a dir to pick up a job in between. Unlike
    remove_runner() (config.sh remove is rejected server-side for a busy
    runner), _svc_restart() is an unconditional kill+relaunch with no such
    guard — restarting on stale busy state kills a runner mid-job.
    """

    def _make_env_dir(self, tmpdir, dirname, text="CI_SLOTS=1\n"):
        d = Path(tmpdir) / dirname
        d.mkdir()
        (d / ".env").write_text(text)
        return d

    def test_dir_that_became_busy_since_the_snapshot_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dirname = "partygame-1"
            d = self._make_env_dir(tmpdir, dirname)
            with (
                mock.patch.object(apply, "RUNNER_BASE", Path(tmpdir)),
                mock.patch.object(apply, "_svc_restart") as svc_restart,
            ):
                # The run-start snapshot would have called this dir idle (it's
                # not even referenced here) — is_busy=True simulates the dir
                # having since picked up a job, which only a fresh check catches.
                failed = apply.apply_env_restarts(
                    [(dirname, "CI_SLOTS=2\n")],
                    reregistered=set(),
                    is_busy=lambda dn: True,
                )

            self.assertEqual(failed, 0)
            svc_restart.assert_not_called()  # no kill+relaunch of a busy runner
            self.assertEqual(
                (d / ".env").read_text(), "CI_SLOTS=1\n"
            )  # untouched: drift persists, next tick retries

    def test_dir_still_idle_is_written_and_restarted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dirname = "partygame-1"
            d = self._make_env_dir(tmpdir, dirname)
            with (
                mock.patch.object(apply, "RUNNER_BASE", Path(tmpdir)),
                mock.patch.object(
                    apply, "_svc_restart", return_value=True
                ) as svc_restart,
            ):
                failed = apply.apply_env_restarts(
                    [(dirname, "CI_SLOTS=2\n")],
                    reregistered=set(),
                    is_busy=lambda dn: False,
                )

            self.assertEqual(failed, 0)
            svc_restart.assert_called_once_with(dirname)
            self.assertEqual((d / ".env").read_text(), "CI_SLOTS=2\n")

    def test_just_reregistered_dir_skips_the_busy_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dirname = "partygame-1"
            self._make_env_dir(tmpdir, dirname)
            checked = []

            def is_busy(dn):
                checked.append(dn)
                return True  # would skip the restart if actually consulted

            with (
                mock.patch.object(apply, "RUNNER_BASE", Path(tmpdir)),
                mock.patch.object(
                    apply, "_svc_restart", return_value=True
                ) as svc_restart,
            ):
                failed = apply.apply_env_restarts(
                    [(dirname, "CI_SLOTS=2\n")],
                    reregistered={dirname},
                    is_busy=is_busy,
                )

            self.assertEqual(failed, 0)
            self.assertEqual(checked, [])  # never consulted: just restarted, known idle
            svc_restart.assert_called_once_with(dirname)

    def test_restart_failure_is_counted_even_when_idle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dirname = "partygame-1"
            self._make_env_dir(tmpdir, dirname)
            with (
                mock.patch.object(apply, "RUNNER_BASE", Path(tmpdir)),
                mock.patch.object(apply, "_svc_restart", return_value=False),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    failed = apply.apply_env_restarts(
                        [(dirname, "CI_SLOTS=2\n")],
                        reregistered=set(),
                        is_busy=lambda dn: False,
                    )

            self.assertEqual(failed, 1)


class DiscoverInstalledDirsTest(unittest.TestCase):
    """discover_installed_dirs finds installed-but-unregistered ("bare") dirs that
    discover_live can't classify, matched by name and anchored on the digit suffix.
    """

    def test_finds_bare_dirs_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            # bare: config.sh, no .runner → found
            (base / "dotfiles-jl-1").mkdir()
            (base / "dotfiles-jl-1" / "config.sh").touch()
            # registered: config.sh + .runner → discover_live's job, not found here
            (base / "dotfiles-jl-2").mkdir()
            (base / "dotfiles-jl-2" / "config.sh").touch()
            (base / "dotfiles-jl-2" / ".runner").touch()
            # different repo (digit anchor keeps -public out of "dotfiles-jl")
            (base / "dotfiles-jl-public-1").mkdir()
            (base / "dotfiles-jl-public-1" / "config.sh").touch()
            with mock.patch.object(apply, "RUNNER_BASE", base):
                self.assertEqual(
                    apply.discover_installed_dirs("dotfiles-jl"), ["dotfiles-jl-1"]
                )


class LoadDesiredTest(unittest.TestCase):
    def _load(self, toml_text, host="j-m4"):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml_text)
            path = Path(f.name)
        try:
            with mock.patch.object(apply, "CONFIG_FILE", path):
                return apply.load_desired(host)
        finally:
            path.unlink()

    def test_labels_parsed_and_not_treated_as_repo(self):
        counts, _, _, _, labels = self._load(
            '[hosts.j-m4]\nlabels = ["bigmem"]\n"o/partygame" = 1\n'
        )
        self.assertEqual(labels, ("bigmem",))
        self.assertEqual(counts, {"o/partygame": 1})

    def test_labels_default_empty(self):
        _, _, _, _, labels = self._load('[hosts.j-m4]\n"o/partygame" = 1\n')
        self.assertEqual(labels, ())

    def test_labels_reject_bad_values(self):
        for bad in ('labels = "bigmem"', "labels = [1]", 'labels = ["has space"]'):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                self._load(f'[hosts.j-m4]\n{bad}\n"o/partygame" = 1\n')

    def test_ci_slots_parsed_and_repos_intact(self):
        counts, ci_slots, _, _, _ = self._load(
            '[hosts.j-m4]\nci_slots = 2\n"o/partygame" = 2\n'
        )
        self.assertEqual(counts, {"o/partygame": 2})
        self.assertEqual(ci_slots, 2)

    def test_ci_slots_absent_falls_back_to_formula(self):
        counts, ci_slots, _, _, _ = self._load('[hosts.j-m4]\n"o/partygame" = 1\n')
        self.assertEqual(counts, {"o/partygame": 1})
        self.assertEqual(ci_slots, apply._default_ci_slots())

    def test_ci_slots_invalid_exits(self):
        with self.assertRaises(SystemExit):
            self._load('[hosts.j-m4]\nci_slots = 0\n"o/partygame" = 1\n')

    def test_e2e_workers_parsed(self):
        counts, _, e2e, _, _ = self._load(
            '[hosts.j-m4]\nci_slots = 2\ne2e_workers = 3\n"o/partygame" = 2\n'
        )
        self.assertEqual(counts, {"o/partygame": 2})
        self.assertEqual(e2e, 3)

    def test_e2e_workers_absent_is_none(self):
        _, _, e2e, _, _ = self._load('[hosts.j-m4]\n"o/partygame" = 1\n')
        self.assertIsNone(e2e)

    def test_e2e_workers_invalid_exits(self):
        for bad in ("e2e_workers = 0", 'e2e_workers = "two"'):
            with self.assertRaises(SystemExit):
                self._load(f'[hosts.j-m4]\n{bad}\n"o/partygame" = 1\n')

    def test_work_root_parsed_and_not_treated_as_repo(self):
        counts, _, _, work_root, _ = self._load(
            '[hosts.j-m4]\nwork_root = "/Volumes/Dev/jason/runner-work"\n'
            '"o/partygame" = 1\n'
        )
        self.assertEqual(counts, {"o/partygame": 1})
        self.assertEqual(work_root, "/Volumes/Dev/jason/runner-work")

    def test_work_root_absent_is_none(self):
        _, _, _, work_root, _ = self._load('[hosts.j-m4]\n"o/partygame" = 1\n')
        self.assertIsNone(work_root)

    def test_work_root_invalid_exits(self):
        for bad in ('work_root = "relative/path"', "work_root = 5"):
            with self.assertRaises(SystemExit):
                self._load(f'[hosts.j-m4]\n{bad}\n"o/partygame" = 1\n')


class UpsertEnvTest(unittest.TestCase):
    BASE = "LANG=en_US.UTF-8\nCI_SLOTS=2\n"

    def test_no_change_returns_none(self):
        self.assertIsNone(apply.upsert_env(self.BASE, {"CI_SLOTS": "2"}))

    def test_value_change_rewrites_line_in_place(self):
        out = apply.upsert_env(self.BASE, {"CI_SLOTS": "4"})
        self.assertEqual(out, "LANG=en_US.UTF-8\nCI_SLOTS=4\n")

    def test_missing_key_is_appended(self):
        out = apply.upsert_env(self.BASE, {"E2E_WORKERS_OVERRIDE": "3"})
        self.assertEqual(out, self.BASE + "E2E_WORKERS_OVERRIDE=3\n")

    def test_none_value_is_unmanaged(self):
        # Neither rewrites an existing line nor appends a missing one.
        self.assertIsNone(
            apply.upsert_env(
                self.BASE, {"CI_SLOTS": None, "E2E_WORKERS_OVERRIDE": None}
            )
        )

    def test_unrelated_lines_and_prefix_keys_untouched(self):
        text = "MY_CI_SLOTS=9\nCI_SLOTS=1\n"
        out = apply.upsert_env(text, {"CI_SLOTS": "2"})
        self.assertEqual(out, "MY_CI_SLOTS=9\nCI_SLOTS=2\n")


class InstallRunnersEnvTest(unittest.TestCase):
    def _call_env(self, e2e_workers, work_root=None):
        with mock.patch.object(apply.subprocess, "call", return_value=0) as call:
            apply.install_runners("o/partygame", 2, 4, e2e_workers, work_root)
        return call.call_args.kwargs["env"]

    def test_exports_ci_slots_and_e2e_workers(self):
        env = self._call_env(3)
        self.assertEqual(env["CI_SLOTS"], "4")
        self.assertEqual(env["E2E_WORKERS_OVERRIDE"], "3")

    def test_omits_e2e_workers_when_unset(self):
        env = self._call_env(None)
        self.assertEqual(env["CI_SLOTS"], "4")
        self.assertNotIn("E2E_WORKERS_OVERRIDE", env)

    def test_exports_work_root_when_set(self):
        env = self._call_env(3, "/Volumes/Dev/jason/runner-work")
        self.assertEqual(env["WORK_ROOT"], "/Volumes/Dev/jason/runner-work")

    def test_omits_work_root_when_unset(self):
        # Even if the installing host's ambient env carries a stale WORK_ROOT,
        # a host without work_root must not leak it into the new runner.
        with mock.patch.dict(apply.os.environ, {"WORK_ROOT": "/stale"}):
            env = self._call_env(None, None)
        self.assertNotIn("WORK_ROOT", env)


class SyncConfigTest(unittest.TestCase):
    """apply.py is the single writer of installed hook files (issue #19: a
    hardcoded list in update-host.sh silently dropped a newly added hook).
    sync_config() must discover hooks/ dynamically — never a filename list —
    so a new hook file needs no code change anywhere to start syncing.
    """

    def _write(self, path, text="#!/bin/sh\necho hi\n"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_new_hook_file_is_synced_without_being_named_anywhere(self):
        # The whole point: this filename appears nowhere in apply.py or its tests.
        with (
            tempfile.TemporaryDirectory() as src,
            tempfile.TemporaryDirectory() as dest,
        ):
            self._write(Path(src) / "hooks" / "totally-new-hook.sh")
            with (
                mock.patch.object(apply, "SCRIPT_DIR", Path(src)),
                mock.patch.object(apply, "RUNNER_BASE", Path(dest)),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    apply.sync_config()
            installed = Path(dest) / "hooks" / "totally-new-hook.sh"
            self.assertTrue(installed.is_file())
            self.assertEqual(installed.read_text(), "#!/bin/sh\necho hi\n")

    def test_installed_file_is_mode_755(self):
        with (
            tempfile.TemporaryDirectory() as src,
            tempfile.TemporaryDirectory() as dest,
        ):
            self._write(Path(src) / "hooks" / "job-started.sh")
            with (
                mock.patch.object(apply, "SCRIPT_DIR", Path(src)),
                mock.patch.object(apply, "RUNNER_BASE", Path(dest)),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    apply.sync_config()
            mode = (Path(dest) / "hooks" / "job-started.sh").stat().st_mode & 0o777
            self.assertEqual(mode, 0o755)

    def test_linux_overlay_overrides_top_level_basename(self):
        with (
            tempfile.TemporaryDirectory() as src,
            tempfile.TemporaryDirectory() as dest,
        ):
            self._write(Path(src) / "hooks" / "job-started.sh", "mac-variant\n")
            self._write(
                Path(src) / "hooks" / "linux" / "job-started.sh", "linux-variant\n"
            )
            with (
                mock.patch.object(apply, "SCRIPT_DIR", Path(src)),
                mock.patch.object(apply, "RUNNER_BASE", Path(dest)),
                mock.patch.object(apply, "IS_MAC", False),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    apply.sync_config()
            installed = Path(dest) / "hooks" / "job-started.sh"
            self.assertEqual(installed.read_text(), "linux-variant\n")

    def test_macos_ignores_linux_overlay_dir(self):
        with (
            tempfile.TemporaryDirectory() as src,
            tempfile.TemporaryDirectory() as dest,
        ):
            self._write(Path(src) / "hooks" / "job-started.sh", "mac-variant\n")
            self._write(
                Path(src) / "hooks" / "linux" / "job-started.sh", "linux-variant\n"
            )
            with (
                mock.patch.object(apply, "SCRIPT_DIR", Path(src)),
                mock.patch.object(apply, "RUNNER_BASE", Path(dest)),
                mock.patch.object(apply, "IS_MAC", True),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    apply.sync_config()
            installed = Path(dest) / "hooks" / "job-started.sh"
            self.assertEqual(installed.read_text(), "mac-variant\n")

    def test_linux_only_hook_is_installed_under_its_own_basename(self):
        # hooks/linux/*.sh with no top-level counterpart still installs (not just
        # an override of an existing name).
        with (
            tempfile.TemporaryDirectory() as src,
            tempfile.TemporaryDirectory() as dest,
        ):
            self._write(Path(src) / "hooks" / "linux" / "only-linux.sh")
            with (
                mock.patch.object(apply, "SCRIPT_DIR", Path(src)),
                mock.patch.object(apply, "RUNNER_BASE", Path(dest)),
                mock.patch.object(apply, "IS_MAC", False),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    apply.sync_config()
            self.assertTrue((Path(dest) / "hooks" / "only-linux.sh").is_file())

    def test_non_sh_files_are_ignored(self):
        with (
            tempfile.TemporaryDirectory() as src,
            tempfile.TemporaryDirectory() as dest,
        ):
            self._write(Path(src) / "hooks" / "job-started.sh")
            self._write(Path(src) / "hooks" / "README.md", "not a hook\n")
            with (
                mock.patch.object(apply, "SCRIPT_DIR", Path(src)),
                mock.patch.object(apply, "RUNNER_BASE", Path(dest)),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    apply.sync_config()
            self.assertFalse((Path(dest) / "hooks" / "README.md").exists())

    def test_missing_source_hooks_dir_is_a_quiet_noop(self):
        with (
            tempfile.TemporaryDirectory() as src,
            tempfile.TemporaryDirectory() as dest,
        ):
            with (
                mock.patch.object(apply, "SCRIPT_DIR", Path(src)),
                mock.patch.object(apply, "RUNNER_BASE", Path(dest)),
            ):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    changed = apply.sync_config()
            self.assertEqual(changed, 0)
            self.assertEqual(out.getvalue(), "")

    def test_second_run_with_no_changes_is_silent(self):
        with (
            tempfile.TemporaryDirectory() as src,
            tempfile.TemporaryDirectory() as dest,
        ):
            self._write(Path(src) / "hooks" / "job-started.sh")
            with (
                mock.patch.object(apply, "SCRIPT_DIR", Path(src)),
                mock.patch.object(apply, "RUNNER_BASE", Path(dest)),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    apply.sync_config()  # first run installs it
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    changed = apply.sync_config()
            self.assertEqual(changed, 0)
            self.assertEqual(out.getvalue(), "")

    def test_changed_content_is_rewritten_and_reported(self):
        with (
            tempfile.TemporaryDirectory() as src,
            tempfile.TemporaryDirectory() as dest,
        ):
            self._write(Path(src) / "hooks" / "job-started.sh", "v1\n")
            with (
                mock.patch.object(apply, "SCRIPT_DIR", Path(src)),
                mock.patch.object(apply, "RUNNER_BASE", Path(dest)),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    apply.sync_config()  # first run installs it
                self._write(Path(src) / "hooks" / "job-started.sh", "v2\n")
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    changed = apply.sync_config()
            self.assertEqual(changed, 1)
            self.assertIn("job-started.sh", out.getvalue())
            self.assertEqual(
                (Path(dest) / "hooks" / "job-started.sh").read_text(), "v2\n"
            )


class ClampTest(unittest.TestCase):
    def test_gated_repo_over_slots_is_clamped(self):
        self.assertEqual(apply.clamp_to_slots("app", 4, 2, gated=("app",)), (2, True))

    def test_gated_repo_at_or_below_slots_untouched(self):
        self.assertEqual(apply.clamp_to_slots("app", 2, 2, gated=("app",)), (2, False))
        self.assertEqual(apply.clamp_to_slots("app", 1, 2, gated=("app",)), (1, False))

    def test_ungated_repo_never_clamped(self):
        self.assertEqual(apply.clamp_to_slots("docs", 4, 2, gated=("app",)), (4, False))

    def test_empty_gated_list_never_clamps(self):
        self.assertEqual(apply.clamp_to_slots("app", 4, 2), (4, False))


if __name__ == "__main__":
    unittest.main()


class ConvergeLabelsTest(unittest.TestCase):
    """Labels converge over the API, not at registration.

    This is the whole point of the function: apply.py only re-registers DEAD
    runners, so a healthy runner would otherwise keep its original label set
    forever and adding a label to runners.toml would silently require an
    uninstall/reinstall of the fleet.
    """

    def _runner(self, name, rid, labels):
        return {"name": name, "id": rid, "status": "online", "labels": labels}

    def test_adds_only_the_missing_label(self):
        gh = [
            self._runner("h-partygame-1", 7, ["self-hosted", "macOS"]),
            self._runner("h-partygame-2", 8, ["self-hosted", "bigmem"]),
        ]
        with mock.patch.object(apply, "_run", return_value=0) as r:
            n = apply.converge_labels("o/partygame", gh, ("bigmem",))
        self.assertEqual(n, 1)
        self.assertEqual(r.call_count, 1)
        cmd = r.call_args[0][0]
        self.assertIn("repos/o/partygame/actions/runners/7/labels", cmd)
        self.assertIn("labels[]=bigmem", cmd)

    def test_noop_when_all_present(self):
        gh = [self._runner("h-partygame-1", 7, ["self-hosted", "bigmem"])]
        with mock.patch.object(apply, "_run", return_value=0) as r:
            self.assertEqual(apply.converge_labels("o/p", gh, ("bigmem",)), 0)
        r.assert_not_called()

    def test_noop_without_extra_labels(self):
        gh = [self._runner("h-partygame-1", 7, ["self-hosted"])]
        with mock.patch.object(apply, "_run", return_value=0) as r:
            self.assertEqual(apply.converge_labels("o/p", gh, ()), 0)
        r.assert_not_called()

    def test_gh_failure_is_not_fatal(self):
        """A failed add is reported and skipped — the next tick retries."""
        gh = [self._runner("h-partygame-1", 7, ["self-hosted"])]
        with mock.patch.object(apply, "_run", return_value=1):
            self.assertEqual(apply.converge_labels("o/p", gh, ("bigmem",)), 0)

    def test_never_removes_an_unexpected_label(self):
        """Additive only: a hand-added label is somebody's deliberate act."""
        gh = [self._runner("h-partygame-1", 7, ["self-hosted", "bigmem", "manual"])]
        with mock.patch.object(apply, "_run", return_value=0) as r:
            self.assertEqual(apply.converge_labels("o/p", gh, ("bigmem",)), 0)
        r.assert_not_called()
