"""PATH-isolated udiskie-unlock installer helpers. No KVM, no real udiskie."""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

from strataqemu.tests_spec import INSTALL_SH_FLAGS, install_sh_argv

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE = REPO_ROOT / "guest-tests" / "smoke-udiskie-unlock.sh"
FIXTURE_WITH = (
    REPO_ROOT / "tests" / "fixtures" / "udiskie-unlock" / "install-with-helpers.sh"
)
FIXTURE_WITHOUT = (
    REPO_ROOT / "tests" / "fixtures" / "udiskie-unlock" / "install-without-helpers.sh"
)

OTHER_WITH = (
    "WITH_SMB",
    "WITH_RAW",
    "WITH_DESKTOP_ENTRY",
    "WITH_FOLDER_ASSOCIATION",
    "WITH_FILE_MANAGER",
    "WITH_FILE_CHOOSER",
    "WITH_OMARCHY_KEYBINDS",
)

NEWER_RELEASE = "newer release"
SETTINGS = "Settings"
INSTALL_FLAG = "--install-udiskie-unlock"


def _kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    argv: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key == "BIN_ARGV":
            argv.append(value)
            continue
        out[key] = value
    out["_argv"] = "\n".join(argv)
    return out


class SmokeUdiskieUnlockTests(unittest.TestCase):
    def test_scripts_are_executable(self) -> None:
        self.assertTrue(SMOKE.is_file())
        self.assertTrue(os.access(SMOKE, os.X_OK))
        self.assertTrue(FIXTURE_WITH.is_file())
        self.assertTrue(FIXTURE_WITHOUT.is_file())
        for path in (SMOKE, FIXTURE_WITH, FIXTURE_WITHOUT):
            result = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def _run(
        self,
        *,
        install_sh: Path = FIXTURE_WITH,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "INSTALL_SH": str(install_sh)}
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(SMOKE)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_parse_args_with_udiskie_unlock_sets_non_interactive_and_flag(self) -> None:
        proc = self._run(
            extra_env={
                "SMOKE_UDISKIE_CASE": "parse-args",
                "SMOKE_UDISKIE_ARGS": "--with-udiskie-unlock",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        kv = _kv(proc.stdout)
        self.assertEqual(kv["NON_INTERACTIVE"], "yes")
        self.assertEqual(kv["WITH_UDISKIE_UNLOCK"], "yes")
        for name in OTHER_WITH:
            self.assertEqual(kv[name], "ask", name)

    def test_parse_args_non_interactive_leaves_udiskie_ask(self) -> None:
        proc = self._run(
            extra_env={
                "SMOKE_UDISKIE_CASE": "parse-args",
                "SMOKE_UDISKIE_ARGS": "--non-interactive",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        kv = _kv(proc.stdout)
        self.assertEqual(kv["NON_INTERACTIVE"], "yes")
        self.assertEqual(kv["WITH_UDISKIE_UNLOCK"], "ask")

    def test_parse_args_omarchy_keybinds_does_not_set_udiskie(self) -> None:
        proc = self._run(
            extra_env={
                "SMOKE_UDISKIE_CASE": "parse-args",
                "SMOKE_UDISKIE_ARGS": "--with-omarchy-keybinds",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        kv = _kv(proc.stdout)
        self.assertEqual(kv["WITH_OMARCHY_KEYBINDS"], "yes")
        self.assertEqual(kv["WITH_UDISKIE_UNLOCK"], "ask")
        self.assertNotEqual(kv["WITH_UDISKIE_UNLOCK"], "yes")

    def test_unknown_with_udiskie_is_rejected(self) -> None:
        proc = self._run(
            extra_env={
                "SMOKE_UDISKIE_CASE": "parse-args",
                "SMOKE_UDISKIE_ARGS": "--with-udiskie",
            }
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Unknown option", proc.stderr)
        self.assertIn("--with-udiskie", proc.stderr)

    def test_configure_decision_tree(self) -> None:
        cases: list[dict] = [
            {
                "name": "omarchy4-stub-marker-prompt-yes-execs",
                "env": {
                    "SMOKE_OMARCHY_MAJOR": "4",
                    "SMOKE_ARCH_BASED": "no",
                    "SMOKE_UDISKIE_STUB": "1",
                    "SMOKE_MARKER": "1",
                    "SMOKE_PROMPT": "yes",
                },
                "rc": 0,
                "calls": 1,
                "stderr_has": (),
                "stderr_not": (NEWER_RELEASE,),
            },
            {
                "name": "omarchy3-stub-marker-prompt-yes-execs",
                "env": {
                    "SMOKE_OMARCHY_MAJOR": "3",
                    "SMOKE_ARCH_BASED": "yes",
                    "SMOKE_UDISKIE_STUB": "1",
                    "SMOKE_MARKER": "1",
                    "SMOKE_PROMPT": "yes",
                },
                "rc": 0,
                "calls": 1,
                "stderr_has": (),
                "stderr_not": (NEWER_RELEASE,),
            },
            {
                "name": "omarchy4-stub-marker-prompt-no-no-exec",
                "env": {
                    "SMOKE_OMARCHY_MAJOR": "4",
                    "SMOKE_UDISKIE_STUB": "1",
                    "SMOKE_MARKER": "1",
                    "SMOKE_PROMPT": "no",
                },
                "rc": 0,
                "calls": 0,
                "stderr_has": (),
                "stderr_not": (NEWER_RELEASE,),
            },
            {
                "name": "omarchy4-stub-marker-ask-default-no",
                "env": {
                    "SMOKE_OMARCHY_MAJOR": "4",
                    "SMOKE_UDISKIE_STUB": "1",
                    "SMOKE_MARKER": "1",
                },
                "rc": 0,
                "calls": 0,
                "stderr_has": (),
                "stderr_not": (NEWER_RELEASE,),
            },
            {
                "name": "omarchy4-no-stub-ask-warns",
                "env": {
                    "SMOKE_OMARCHY_MAJOR": "4",
                    "SMOKE_MARKER": "1",
                },
                "rc": 0,
                "calls": 0,
                "stderr_has": ("udiskie",),
                "stderr_not": (INSTALL_FLAG, NEWER_RELEASE),
            },
            {
                "name": "omarchy4-no-stub-flag-dies-path",
                "env": {
                    "SMOKE_OMARCHY_MAJOR": "4",
                    "SMOKE_UDISKIE_ARGS": "--with-udiskie-unlock",
                    "SMOKE_MARKER": "1",
                },
                "rc": 1,
                "calls": 0,
                "stderr_has": ("udiskie is not on PATH",),
                "stderr_not": (NEWER_RELEASE,),
            },
            {
                "name": "arch-stub-marker-ask-default-no",
                "env": {
                    "SMOKE_ARCH_BASED": "yes",
                    "SMOKE_UDISKIE_STUB": "1",
                    "SMOKE_MARKER": "1",
                },
                "rc": 0,
                "calls": 0,
                "stderr_has": (),
                "stderr_not": (NEWER_RELEASE,),
            },
            {
                "name": "arch-stub-marker-prompt-yes-execs",
                "env": {
                    "SMOKE_ARCH_BASED": "yes",
                    "SMOKE_UDISKIE_STUB": "1",
                    "SMOKE_MARKER": "1",
                    "SMOKE_PROMPT": "yes",
                },
                "rc": 0,
                "calls": 1,
                "stderr_has": (),
                "stderr_not": (NEWER_RELEASE,),
            },
            {
                "name": "arch-no-stub-ask-silent",
                "env": {
                    "SMOKE_ARCH_BASED": "yes",
                    "SMOKE_MARKER": "1",
                },
                "rc": 0,
                "calls": 0,
                "silent": True,
            },
            {
                "name": "arch-no-stub-host-udiskie-ignored",
                "env": {
                    "SMOKE_ARCH_BASED": "yes",
                    "SMOKE_HOST_UDISKIE": "1",
                    "SMOKE_MARKER": "1",
                },
                "rc": 0,
                "calls": 0,
                "silent": True,
                "udiskie_on_path": "0",
            },
            {
                "name": "non-arch-non-omarchy-ask-silent",
                "env": {
                    "SMOKE_ARCH_BASED": "no",
                    "SMOKE_UDISKIE_STUB": "1",
                    "SMOKE_MARKER": "1",
                },
                "rc": 0,
                "calls": 0,
                "silent": True,
                "stderr_not": (NEWER_RELEASE, "encrypted-volume"),
            },
            {
                "name": "non-arch-flag-dies-eligibility",
                "env": {
                    "SMOKE_ARCH_BASED": "no",
                    "SMOKE_UDISKIE_STUB": "1",
                    "SMOKE_MARKER": "1",
                    "SMOKE_UDISKIE_ARGS": "--with-udiskie-unlock",
                },
                "rc": 1,
                "calls": 0,
                "stderr_has": (
                    "requires Omarchy 3 or 4, or an Arch-based system with udiskie on PATH",
                ),
            },
            {
                "name": "eligible-ask-missing-marker-warns",
                "env": {
                    "SMOKE_OMARCHY_MAJOR": "4",
                    "SMOKE_UDISKIE_STUB": "1",
                },
                "rc": 0,
                "calls": 0,
                "stderr_has": ("encrypted-volume unlock",),
                "stderr_not": (INSTALL_FLAG,),
                "release_supports": "0",
            },
            {
                "name": "eligible-flag-missing-marker-dies",
                "env": {
                    "SMOKE_OMARCHY_MAJOR": "4",
                    "SMOKE_UDISKIE_STUB": "1",
                    "SMOKE_UDISKIE_ARGS": "--with-udiskie-unlock",
                },
                "rc": 1,
                "calls": 0,
                "stderr_has": (NEWER_RELEASE,),
            },
            {
                "name": "ineligible-ask-missing-marker-silent",
                "env": {
                    "SMOKE_ARCH_BASED": "no",
                },
                "rc": 0,
                "calls": 0,
                "silent": True,
                "stderr_not": (NEWER_RELEASE, "encrypted-volume"),
            },
            {
                "name": "bin-nonzero-omarchy-mentions-settings-and-cli",
                "env": {
                    "SMOKE_OMARCHY_MAJOR": "4",
                    "SMOKE_UDISKIE_STUB": "1",
                    "SMOKE_MARKER": "1",
                    "SMOKE_PROMPT": "yes",
                    "SMOKE_BIN_EXIT": "1",
                },
                "rc": 1,
                "calls": 1,
                "stderr_has": (SETTINGS, INSTALL_FLAG),
            },
            {
                "name": "bin-nonzero-arch-cli-only",
                "env": {
                    "SMOKE_ARCH_BASED": "yes",
                    "SMOKE_UDISKIE_STUB": "1",
                    "SMOKE_MARKER": "1",
                    "SMOKE_PROMPT": "yes",
                    "SMOKE_BIN_EXIT": "1",
                },
                "rc": 1,
                "calls": 1,
                "stderr_has": (INSTALL_FLAG,),
                "stderr_not": (SETTINGS,),
            },
        ]
        for case in cases:
            with self.subTest(case["name"]):
                proc = self._run(extra_env={"SMOKE_UDISKIE_CASE": "configure", **case["env"]})
                blob = proc.stderr + proc.stdout
                self.assertEqual(proc.returncode, case["rc"], blob)
                kv = _kv(proc.stdout)
                self.assertEqual(int(kv.get("BIN_CALLS", "0")), case["calls"], blob)
                self.assertEqual(kv.get("UDISKIE_RAN", "1"), "0", blob)
                self.assertEqual(int(kv.get("DECOY_CALLS", "1")), 0, blob)
                argv_lines = [
                    line.partition("=")[2]
                    for line in proc.stdout.splitlines()
                    if line.startswith("BIN_ARGV=")
                ]
                if case["calls"]:
                    self.assertEqual(argv_lines, [INSTALL_FLAG] * case["calls"], blob)
                    bin_path = kv["BIN_PATH"]
                    extracted = kv["EXTRACTED"]
                    self.assertTrue(bin_path)
                    self.assertFalse(
                        Path(bin_path).resolve().is_relative_to(Path(extracted).resolve()),
                        blob,
                    )
                else:
                    self.assertEqual(argv_lines, [], blob)
                for needle in case.get("stderr_has", ()):
                    self.assertIn(needle, proc.stderr, blob)
                for needle in case.get("stderr_not", ()):
                    self.assertNotIn(needle, proc.stderr, blob)
                if case.get("silent"):
                    self.assertNotIn("warning:", proc.stderr)
                    self.assertNotIn("error:", proc.stderr)
                    self.assertNotIn(NEWER_RELEASE, blob)
                if "udiskie_on_path" in case:
                    self.assertEqual(kv.get("UDISKIE_ON_PATH"), case["udiskie_on_path"], blob)
                if "release_supports" in case:
                    self.assertEqual(kv.get("RELEASE_SUPPORTS"), case["release_supports"], blob)
                path_used = kv.get("PATH_USED", "")
                self.assertTrue(path_used, blob)
                self.assertNotIn(os.pathsep, path_used)
                self.assertNotIn("/usr/bin", path_used)
                self.assertNotIn("/usr/local/bin", path_used)

    def test_host_udiskie_on_real_path_is_ignored(self) -> None:
        """Arch + no stub stays ineligible even if the host has udiskie."""
        host_udiskie = shutil.which("udiskie")
        proc = self._run(
            extra_env={
                "SMOKE_UDISKIE_CASE": "configure",
                "SMOKE_ARCH_BASED": "yes",
                "SMOKE_HOST_UDISKIE": "1",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        kv = _kv(proc.stdout)
        self.assertEqual(kv["BIN_CALLS"], "0")
        self.assertEqual(kv["UDISKIE_ON_PATH"], "0")
        self.assertEqual(kv["UDISKIE_WHICH"], "")
        path_used = kv["PATH_USED"]
        self.assertNotIn(os.pathsep, path_used)
        if host_udiskie:
            self.assertNotIn(str(Path(host_udiskie).parent), path_used)
            self.assertNotEqual(kv["UDISKIE_WHICH"], host_udiskie)
        self.assertNotIn("warning:", proc.stderr)
        self.assertNotIn(NEWER_RELEASE, proc.stderr)

    def test_release_supports_false_without_marker_even_if_bin_source_has_flag(self) -> None:
        proc = self._run(
            extra_env={
                "SMOKE_UDISKIE_CASE": "configure",
                "SMOKE_OMARCHY_MAJOR": "4",
                "SMOKE_UDISKIE_STUB": "1",
                "SMOKE_BIN_HAS_FLAG_STRING": "1",
                "SMOKE_PROMPT": "yes",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        kv = _kv(proc.stdout)
        self.assertEqual(kv["RELEASE_SUPPORTS"], "0")
        self.assertEqual(kv["BIN_CALLS"], "0")
        self.assertEqual(kv["BIN_SOURCE_HAS_FLAG"], "1")
        self.assertNotIn(INSTALL_FLAG, proc.stderr)
        self.assertIn("encrypted-volume unlock", proc.stderr)

    def test_configure_twice_uses_only_permanent_bin_path(self) -> None:
        proc = self._run(
            extra_env={
                "SMOKE_UDISKIE_CASE": "configure",
                "SMOKE_OMARCHY_MAJOR": "4",
                "SMOKE_UDISKIE_STUB": "1",
                "SMOKE_MARKER": "1",
                "SMOKE_PROMPT": "yes",
                "SMOKE_CALL_TWICE": "1",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        kv = _kv(proc.stdout)
        self.assertEqual(kv["BIN_CALLS"], "2")
        self.assertEqual(kv["DECOY_CALLS"], "0")
        argv_lines = [
            line.partition("=")[2]
            for line in proc.stdout.splitlines()
            if line.startswith("BIN_ARGV=")
        ]
        self.assertEqual(argv_lines, [INSTALL_FLAG, INSTALL_FLAG])
        bin_path = Path(kv["BIN_PATH"]).resolve()
        extracted = Path(kv["EXTRACTED"]).resolve()
        self.assertFalse(bin_path.is_relative_to(extracted))
        self.assertIn("permanent", str(bin_path))

    def test_without_helpers_fail_closed(self) -> None:
        proc = self._run(
            install_sh=FIXTURE_WITHOUT,
            extra_env={
                "SMOKE_UDISKIE_CASE": "configure",
                "SMOKE_OMARCHY_MAJOR": "4",
                "SMOKE_UDISKIE_STUB": "1",
                "SMOKE_MARKER": "1",
                "SMOKE_PROMPT": "yes",
            },
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("configure_udiskie_unlock missing", proc.stderr)
        kv = _kv(proc.stdout)
        self.assertEqual(kv.get("BIN_CALLS", "0"), "0")

    def test_does_not_write_tester_real_udiskie_config(self) -> None:
        real_xdg = os.environ.get("XDG_CONFIG_HOME")
        if real_xdg:
            real_dir = Path(real_xdg) / "udiskie"
        else:
            real_dir = Path.home() / ".config" / "udiskie"
        real_yml = real_dir / "config.yml"
        before_exists = real_yml.exists()
        before_stat = real_yml.stat() if before_exists else None
        proc = self._run(
            extra_env={
                "SMOKE_UDISKIE_CASE": "configure",
                "SMOKE_OMARCHY_MAJOR": "4",
                "SMOKE_UDISKIE_STUB": "1",
                "SMOKE_MARKER": "1",
                "SMOKE_PROMPT": "yes",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(_kv(proc.stdout)["BIN_CALLS"], "1")
        if before_exists:
            assert before_stat is not None
            after = real_yml.stat()
            self.assertEqual(after.st_mtime_ns, before_stat.st_mtime_ns)
            self.assertEqual(after.st_size, before_stat.st_size)
        else:
            self.assertFalse(real_yml.exists())

    def test_install_from_flags_do_not_enable_udiskie_unlock(self) -> None:
        self.assertNotIn("--with-udiskie-unlock", INSTALL_SH_FLAGS)
        self.assertNotIn("--with-udiskie-unlock", install_sh_argv())
        self.assertNotIn(
            "--with-udiskie-unlock",
            install_sh_argv(archive="/tmp/strata-archive.tar.gz"),
        )

    def test_smoke_does_not_mention_pexpect_or_pipe_curl(self) -> None:
        text = SMOKE.read_text(encoding="utf-8")
        self.assertIn("STRATA_INSTALLER_TESTING=1", text)
        self.assertIn("configure_udiskie_unlock", text)
        self.assertIn("parse_args", text)
        self.assertNotIn("pexpect", text)
        self.assertNotIn("| bash", text)
        commands = [
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertFalse(any("udiskie --" in line for line in commands))
