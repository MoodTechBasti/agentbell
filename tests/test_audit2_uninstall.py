"""Audit 2026-09-22, cluster "uninstall": regression tests.

M10  purge must not rmtree a shared AGENTBELL_CONFIG_DIR / AGENTBELL_STATE_DIR
L    `pipx list` exiting 1 must not hide a pipx install of agentbell
L    a custom AGENTBELL_CONFIG file must be listed and removed
L    the Windows pip launcher (Scripts\\agentbell.exe) must be removed
     + the report says what it removed and what it left
"""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import unittest.mock
import zipfile
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agentbell as an  # noqa: E402
import test_agentbell as base  # noqa: E402

ENV_KEYS = (an.CONFIG_DIR_ENV, an.STATE_DIR_ENV, an.CONFIG_FILE_ENV,
            "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_BIN_HOME", "APPDATA", "LOCALAPPDATA")


class _UninstallFixture(unittest.TestCase):
    """A temp home, and no look at the real pipx or pip --user site."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="agentbell-uninstall-")
        self.home = os.path.join(self.root, "home")
        self.project = os.path.join(self.root, "project")
        os.makedirs(self.home)
        os.makedirs(self.project)
        self.old_home = base._set_home(self.home)
        self.old_env = {key: os.environ.get(key) for key in ENV_KEYS}
        # `uninstall --yes` applies every entry it finds: every config path it
        # scans (Claude Desktop, VS Code, OpenCode ...) must be inside the
        # temp home, whatever the machine running the tests has set
        for key, rel in (("XDG_CONFIG_HOME", ".config"), ("XDG_STATE_HOME", ".local/state"),
                         ("XDG_BIN_HOME", ".local/bin"), ("APPDATA", "AppData/Roaming"),
                         ("LOCALAPPDATA", "AppData/Local")):
            os.environ[key] = os.path.join(self.home, *rel.split("/"))
        os.environ.pop(an.CONFIG_FILE_ENV, None)
        self.old_argv0 = sys.argv[0]
        sys.argv[0] = base._fake_launcher(self.root)
        patches = [
            unittest.mock.patch.object(an, "_pipx_installed", return_value=None),
            unittest.mock.patch.object(an, "_user_site_dirs", return_value=(None, None)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        base._restore_home(self.old_home)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        sys.argv[0] = self.old_argv0
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, path, content="x"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def _uninstall(self, *extra):
        parser = an.build_parser()
        args = parser.parse_args(["uninstall", "--project", self.project, *extra])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                args.func(args)
            except SystemExit as exc:
                self.fail(f"uninstall exited {exc.code}:\n{out.getvalue()}{err.getvalue()}")
        return out.getvalue(), err.getvalue()

    def _tree(self, directory):
        found = []
        for dirpath, dirnames, filenames in os.walk(directory):
            for name in dirnames + filenames:
                found.append(os.path.relpath(os.path.join(dirpath, name), directory))
        return sorted(found)


class TestSharedDirsM10(_UninstallFixture):
    """AGENTBELL_CONFIG_DIR / AGENTBELL_STATE_DIR may name a directory other
    programs use too. Purge used to rmtree it whole."""

    def _share(self):
        self.cdir = os.path.join(self.home, ".config")
        self.sdir = os.path.join(self.home, ".local", "state")
        os.environ[an.CONFIG_DIR_ENV] = self.cdir
        os.environ[an.STATE_DIR_ENV] = self.sdir
        os.environ.pop(an.CONFIG_FILE_ENV, None)
        self.foreign = [
            self._write(os.path.join(self.cdir, "othertool", "settings.ini"), "keep"),
            self._write(os.path.join(self.cdir, "notes.txt"), "keep"),
            self._write(os.path.join(self.sdir, "nvim", "shada"), "keep"),
            # generic names next to ours: only exact names and their
            # .tmp/.lock siblings are agentbell's
            self._write(os.path.join(self.sdir, "history.jsonl.bak"), "keep"),
            self._write(os.path.join(self.sdir, "queue.db"), "keep"),
        ]

    def _write_agentbell_state(self):
        """Every kind of file agentbell writes, through its own writers."""
        cfg = base.make_config("http://127.0.0.1:9")
        cfg.path = an.config_path()
        cfg.data["license"] = "AB1-secret"
        cfg.save()
        an.write_history({"event": "notify", "message": "m"})
        an.enqueue_item(cfg, {"message": "queued", "channels": ["ntfy"],
                              "priority": "normal", "event": "notify"})
        an.defer_item(cfg, "held")
        an.write_start_marker("claude", session_id="s1")
        an.claim_hook_send("claude", "run_completed", "m", window=60)
        an.write_bot_heartbeat()
        an.release_bot_lock(an.acquire_bot_lock())
        an.write_tg_answer("a1", "yes")
        an.write_tg_pending("a1", "q?", 60)
        an.write_ntfy_pending("a2", "q?", 60)
        self.assertTrue(an.claim_consumed("ntfy", "m1"))
        self._write(os.path.join(self.sdir, "history.jsonl.tmp"), "")
        os.makedirs(os.path.join(self.sdir, "ntfy-consumed.lock"))

    def test_yes_deletes_only_agentbell_entries_of_shared_dirs(self):
        self._share()
        self._write_agentbell_state()
        out, _ = self._uninstall("--yes")
        for path in self.foreign:
            self.assertTrue(os.path.exists(path), f"purge deleted {path}\n{out}")
        # nothing agentbell wrote is left behind in either directory
        self.assertEqual(self._tree(self.cdir),
                         ["notes.txt", "othertool", os.path.join("othertool", "settings.ini")])
        self.assertEqual(self._tree(self.sdir),
                         ["history.jsonl.bak", "nvim", os.path.join("nvim", "shada"), "queue.db"])

    def test_report_says_what_it_removed_and_what_it_kept(self):
        self._share()
        self._write_agentbell_state()
        plan, _ = self._uninstall()
        self.assertIn(f"agentbell's config files in {self.cdir}", plan)
        self.assertIn("config.json", plan)
        self.assertIn("the directory stays", plan)
        self.assertNotIn("delete directory", plan)
        self.assertTrue(os.path.exists(os.path.join(self.cdir, "config.json")))
        out, _ = self._uninstall("--yes")
        self.assertIn(f"removed  agentbell's config files in {self.cdir}", out)
        self.assertIn(f"removed  agentbell's state files in {self.sdir}", out)
        self.assertIn(f"kept     {self.cdir}: notes.txt, othertool (not agentbell's)", out)
        self.assertIn(f"kept     {self.sdir}: history.jsonl.bak, nvim, queue.db (not agentbell's)",
                      out)

    def test_shared_dir_holding_only_agentbell_files_is_removed(self):
        self.cdir = os.path.join(self.root, "ab-config")
        self.sdir = os.path.join(self.root, "ab-state")
        os.environ[an.CONFIG_DIR_ENV] = self.cdir
        os.environ[an.STATE_DIR_ENV] = self.sdir
        self._write_agentbell_state()
        plan, _ = self._uninstall()
        self.assertIn("then the directory (nothing else is in it)", plan)
        out, _ = self._uninstall("--yes")
        self.assertFalse(os.path.exists(self.cdir), out)
        self.assertFalse(os.path.exists(self.sdir), out)
        self.assertNotIn("kept ", out)

    def test_config_and_state_in_the_same_shared_dir(self):
        shared = os.path.join(self.home, ".agentbell-and-more")
        self.sdir = shared
        os.environ[an.CONFIG_DIR_ENV] = shared
        os.environ[an.STATE_DIR_ENV] = shared
        keep = self._write(os.path.join(shared, "mine.txt"), "keep")
        self._write_agentbell_state()
        plan, _ = self._uninstall()
        # one entry for the directory, and it does not call agentbell's own
        # state files "other entries"
        self.assertEqual(plan.count(f"files in {shared}"), 1, plan)
        self.assertIn("the directory stays: it also holds mine.txt", plan)
        out, _ = self._uninstall("--yes")
        self.assertEqual(os.listdir(shared), ["mine.txt"], out)
        self.assertTrue(os.path.exists(keep))

    @unittest.skipIf(os.name == "nt", "os.symlink requires admin or developer mode on Windows")
    def test_symlinked_shared_dir_is_emptied_but_the_link_stays(self):
        target = os.path.join(self.root, "real-state")
        link = os.path.join(self.home, "state-link")
        os.makedirs(target)
        os.symlink(target, link)
        self.sdir = link
        os.environ[an.CONFIG_DIR_ENV] = os.path.join(self.root, "ab-config")
        os.environ[an.STATE_DIR_ENV] = link
        self._write_agentbell_state()
        out, _ = self._uninstall("--yes")
        self.assertIn(f"removed  agentbell's state files in {link}", out)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.listdir(target), [])

    def test_second_run_reports_nothing_for_shared_dirs(self):
        self._share()
        self._write_agentbell_state()
        self._uninstall("--yes")
        report = an.purge_report(project=self.project)
        self.assertEqual([e["label"] for e in report["entries"]], [])

    def test_default_dirs_are_still_removed_whole(self):
        # no AGENTBELL_* dir override: the directories are named after
        # agentbell and hold nothing else, so they go whole - including a
        # file an older version wrote that today's names do not cover
        os.environ.pop(an.CONFIG_DIR_ENV, None)
        os.environ.pop(an.STATE_DIR_ENV, None)
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.home, ".config")
        os.environ["XDG_STATE_HOME"] = os.path.join(self.home, ".local", "state")
        self._write(an.config_path(), "{}")
        self._write(an.history_path(), "{}\n")
        self._write(os.path.join(an.state_dir(), "old-version-file"), "x")
        keep = self._write(os.path.join(self.home, ".config", "othertool", "a.ini"))
        plan, _ = self._uninstall()
        self.assertIn(f"config directory {an.config_dir()}", plan)
        self.assertIn(f"state directory {an.state_dir()}", plan)
        self._uninstall("--yes")
        self.assertFalse(os.path.exists(an.config_dir()))
        self.assertFalse(os.path.exists(an.state_dir()))
        self.assertTrue(os.path.exists(keep))

    def test_state_names_cover_every_state_writer(self):
        """Drift guard: a new file or directory written straight into the
        state dir must be added to STATE_DIR_NAMES, or purge leaves it in a
        shared state dir."""
        with open(an.__file__, "r", encoding="utf-8") as fh:
            source = fh.read()
        written = set(re.findall(r'os\.path\.join\(state_dir\(\),\s*"([^"/]+)"', source))
        written |= set(re.findall(r'_pending_dir\("([^"]+)"\)', source))
        written |= {f"{name}-consumed" for name in
                    re.findall(r'claim_consumed\("([^"]+)"', source)}
        self.assertTrue({"history.jsonl", "queue", "bot.lock", "tg-pending"} <= written)
        self.assertEqual(sorted(written - set(an.STATE_DIR_NAMES)), [])


class TestCustomConfigFile(_UninstallFixture):
    """AGENTBELL_CONFIG names a config file outside the config dir."""

    def setUp(self):
        super().setUp()
        os.environ[an.CONFIG_DIR_ENV] = os.path.join(self.home, ".config", "agentbell")
        os.environ[an.STATE_DIR_ENV] = os.path.join(self.home, ".local", "state", "agentbell")

    def test_custom_config_file_is_listed_and_removed(self):
        custom = self._write(os.path.join(self.home, "dotfiles", "agentbell.json"),
                             json.dumps({"license": "AB1-secret"}))
        sibling = self._write(os.path.join(self.home, "dotfiles", "zshrc"), "keep")
        old = self._write(os.path.join(an.config_dir(), "config.json"), "{}")
        os.environ[an.CONFIG_FILE_ENV] = custom
        plan, _ = self._uninstall()
        self.assertIn(f"config file {custom} (AGENTBELL_CONFIG)", plan)
        self.assertTrue(os.path.exists(custom))
        out, _ = self._uninstall("--yes")
        self.assertIn(f"removed  config file {custom} (AGENTBELL_CONFIG)", out)
        self.assertFalse(os.path.exists(custom))
        self.assertFalse(os.path.exists(old))     # the config dir still goes too
        self.assertTrue(os.path.exists(sibling))  # the file's directory is not ours

    def test_custom_config_file_inside_a_shared_config_dir(self):
        shared = os.path.join(self.home, ".config")
        os.environ[an.CONFIG_DIR_ENV] = shared
        custom = self._write(os.path.join(shared, "agentbell.json"), "{}")
        self._write(os.path.join(shared, "config.json"), "{}")
        keep = self._write(os.path.join(shared, "othertool.ini"), "keep")
        os.environ[an.CONFIG_FILE_ENV] = custom
        plan, _ = self._uninstall()
        self.assertEqual(plan.count("agentbell.json"), 1, plan)
        self.assertIn("the directory stays: it also holds othertool.ini", plan)
        self._uninstall("--yes")
        self.assertEqual(os.listdir(shared), ["othertool.ini"])
        self.assertTrue(os.path.exists(keep))

    def test_missing_custom_config_file_is_not_listed(self):
        os.environ[an.CONFIG_FILE_ENV] = os.path.join(self.home, "nowhere.json")
        report = an.purge_report(project=self.project)
        self.assertEqual(report["entries"], [])


class TestPipxDetection(unittest.TestCase):
    """`pipx list` exits 1 as soon as any pipx venv has a problem."""

    HEALTHY = ("venvs are in /x/pipx/venvs\napps are exposed on your $PATH at /x/bin\n"
               "   package agentbell 1.6.3, installed using Python 3.12.3\n    - agentbell\n")
    BROKEN_OTHER = ("   package brokentool has invalid interpreter /nonexistent/python3.99\r\u26a0\ufe0f\n"
                    "\nOne or more packages have a missing python interpreter.\n"
                    "    To fix, execute: pipx reinstall-all\n\n")

    def _run(self, returncode, stdout="", stderr="", error=None):
        completed = an.subprocess.CompletedProcess(["/tools/pipx", "list"], returncode,
                                                   stdout=stdout, stderr=stderr)
        run = unittest.mock.patch.object(
            an.subprocess, "run",
            side_effect=error if error else None,
            return_value=completed)
        which = unittest.mock.patch.object(
            an.shutil, "which",
            side_effect=lambda name: "/tools/pipx" if name == "pipx" else None)
        warnings = []
        with run, which:
            found = an._pipx_installed(warnings)
        return found, warnings

    def test_exit_1_from_an_unrelated_broken_venv_still_finds_agentbell(self):
        found, warnings = self._run(1, stdout=self.HEALTHY, stderr=self.BROKEN_OTHER)
        self.assertEqual(found, "/tools/pipx")
        self.assertEqual(warnings, [])

    def test_broken_agentbell_venv_listed_on_stderr_is_found(self):
        stderr = ("   package agentbell has invalid interpreter /usr/bin/python3.11\r\u26a0\ufe0f\n"
                  + self.BROKEN_OTHER)
        found, _ = self._run(1, stdout="venvs are in /x/pipx/venvs\n", stderr=stderr)
        self.assertEqual(found, "/tools/pipx")

    def test_other_package_with_agentbell_prefix_is_not_ours(self):
        found, warnings = self._run(0, stdout="   package agentbell-extras 0.1, installed\n")
        self.assertIsNone(found)
        self.assertEqual(warnings, [])

    def test_failed_pipx_list_leaves_a_warning(self):
        found, warnings = self._run(1, stderr="Traceback (most recent call last):\nKeyError: 'x'\n")
        self.assertIsNone(found)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("pipx list", warnings[0])
        self.assertIn("KeyError: 'x'", warnings[0])
        self.assertIn("pipx uninstall agentbell", warnings[0])

    def test_pipx_that_cannot_run_leaves_a_warning(self):
        found, warnings = self._run(0, error=an.subprocess.TimeoutExpired(["pipx", "list"], 30))
        self.assertIsNone(found)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("pipx uninstall agentbell", warnings[0])

    def test_nothing_installed_with_pipx_is_silent(self):
        found, warnings = self._run(0, stderr="nothing has been installed with pipx \U0001f634\n")
        self.assertIsNone(found)
        self.assertEqual(warnings, [])


class TestPipxWarningReachesTheReport(_UninstallFixture):

    def test_purge_report_carries_the_pipx_warning(self):
        def fake(warnings=None):
            warnings.append("could not check pipx")
            return None
        with unittest.mock.patch.object(an, "_pipx_installed", side_effect=fake):
            report = an.purge_report(project=self.project)
        self.assertIn("could not check pipx", report["warnings"])


def _windows_launcher(module="agentbell"):
    """The shape pip writes on Windows: a launcher .exe with a shebang and a
    zip holding __main__.py appended to its end."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("__main__.py", f"import sys\nfrom {module} import main\n"
                                   "if __name__ == '__main__':\n    sys.exit(main())\n")
    return (b"MZ\x90\x00" + b"\x00" * 100000
            + b"#!C:\\Python314\\python.exe\n" + buffer.getvalue())


class TestWindowsLauncher(_UninstallFixture):
    """`py -m pip install --user .` puts Scripts\\agentbell.exe next to the
    user site-packages; purge only looked for <user base>/bin/agentbell."""

    def _pip_user_install(self, launcher_bytes):
        user_base = os.path.join(self.home, "AppData", "Roaming", "Python")
        user_site = os.path.join(user_base, "Python314", "site-packages")
        os.makedirs(os.path.join(user_site, "agentbell-1.6.3.dist-info"))
        self._write(os.path.join(user_site, "agentbell.py"), "# agentbell\n")
        launcher = os.path.join(user_base, "Python314", "Scripts", "agentbell.exe")
        os.makedirs(os.path.dirname(launcher))
        with open(launcher, "wb") as fh:
            fh.write(launcher_bytes)
        os.environ[an.CONFIG_DIR_ENV] = os.path.join(self.root, "cfg")
        os.environ[an.STATE_DIR_ENV] = os.path.join(self.root, "state")
        return user_base, user_site, launcher

    def test_launcher_is_ours_by_its_zipped_tail(self):
        path = os.path.join(self.root, "agentbell.exe")
        with open(path, "wb") as fh:
            fh.write(_windows_launcher())
        self.assertTrue(an._is_our_binary(path))
        with open(path, "wb") as fh:
            fh.write(_windows_launcher(module="othertool"))
        self.assertFalse(an._is_our_binary(path))

    def test_pip_user_launcher_is_listed_and_removed(self):
        user_base, user_site, launcher = self._pip_user_install(_windows_launcher())
        with unittest.mock.patch.object(an, "_user_site_dirs",
                                        return_value=(user_base, user_site)):
            plan, _ = self._uninstall()
            self.assertIn(f"pip --user script {launcher}", plan)
            out, _ = self._uninstall("--yes")
        self.assertIn(f"removed  pip --user script {launcher}", out)
        self.assertFalse(os.path.exists(launcher))
        self.assertFalse(os.path.exists(os.path.join(user_site, "agentbell.py")))

    def test_launcher_running_this_command_points_to_py_m(self):
        user_base, user_site, launcher = self._pip_user_install(_windows_launcher())
        # pip's launcher hands Python its own path with '.exe' cut off
        sys.argv[0] = launcher[:-len(".exe")]
        with unittest.mock.patch.object(an, "_user_site_dirs",
                                        return_value=(user_base, user_site)), \
                unittest.mock.patch.object(an.platform, "system", return_value="Windows"):
            plan, _ = self._uninstall()
        self.assertIn(f"delete {launcher} (Windows locks it while it runs this command)", plan)
        self.assertIn("run 'py -m agentbell uninstall --yes' to delete everything", plan)
        with unittest.mock.patch.object(an, "_user_site_dirs",
                                        return_value=(user_base, user_site)), \
                unittest.mock.patch.object(an.platform, "system", return_value="Linux"):
            plan, _ = self._uninstall()
        self.assertNotIn("Windows locks", plan)     # POSIX deletes a running script fine
        self.assertIn("run 'agentbell uninstall --yes' to delete everything", plan)

    def test_foreign_launcher_with_our_name_stays(self):
        user_base, user_site, launcher = self._pip_user_install(
            _windows_launcher(module="othertool"))
        with unittest.mock.patch.object(an, "_user_site_dirs",
                                        return_value=(user_base, user_site)):
            self._uninstall("--yes")
        self.assertTrue(os.path.exists(launcher))


class TestUninstallMessages(_UninstallFixture):

    def test_fresh_start_hint_does_not_assume_a_checkout(self):
        os.environ[an.CONFIG_DIR_ENV] = os.path.join(self.root, "cfg")
        os.environ[an.STATE_DIR_ENV] = os.path.join(self.root, "state")
        self._write(an.config_path(), "{}")
        out, _ = self._uninstall("--yes")
        self.assertIn("pipx install agentbell && agentbell init", out)
        self.assertIn("./install.sh from a checkout", out)

    def test_windows_locked_launcher_failure_says_how_to_finish(self):
        entry = {"kind": "binary", "label": "pip --user script C:\\x\\agentbell.exe",
                 "action": "delete", "apply": unittest.mock.Mock(
                     side_effect=PermissionError(13, "Access is denied",
                                                 "C:\\x\\agentbell.exe"))}
        report = {"entries": [entry], "warnings": []}
        parser = an.build_parser()
        args = parser.parse_args(["uninstall", "--yes", "--project", self.project])
        out = io.StringIO()
        with unittest.mock.patch.object(an, "purge_report", return_value=report), \
                redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            an.cmd_uninstall(args)
        self.assertEqual(raised.exception.code, 1)
        text = out.getvalue()
        self.assertIn("failed   pip --user script C:\\x\\agentbell.exe", text)
        self.assertIn("delete it by hand", text)
        # the module is gone by now, so a re-run cannot finish the job
        self.assertIn("1 step(s) failed - see above\n", text)
        self.assertNotIn("re-run", text)

    def test_other_permission_errors_keep_the_rerun_advice(self):
        entry = {"kind": "binary", "label": "pip --user module /x/agentbell.py",
                 "action": "delete", "apply": unittest.mock.Mock(
                     side_effect=PermissionError(13, "Permission denied", "/x/agentbell.py"))}
        parser = an.build_parser()
        args = parser.parse_args(["uninstall", "--yes", "--project", self.project])
        out = io.StringIO()
        with unittest.mock.patch.object(an, "purge_report",
                                        return_value={"entries": [entry], "warnings": []}), \
                redirect_stdout(out), self.assertRaises(SystemExit):
            an.cmd_uninstall(args)
        self.assertNotIn("Windows locks", out.getvalue())
        self.assertIn("1 step(s) failed - see above; re-run 'agentbell uninstall --yes'",
                      out.getvalue())


if __name__ == "__main__":
    unittest.main()
