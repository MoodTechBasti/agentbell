"""Audit 3, cluster "files-uninstall": regression tests.

FU-1  a block marker inside user text never makes that text agentbell's
FU-8  removing a block keeps every other byte; a user's `.clinerules` stays
FU-2  a generic state subdir (runs, queue) keeps someone else's files
FU-4/S6  an unlistable shared dir is not reported as fully removed
FU-5  pip --user: agentbell-extras is not agentbell
FU-6  a symlinked config file: the report says the target stays
FU-7  a symlinked shared dir: the report says the link stays
FU-9  the STATE_DIR_NAMES guard sees names passed to the pending helpers
S1/FU-3/IW-2  no test resolves a path outside the test root
IW-12 the integration-guide test does not depend on the checkout path length
"""

import ast
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agentbell as an  # noqa: E402
import test_agentbell as base  # noqa: E402

NEEDS_SYMLINK = unittest.skipIf(os.name == "nt", "os.symlink needs admin or developer mode")
AIDER_BLOCK = (f"{an.BLOCK_START}\n{an._instructions_text('aider').rstrip()}\n"
               f"{an.BLOCK_END}\n")


class _Fixture(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="agentbell-audit3-fu-")
        self.home = os.path.join(self.root, "home")
        self.project = os.path.join(self.root, "project")
        os.makedirs(self.home)
        os.makedirs(self.project)
        old_home = base._set_home(self.home)
        self.addCleanup(base._restore_home, old_home)
        old_env = {key: os.environ.get(key)
                   for key in (an.CONFIG_DIR_ENV, an.STATE_DIR_ENV, an.CONFIG_FILE_ENV)}
        self.addCleanup(base._restore_home, old_env)
        os.environ[an.CONFIG_DIR_ENV] = os.path.join(self.root, "cfg")
        os.environ[an.STATE_DIR_ENV] = os.path.join(self.root, "state")
        old_argv0 = sys.argv[0]
        sys.argv[0] = base._fake_launcher(self.root)
        self.addCleanup(sys.argv.__setitem__, 0, old_argv0)
        for name, value in (("_pipx_installed", None), ("_user_site_dirs", (None, None))):
            patcher = unittest.mock.patch.object(an, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.root, True)

    def _write(self, path, content="x"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        return path

    def _read(self, path):
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def _uninstall(self, *extra, code=None):
        args = an.build_parser().parse_args(["uninstall", "--project", self.project, *extra])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                args.func(args)
                exit_code = 0
            except SystemExit as exc:
                exit_code = exc.code
        self.assertEqual(exit_code, code or 0, out.getvalue() + err.getvalue())
        return out.getvalue(), err.getvalue()

    def _hooks(self, agent, add=True):
        return an.install_hooks(agent, project=self.project, add=add)


class TestStrayMarkersFU1(_Fixture):
    """A doc line that mentions a marker joined the next real block into one
    match, and repair / uninstall / purge deleted the user text between."""

    DOC = ("# Rules\n\nOur tool writes `<!-- agentbell:start -->` markers.\n\n"
           "## Important team rules\n- never push to main\n\n")

    def setUp(self):
        super().setUp()
        self.agents_md = os.path.join(self.project, "AGENTS.md")

    def test_marker_in_user_text_above_a_block_stays_user_text(self):
        self._write(self.agents_md, self.DOC + AIDER_BLOCK)
        self.assertEqual(an.aider_block_state(self.project), "current")
        self.assertFalse(self._hooks("aider")["changed"])
        result = self._hooks("aider", add=False)
        self.assertTrue(result["changed"])
        self.assertEqual(self._read(self.agents_md), self.DOC)

    def test_repair_of_an_outdated_block_keeps_the_text_above(self):
        self._write(self.agents_md, self.DOC + AIDER_BLOCK.replace("run_failed", "failed"))
        self.assertEqual(an.aider_block_state(self.project), "outdated")
        self.assertTrue(self._hooks("aider")["changed"])
        self.assertEqual(self._read(self.agents_md), self.DOC + AIDER_BLOCK)

    def test_purge_leaves_a_doc_that_mentions_both_markers(self):
        doc = ("# Agent notes\n\nagentbell wraps its block in `<!-- agentbell:start -->`.\n\n"
               "## Team rules (keep!)\n- never force-push\n\n"
               "The block ends with `<!-- agentbell:end -->`.\n")
        self._write(self.agents_md, doc)
        labels = [e["label"] for e in an.purge_report(self.project)["entries"]]
        self.assertFalse([label for label in labels if "AGENTS.md" in label], labels)
        self._uninstall("--yes")
        self.assertEqual(self._read(self.agents_md), doc)

    def test_purge_removes_only_blocks_that_run_agentbell(self):
        foreign = f"{an.BLOCK_START}\nsomeone else's notes\n{an.BLOCK_END}\n"
        self._write(self.agents_md, "# Notes\n\n" + foreign + AIDER_BLOCK)
        self._uninstall("--yes")
        self.assertEqual(self._read(self.agents_md), "# Notes\n\n" + foreign)

    def test_opencode_install_keeps_text_before_a_legacy_block(self):
        doc = "# Notes\n\nDo not edit inside `<!-- agentbell:start -->` blocks.\n\n- keep me\n\n"
        legacy = f"{an.BLOCK_START}\n{an.OPENCODE_INSTRUCTIONS.rstrip()}\n{an.BLOCK_END}\n"
        self._write(self.agents_md, doc + legacy)
        self._hooks("opencode")
        self.assertEqual(self._read(self.agents_md), doc)

    def test_a_marker_line_without_its_partner_is_left_alone_visibly(self):
        # the user deleted our end marker once: the next end marker belongs
        # to another block, and everything between would be taken for ours
        text = (f"{an.BLOCK_START}\nold notes\n\n## Team rules\n- never push to main\n\n"
                + AIDER_BLOCK)
        self._write(self.agents_md, text)
        self.assertEqual(an.aider_block_state(self.project), "absent")
        install = self._hooks("aider")
        self.assertFalse(install["changed"])
        self.assertTrue(any("left it unchanged" in note for note in install["notes"]))
        remove = self._hooks("aider", add=False)
        self.assertFalse(remove["changed"])
        self.assertTrue(any("stray marker" in note for note in remove["notes"]), remove)
        out, _ = self._uninstall("--yes", code=1)
        self.assertIn(f"failed   agentbell block(s) in {self.agents_md}", out)
        self.assertEqual(self._read(self.agents_md), text)

    def test_opencode_names_a_stray_marker_only_near_its_own_block(self):
        text = f"{an.BLOCK_START}\nnotes\n" + AIDER_BLOCK
        self._write(self.agents_md, text)
        self.assertEqual(self._hooks("opencode").get("notes"), [])
        legacy = f"{an.BLOCK_START}\n{an.OPENCODE_INSTRUCTIONS.rstrip()}\n{an.BLOCK_END}\n"
        self._write(self.agents_md, f"{an.BLOCK_START}\nnotes\n" + legacy)
        self.assertTrue(self._hooks("opencode")["notes"])


class TestBlockRemovalKeepsBytesFU8(_Fixture):

    def test_install_then_uninstall_gives_back_the_same_bytes(self):
        originals = ["# Rules\r\n\r\n- keep\r\n\r\n\r\n", "\n\n  # indented\n", "a\n\n\n",
                     "# Rules\n", "﻿# BOM\r\n"]
        path = os.path.join(self.project, "AGENTS.md")
        for original in originals:
            with self.subTest(original=original):
                self._write(path, original)
                self.assertTrue(self._hooks("aider")["changed"])
                self.assertTrue(self._hooks("aider", add=False)["changed"])
                self.assertEqual(self._read(path), original)

    def test_users_empty_clinerules_file_is_kept(self):
        path = self._write(os.path.join(self.project, ".clinerules"), "")
        self.assertTrue(self._hooks("cline")["changed"])
        self.assertTrue(self._hooks("cline", add=False)["changed"])
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(self._read(path), "")

    def test_a_file_agentbell_created_goes_again(self):
        path = os.path.join(self.project, ".continue", "rules", "agentbell.md")
        self.assertTrue(self._hooks("continue")["changed"])
        self.assertTrue(self._hooks("continue", add=False)["changed"])
        self.assertFalse(os.path.exists(path))


class TestSharedStateSubdirsFU2(_Fixture):

    def setUp(self):
        super().setUp()
        self.sdir = os.path.join(self.home, ".local", "state")
        os.environ[an.STATE_DIR_ENV] = self.sdir

    def test_foreign_files_in_a_generic_subdir_survive(self):
        model = self._write(os.path.join(self.sdir, "runs", "experiment-42", "model.bin"))
        readme = self._write(os.path.join(self.sdir, "runs", "README.txt"))
        results = self._write(os.path.join(self.sdir, "queue", "results.json.bak"))
        an.write_history({"event": "notify", "message": "m"})
        an.write_start_marker("claude", session_id="s1")
        an.claim_hook_send("claude", "run_completed", "m", window=60)
        an.enqueue_item(base.make_config("http://127.0.0.1:9"),
                        {"message": "queued", "channels": ["ntfy"], "event": "notify"})
        plan, _ = self._uninstall()
        self.assertIn("the directory stays: it also holds queue/results.json.bak, "
                      "runs/README.txt, runs/experiment-42", plan)
        out, _ = self._uninstall("--yes")
        for path in (model, readme, results):
            self.assertTrue(os.path.exists(path), out)
        self.assertEqual(sorted(os.listdir(os.path.join(self.sdir, "runs"))),
                         ["README.txt", "experiment-42"])
        self.assertEqual(os.listdir(os.path.join(self.sdir, "queue")), ["results.json.bak"])
        self.assertIn(f"kept     {self.sdir}: queue/results.json.bak, runs/README.txt, "
                      "runs/experiment-42 (not agentbell's)", out)
        self.assertEqual(an.purge_report(self.project)["entries"], [])


@unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                 "needs POSIX permissions that bind the user")
class TestUnlistableSharedDirFU4(_Fixture):

    def test_is_listed_and_fails_instead_of_already_removed(self):
        sdir = os.path.join(self.root, "ab-state")
        os.environ[an.STATE_DIR_ENV] = sdir
        history = self._write(os.path.join(sdir, "history.jsonl"), '{"message": "secret"}\n')
        os.chmod(sdir, 0o300)
        self.addCleanup(os.chmod, sdir, 0o700)
        plan, _ = self._uninstall()
        self.assertNotIn("already fully removed", plan)
        self.assertIn(f"agentbell's state files in {sdir}", plan)
        self.assertIn(f"cannot list {sdir} (Permission denied)", plan)
        out, _ = self._uninstall("--yes", code=1)
        self.assertIn(f"failed   agentbell's state files in {sdir}", out)
        os.chmod(sdir, 0o700)
        self.assertTrue(os.path.exists(history))


class TestPipUserExactNamesFU5(_Fixture):

    def test_agentbell_extras_is_not_agentbell(self):
        user_site = os.path.join(self.root, "site")
        extras = [os.path.join(user_site, "agentbell_extras-0.1.dist-info", "METADATA"),
                  os.path.join(user_site, "agentbell_extras", "__init__.py"),
                  os.path.join(user_site, "__pycache__",
                               "agentbell_extras_helper.cpython-312.pyc")]
        ours = [os.path.join(user_site, "agentbell-1.6.3.dist-info", "METADATA"),
                os.path.join(user_site, "agentbell.egg-info", "PKG-INFO"),
                os.path.join(user_site, "agentbell.py"),
                os.path.join(user_site, "__pycache__", "agentbell.cpython-312.pyc")]
        for path in extras + ours:
            self._write(path)
        with unittest.mock.patch.object(an, "_user_site_dirs",
                                        return_value=(self.root, user_site)):
            labels = [e["label"] for e in an.purge_report(self.project)["entries"]]
            self._uninstall("--yes")
        self.assertFalse([label for label in labels if "extras" in label], labels)
        self.assertEqual(len([label for label in labels if "pip --user" in label]), 4, labels)
        for path in extras:
            self.assertTrue(os.path.exists(path), path)
        for path in ours:
            self.assertFalse(os.path.exists(path), path)


@NEEDS_SYMLINK
class TestSymlinksFU6FU7(_Fixture):

    def test_symlinked_config_file_says_the_target_keeps_the_key(self):
        target = self._write(os.path.join(self.root, "dotfiles", "agentbell.json"),
                             json.dumps({"license": "AB1-secret-key"}))
        link = os.path.join(self.home, "ab.json")
        os.symlink(target, link)
        os.environ[an.CONFIG_FILE_ENV] = link
        report = an.purge_report(self.project)
        entry = next(e for e in report["entries"] if link in e["label"])
        self.assertNotIn("license key", entry["action"])
        self.assertTrue(any(target in w and "license key" in w for w in report["warnings"]),
                        report["warnings"])
        _, err = self._uninstall("--yes")
        self.assertIn(target, err)
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(os.path.exists(target))

    def test_symlinked_config_json_in_the_default_dir_is_named_too(self):
        os.environ.pop(an.CONFIG_DIR_ENV)
        target = self._write(os.path.join(self.root, "dotfiles", "config.json"), "{}")
        os.makedirs(an.config_dir())
        os.symlink(target, an.config_path())
        warnings = an.purge_report(self.project)["warnings"]
        self.assertTrue(any(target in w for w in warnings), warnings)

    def test_symlinked_shared_dir_says_the_link_stays(self):
        target = os.path.join(self.root, "real-state")
        link = os.path.join(self.home, "state-link")
        os.makedirs(target)
        os.symlink(target, link)
        os.environ[an.STATE_DIR_ENV] = link
        an.write_history({"event": "notify", "message": "m"})
        plan, _ = self._uninstall()
        self.assertIn("the link and the directory it points to stay", plan)
        self.assertNotIn("then the directory", plan)
        out, _ = self._uninstall("--yes")
        self.assertIn(f"kept     {link} (a link) and {os.path.realpath(target)}", out)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.listdir(target), [])


def _state_names_written(source):
    """Top-level state-dir names the source writes, including names passed
    through helpers that end in _pending_dir(name) or _consumed_path(name)."""
    tree = ast.parse(source)
    patterns = {"_pending_dir": "{}", "_consumed_path": "{}-consumed"}
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]

    def calls(node):
        return [c for c in ast.walk(node) if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name) and c.func.id in patterns and c.args]

    changed = True
    while changed:                   # helpers that pass their first parameter on
        changed = False
        for func in functions:
            if func.name in patterns or not func.args.args:
                continue
            param = func.args.args[0].arg
            for call in calls(func):
                if isinstance(call.args[0], ast.Name) and call.args[0].id == param:
                    patterns[func.name] = patterns[call.func.id]
                    changed = True
                    break
    return {patterns[c.func.id].format(c.args[0].value) for c in calls(tree)
            if isinstance(c.args[0], ast.Constant) and isinstance(c.args[0].value, str)}


class TestStateNamesGuardFU9(unittest.TestCase):

    def setUp(self):
        with open(an.__file__, "r", encoding="utf-8") as fh:
            self.source = fh.read()

    def test_every_pending_and_consumed_name_is_covered(self):
        written = _state_names_written(self.source)
        self.assertTrue({"tg-pending", "ntfy-pending", "ntfy-consumed"} <= written, written)
        self.assertEqual(sorted(written - set(an.STATE_DIR_NAMES)), [])

    def test_guard_sees_a_name_passed_to_write_pending(self):
        # write_pending("ntfy-pending", ...) never spells _pending_dir("ntfy-pending")
        self.assertNotIn('_pending_dir("ntfy-pending")', self.source)
        source = self.source + '\n\ndef _new_channel():\n    write_pending("slack-pending", 1, "m", 1)\n'
        self.assertIn("slack-pending", _state_names_written(source))


class TestTestsStayInTheirRootS1(unittest.TestCase):
    """The suite used to move only HOME/USERPROFILE: KIMI_CODE_HOME, QWEN_HOME,
    APPDATA or PYTHONUSERBASE set on the machine led tests into live configs
    and the real pip --user install."""

    def _resolved(self):
        paths = [os.path.expanduser("~"), an._home(),
                 os.environ.get("XDG_BIN_HOME") or os.path.join(an._home(), ".local", "bin")]
        for name, func in inspect.getmembers(an, inspect.isfunction):
            if not name.endswith(("_path", "_paths", "_dir", "_dirs")) or name in (
                    "_project_dir", "ensure_state_dir"):
                continue
            if any(p.default is p.empty for p in inspect.signature(func).parameters.values()):
                continue
            for system in ("Linux", "Darwin", "Windows"):
                with unittest.mock.patch.object(an.platform, "system", return_value=system):
                    value = func()
                for item in (value if isinstance(value, (tuple, list)) else [value]):
                    paths += item if isinstance(item, list) else [item]
        return [p for p in paths if p]

    def _assert_inside(self, *roots):
        roots = [os.path.realpath(root) for root in roots]
        outside = [p for p in self._resolved() if not any(
            os.path.realpath(p) == root or os.path.realpath(p).startswith(root + os.sep)
            for root in roots)]
        self.assertEqual(outside, [])

    def test_every_env_derived_path_is_inside_the_test_root(self):
        self._assert_inside(base._TEST_ROOT)

    def test_set_home_overrides_what_a_developer_has_set(self):
        real = tempfile.mkdtemp(prefix="agentbell-real-")
        home = tempfile.mkdtemp(prefix="agentbell-home-")
        self.addCleanup(shutil.rmtree, real, True)
        self.addCleanup(shutil.rmtree, home, True)
        keys = ("APPDATA", "LOCALAPPDATA") + base._HOME_OVERRIDES
        saved = {key: os.environ.get(key) for key in keys}
        self.addCleanup(base._restore_home, saved)
        for key in keys:
            os.environ[key] = os.path.join(real, key.lower())
        old = base._set_home(home)
        self.addCleanup(base._restore_home, old)
        self._assert_inside(home, base._TEST_ROOT)      # AGENTBELL_*_DIR stay in the root

    def test_purge_tests_leave_the_real_pip_user_install(self):
        real = tempfile.mkdtemp(prefix="agentbell-userbase-")
        self.addCleanup(shutil.rmtree, real, True)
        env = dict(os.environ, PYTHONUSERBASE=real, APPDATA=real)
        user_site = subprocess.run([sys.executable, "-m", "site", "--user-site"], env=env,
                                   capture_output=True, text=True, check=True).stdout.strip()
        module = os.path.join(user_site, "agentbell.py")
        metadata = os.path.join(user_site, "agentbell-1.6.3.dist-info", "METADATA")
        for path in (module, metadata):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("# agentbell\n")
        saved = {key: os.environ.get(key) for key in ("PYTHONUSERBASE", "APPDATA")}
        self.addCleanup(base._restore_home, saved)
        os.environ.update(PYTHONUSERBASE=real, APPDATA=real)
        result = unittest.TestResult()
        base.TestPurge("test_purge_second_run_reports_nothing").run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertTrue(os.path.exists(module))
        self.assertTrue(os.path.exists(metadata))


class TestGuideAdjacencyIW12(unittest.TestCase):

    def test_long_checkout_path_does_not_break_the_guide_test(self):
        binary = os.path.join(os.sep, "x" * 200, "agentbell")
        result = unittest.TestResult()
        with unittest.mock.patch.object(an, "agentbell_binary", return_value=binary):
            self.assertIn(binary, an.integration_guide(an.integration_manifest()))
            base.TestIntegrationGuide(
                "test_silent_and_min_duration_are_coupled_in_one_section").run(result)
        self.assertTrue(result.wasSuccessful(), result.failures)


if __name__ == "__main__":
    unittest.main()
