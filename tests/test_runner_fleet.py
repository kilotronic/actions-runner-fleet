#!/usr/bin/env python3
"""Unit tests for the shared runner discovery / busy-idle module.

Run: python3 -m unittest test_runner_fleet

discover_runners() is exercised over a real tempdir tree (the fake-tree style
test_apply.py uses for discover_installed_dirs) since discovery is inherently
filesystem I/O. is_busy() and _repo_from_gh_url() are pure — tested over
injected process listings / strings, no I/O at all.
"""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "runner_fleet", Path(__file__).resolve().parents[1] / "runner_fleet.py"
)
runner_fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner_fleet)


def write_runner_file(dirpath, github_url, agent_name=None):
    dirpath.mkdir(parents=True, exist_ok=True)
    data = {"gitHubUrl": github_url}
    if agent_name is not None:
        data["agentName"] = agent_name
    (dirpath / ".runner").write_text(json.dumps(data))


class RepoFromGhUrlTest(unittest.TestCase):
    def test_parses_owner_repo(self):
        self.assertEqual(
            runner_fleet._repo_from_gh_url("https://github.com/o/partygame"),
            "o/partygame",
        )

    def test_tolerates_trailing_slash(self):
        self.assertEqual(
            runner_fleet._repo_from_gh_url("https://github.com/o/partygame/"),
            "o/partygame",
        )

    def test_none_on_unrecognized_url(self):
        self.assertIsNone(runner_fleet._repo_from_gh_url("not a url"))

    def test_none_on_empty_or_missing(self):
        self.assertIsNone(runner_fleet._repo_from_gh_url(""))
        self.assertIsNone(runner_fleet._repo_from_gh_url(None))


class DiscoverRunnersTest(unittest.TestCase):
    def test_finds_dir_with_runner_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_runner_file(
                base / "partygame-1",
                "https://github.com/o/partygame",
                agent_name="j-air-partygame-1",
            )
            runners = runner_fleet.discover_runners(base_dir=base)
            self.assertEqual(len(runners), 1)
            r = runners[0]
            self.assertEqual(r.dir, base / "partygame-1")
            self.assertEqual(r.repo, "o/partygame")
            self.assertEqual(r.name, "j-air-partygame-1")

    def test_dir_without_runner_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "partygame-1").mkdir()  # bare: config.sh only, no .runner
            (base / "partygame-1" / "config.sh").touch()
            self.assertEqual(runner_fleet.discover_runners(base_dir=base), [])

    def test_non_directory_entries_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "stray-file").touch()
            self.assertEqual(runner_fleet.discover_runners(base_dir=base), [])

    def test_missing_base_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist"
            self.assertEqual(runner_fleet.discover_runners(base_dir=missing), [])

    def test_malformed_runner_json_still_yields_a_runner_with_no_repo(self):
        # A corrupt/unparseable .runner shouldn't make the runner invisible to
        # host-local consumers (load-watchdog's busy scan needs every dir);
        # it just can't be attributed to a repo.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            d = base / "partygame-1"
            d.mkdir()
            (d / ".runner").write_text("{not json")
            runners = runner_fleet.discover_runners(base_dir=base)
            self.assertEqual(len(runners), 1)
            self.assertEqual(runners[0].dir, d)
            self.assertIsNone(runners[0].repo)
            self.assertEqual(runners[0].name, "partygame-1")  # falls back to dirname

    def test_unrecognized_github_url_yields_no_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_runner_file(base / "partygame-1", "not-a-github-url")
            runners = runner_fleet.discover_runners(base_dir=base)
            self.assertEqual(len(runners), 1)
            self.assertIsNone(runners[0].repo)

    def test_missing_agent_name_falls_back_to_dirname(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_runner_file(base / "partygame-1", "https://github.com/o/partygame")
            runners = runner_fleet.discover_runners(base_dir=base)
            self.assertEqual(runners[0].name, "partygame-1")

    def test_multiple_runners_sorted_by_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_runner_file(base / "partygame-2", "https://github.com/o/partygame")
            write_runner_file(base / "partygame-1", "https://github.com/o/partygame")
            runners = runner_fleet.discover_runners(base_dir=base)
            self.assertEqual(
                [r.dir.name for r in runners], ["partygame-1", "partygame-2"]
            )

    def test_default_base_dir_is_home_actions_runner(self):
        self.assertEqual(runner_fleet.RUNNER_BASE, Path.home() / "actions-runner")


class IsBusyTest(unittest.TestCase):
    def test_true_when_worker_cmdline_under_runner_dir(self):
        workers = ["/Users/j/actions-runner/partygame-1/bin/Runner.Worker 1 2"]
        self.assertTrue(
            runner_fleet.is_busy(Path("/Users/j/actions-runner/partygame-1"), workers)
        )

    def test_false_when_no_matching_worker(self):
        workers = ["/Users/j/actions-runner/partygame-2/bin/Runner.Worker 1 2"]
        self.assertFalse(
            runner_fleet.is_busy(Path("/Users/j/actions-runner/partygame-1"), workers)
        )

    def test_false_with_no_workers(self):
        self.assertFalse(
            runner_fleet.is_busy(Path("/Users/j/actions-runner/partygame-1"), [])
        )

    def test_index_prefix_collision_does_not_match(self):
        # partygame-1's needle must not match partygame-10's worker.
        workers = ["/Users/j/actions-runner/partygame-10/bin/Runner.Worker 1 2"]
        self.assertFalse(
            runner_fleet.is_busy(Path("/Users/j/actions-runner/partygame-1"), workers)
        )

    def test_accepts_runner_namedtuple(self):
        r = runner_fleet.Runner(
            dir=Path("/x/actions-runner/partygame-1"), name="n", repo="o/r"
        )
        workers = ["/x/actions-runner/partygame-1/bin/Runner.Worker"]
        self.assertTrue(runner_fleet.is_busy(r, workers))

    def test_workers_none_scans_live_processes(self):
        with mock.patch.object(
            runner_fleet,
            "worker_cmdlines",
            return_value=["/x/actions-runner/partygame-1/bin/Runner.Worker"],
        ):
            self.assertTrue(runner_fleet.is_busy(Path("/x/actions-runner/partygame-1")))


class WorkerCmdlinesTest(unittest.TestCase):
    def test_filters_to_runner_worker_lines_only(self):
        fake_stdout = (
            "/usr/bin/some-other-process\n"
            "/x/actions-runner/partygame-1/bin/Runner.Worker 1 2\n"
            "/bin/zsh\n"
        )
        with mock.patch.object(runner_fleet.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout=fake_stdout)
            out = runner_fleet.worker_cmdlines()
        self.assertEqual(out, ["/x/actions-runner/partygame-1/bin/Runner.Worker 1 2"])


class MainJsonTest(unittest.TestCase):
    def test_json_output_shape(self):
        fake_runners = [
            runner_fleet.Runner(
                dir=Path("/x/actions-runner/partygame-1"),
                name="j-air-partygame-1",
                repo="o/partygame",
            )
        ]
        with (
            mock.patch.object(
                runner_fleet, "discover_runners", return_value=fake_runners
            ),
            mock.patch.object(runner_fleet, "worker_cmdlines", return_value=[]),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = runner_fleet.main(["--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(
            data,
            [
                {
                    "dir": "/x/actions-runner/partygame-1",
                    "name": "j-air-partygame-1",
                    "repo": "o/partygame",
                    "busy": False,
                }
            ],
        )

    def test_json_output_marks_busy_runner(self):
        fake_runners = [
            runner_fleet.Runner(
                dir=Path("/x/actions-runner/partygame-1"), name="n", repo="o/r"
            )
        ]
        workers = ["/x/actions-runner/partygame-1/bin/Runner.Worker"]
        with (
            mock.patch.object(
                runner_fleet, "discover_runners", return_value=fake_runners
            ),
            mock.patch.object(runner_fleet, "worker_cmdlines", return_value=workers),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                runner_fleet.main(["--json"])
        data = json.loads(out.getvalue())
        self.assertTrue(data[0]["busy"])


class MainAnyBusyTest(unittest.TestCase):
    def _run(self, workers):
        fake_runners = [
            runner_fleet.Runner(
                dir=Path("/x/actions-runner/partygame-1"), name="n", repo="o/r"
            )
        ]
        with (
            mock.patch.object(
                runner_fleet, "discover_runners", return_value=fake_runners
            ),
            mock.patch.object(runner_fleet, "worker_cmdlines", return_value=workers),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = runner_fleet.main(["--any-busy"])
        return rc, out.getvalue()

    def test_exit_0_when_a_runner_is_busy(self):
        rc, printed = self._run(["/x/actions-runner/partygame-1/bin/Runner.Worker"])
        self.assertEqual(rc, 0)
        self.assertEqual(printed, "")

    def test_exit_1_when_idle(self):
        rc, printed = self._run([])
        self.assertEqual(rc, 1)
        self.assertEqual(printed, "")

    def test_exit_0_on_crash(self):
        with mock.patch.object(
            runner_fleet, "discover_runners", side_effect=OSError("boom")
        ):
            rc = runner_fleet.main(["--any-busy"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
