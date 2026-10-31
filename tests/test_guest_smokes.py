"""Fixture-drive guest-tests smokes. No KVM."""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from strataqemu.tests_spec import (
    DEFAULT_UPDATE_FROM_VERSIONS,
    OMARCHY_DEV_HASH_OUTPUT,
    OMARCHY_TOKEN_CASES,
    parse_install_sh_sha256,
    parse_smoke_kv,
    previous_release_archive_name,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SESSION = REPO_ROOT / "guest-tests" / "smoke-session.sh"
SMOKE_DESKTOP = REPO_ROOT / "guest-tests" / "smoke-desktop.sh"
SMOKE_INSTALL = REPO_ROOT / "guest-tests" / "smoke-install.sh"
SMOKE_UPDATE = REPO_ROOT / "guest-tests" / "smoke-update.sh"
SMOKE_ABOUT = REPO_ROOT / "guest-tests" / "smoke-about.sh"
SMOKE_OMARCHY_DETECT = REPO_ROOT / "guest-tests" / "smoke-omarchy-detect.sh"
SMOKE_OMARCHY_BINDINGS = REPO_ROOT / "guest-tests" / "smoke-omarchy-bindings.sh"
SMOKE_APPARMOR_BWRAP = REPO_ROOT / "guest-tests" / "smoke-apparmor-bwrap.sh"
FIXTURE_PR743 = REPO_ROOT / "tests" / "fixtures" / "omarchy-detect" / "install-pr743.sh"
FIXTURE_MAIN = REPO_ROOT / "tests" / "fixtures" / "omarchy-detect" / "install-main.sh"
UID = 1000
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

LOGINCTL_FAKE = r"""#!/usr/bin/env bash
set -euo pipefail
FIXTURE="${SMOKE_FIXTURE:?}"
if [[ "${1:-}" == "--no-legend" && "${2:-}" == "list-sessions" ]]; then
  cat "$FIXTURE/list-sessions"
  exit 0
fi
if [[ "${1:-}" == "show-session" ]]; then
  sid="${2:-}"
  prop=""
  value_only=0
  shift 2 || true
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -p|--property)
        prop="${2:-}"
        shift 2
        ;;
      --value)
        value_only=1
        shift
        ;;
      *)
        shift
        ;;
    esac
  done
  file="$FIXTURE/session-$sid"
  if [[ ! -f "$file" ]]; then
    exit 1
  fi
  if [[ -n "$prop" ]]; then
    val="$(sed -n "s/^${prop}=//p" "$file" | head -n1)"
    if [[ "$value_only" == 1 ]]; then
      printf '%s\n' "$val"
    else
      printf '%s=%s\n' "$prop" "$val"
    fi
  else
    cat "$file"
  fi
  exit 0
fi
exit 1
"""

PGREP_FAKE = r"""#!/usr/bin/env bash
set -euo pipefail
FIXTURE="${SMOKE_FIXTURE:?}"
name=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -x)
      name="${2:-}"
      shift 2
      ;;
    *)
      name="$1"
      shift
      ;;
  esac
done
if [[ -f "$FIXTURE/proc-$name" ]]; then
  exit 0
fi
exit 1
"""

BUSCTL_FAKE = """#!/bin/sh
exit 0
"""

GDBUS_FAKE = r"""#!/usr/bin/env bash
set -euo pipefail
FIXTURE="${SMOKE_FIXTURE:?}"
if [[ "$*" == *NameHasOwner* ]]; then
  cat "$FIXTURE/gdbus-reply"
  exit 0
fi
exit 1
"""

GNOME_SCREENSHOT_FAKE = r"""#!/usr/bin/env bash
set -euo pipefail
dest=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-f" ]]; then
    dest="${2:-}"
    shift 2
    continue
  fi
  shift
done
if [[ -z "$dest" ]]; then
  exit 1
fi
# PNG magic + padding (gnome-screenshot oracle only checks the file exists)
printf '\x89PNG\r\n\x1a\n' > "$dest"
dd if=/dev/zero bs=256 count=1 >> "$dest" 2>/dev/null || true
"""

GRIM_FAKE = r"""#!/usr/bin/env bash
set -euo pipefail
dest="${1:-}"
if [[ -z "$dest" ]]; then
  exit 1
fi
printf '\x89PNG\r\n\x1a\n' > "$dest"
dd if=/dev/zero bs=256 count=1 >> "$dest" 2>/dev/null || true
"""

HYPRCTL_FAKE = r"""#!/usr/bin/env bash
set -euo pipefail
FIXTURE="${SMOKE_FIXTURE:?}"
if [[ "$*" == *clients* ]]; then
  cat "$FIXTURE/hyprctl-clients.json"
  exit 0
fi
exit 1
"""

JQ_FAKE = r"""#!/usr/bin/env bash
set -euo pipefail
input="$(cat)"
if [[ "$input" == *io.github.lgse.Strata* ]]; then
  printf '%s\n' "$input"
  exit 0
fi
exit 1
"""

HYPR_CLIENTS_HIT = """\
[{"class": "io.github.lgse.Strata", "title": "Strata"}]
"""

HYPR_CLIENTS_MISS = """\
[{"class": "kitty", "title": "term"}]
"""

MIXED_LIST = """\
c1 1000 tester seat0 tty2
c2 1000 tester - pts/0
"""

WAYLAND_SHOW = """\
Id=c1
User=1000
Name=tester
Seat=seat0
TTY=tty2
Type=wayland
Class=user
State=active
"""

TTY_SHOW = """\
Id=c2
User=1000
Name=tester
Seat=
TTY=pts/0
Type=tty
Class=user
State=active
"""

X11_SHOW = """\
Id=c1
User=1000
Name=tester
Seat=seat0
TTY=tty2
Type=x11
Class=user
State=active
"""


def _write_exec(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _unix_socket(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if path.exists():
        path.unlink()
    sock.bind(str(path))
    return sock


class SmokeSessionScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.fixture = self.tmp / "fixture"
        self.bindir = self.tmp / "bin"
        self.runtime = self.tmp / "run"
        self.fixture.mkdir()
        self.bindir.mkdir()
        self.runtime.mkdir()
        _write_exec(self.bindir / "loginctl", LOGINCTL_FAKE)
        _write_exec(self.bindir / "pgrep", PGREP_FAKE)
        _write_exec(self.bindir / "busctl", BUSCTL_FAKE)
        (self.fixture / "list-sessions").write_text(MIXED_LIST, encoding="utf-8")
        (self.fixture / "session-c1").write_text(WAYLAND_SHOW, encoding="utf-8")
        (self.fixture / "session-c2").write_text(TTY_SHOW, encoding="utf-8")
        (self.fixture / "proc-gnome-shell").write_text("1\n", encoding="utf-8")
        (self.runtime / "wayland-0.lock").write_bytes(b"lock")
        self._sock = _unix_socket(self.runtime / "wayland-0")

    def tearDown(self) -> None:
        self._sock.close()
        self._td.cleanup()

    def _env(self, **over: str) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = f"{self.bindir}{os.pathsep}{env.get('PATH', '')}"
        env["SMOKE_FIXTURE"] = str(self.fixture)
        env["SMOKE_UID"] = str(UID)
        env["SMOKE_RUNTIME_DIR"] = str(self.runtime)
        env["SMOKE_COMPOSITOR"] = "gnome-shell"
        env.pop("XDG_SESSION_ID", None)
        env.update(over)
        return env

    def _run(self, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        assert bash is not None
        return subprocess.run(
            [bash, str(SMOKE_SESSION)],
            check=False,
            capture_output=True,
            text=True,
            env=env if env is not None else self._env(),
        )

    def test_mixed_tty_wayland_picks_wayland_not_ssh(self) -> None:
        env = self._env(XDG_SESSION_ID="c2")
        proc = self._run(env)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("SESSION_ID=c1", proc.stdout)
        self.assertNotIn("SESSION_ID=c2", proc.stdout)
        self.assertIn("WAYLAND_DISPLAY=wayland-0", proc.stdout)
        self.assertNotIn("wayland-0.lock", proc.stdout)

    def test_refuses_ssh_xdg_session_id_even_if_wayland(self) -> None:
        env = self._env(XDG_SESSION_ID="c1")
        proc = self._run(env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("wayland", proc.stderr.lower())
        self.assertNotIn("SESSION_ID=c1", proc.stdout)

    def test_x11_is_hard_fail(self) -> None:
        (self.fixture / "session-c1").write_text(X11_SHOW, encoding="utf-8")
        proc = self._run(self._env(XDG_SESSION_ID="c2"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("x11", proc.stderr.lower())

    def test_lock_only_is_not_a_wayland_socket(self) -> None:
        self._sock.close()
        (self.runtime / "wayland-0").unlink(missing_ok=True)
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("wayland socket", proc.stderr.lower())

    def test_missing_compositor_fails(self) -> None:
        (self.fixture / "proc-gnome-shell").unlink()
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("compositor", proc.stderr.lower())

    def test_hyprland_exports_instance_signature(self) -> None:
        (self.fixture / "proc-Hyprland").write_text("1\n", encoding="utf-8")
        hypr = self.runtime / "hypr" / "sig-newer"
        hypr.mkdir(parents=True)
        older = self.runtime / "hypr" / "sig-older"
        older.mkdir()
        os.utime(older, (1, 1))
        os.utime(hypr, None)
        proc = self._run(self._env(SMOKE_COMPOSITOR="Hyprland"))
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("HYPRLAND_INSTANCE_SIGNATURE=sig-newer", proc.stdout)
        self.assertIn("WAYLAND_DISPLAY=wayland-0", proc.stdout)


class SmokeDesktopScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.fixture = self.tmp / "fixture"
        self.bindir = self.tmp / "bin"
        self.fixture.mkdir()
        self.bindir.mkdir()
        _write_exec(self.bindir / "gdbus", GDBUS_FAKE)
        _write_exec(self.bindir / "gnome-screenshot", GNOME_SCREENSHOT_FAKE)
        self.shot = self.tmp / "window.png"

    def tearDown(self) -> None:
        self._td.cleanup()

    def _env(self, *, owner: bool, with_screenshot: bool = True) -> dict[str, str]:
        env = os.environ.copy()
        path = str(self.bindir)
        if not with_screenshot:
            path = str(self.tmp / "empty-bin")
            Path(path).mkdir(exist_ok=True)
            _write_exec(Path(path) / "gdbus", GDBUS_FAKE)
            env["PATH"] = path
        else:
            env["PATH"] = f"{path}{os.pathsep}{env.get('PATH', '')}"
        env["SMOKE_FIXTURE"] = str(self.fixture)
        env["SMOKE_SCREENSHOT_PATH"] = str(self.shot)
        (self.fixture / "gdbus-reply").write_text(
            "(true,)\n" if owner else "(false,)\n",
            encoding="utf-8",
        )
        return env

    def _run(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        assert bash is not None
        return subprocess.run(
            [bash, str(SMOKE_DESKTOP)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_owner_passes_and_writes_png(self) -> None:
        proc = self._run(self._env(owner=True))
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertTrue(self.shot.is_file())
        self.assertTrue(self.shot.read_bytes().startswith(PNG_MAGIC))

    def test_not_owner_fails(self) -> None:
        proc = self._run(self._env(owner=False))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("io.github.lgse.Strata", proc.stderr)
        self.assertIn("not owned", proc.stderr.lower())

    def test_missing_gnome_screenshot_is_golden_bug(self) -> None:
        proc = self._run(self._env(owner=True, with_screenshot=False))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("screenshot tool missing; rebuild the golden", proc.stderr)
        self.assertNotIn("timeout", proc.stderr.lower())

    def test_script_does_not_gtk_launch(self) -> None:
        text = SMOKE_DESKTOP.read_text(encoding="utf-8")
        self.assertIn("NameHasOwner", text)
        self.assertIn("io.github.lgse.Strata", text)
        self.assertIn("gnome-screenshot", text)
        commands = [
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertFalse(any("gtk-launch" in line for line in commands))


class SmokeDesktopHyprlandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.fixture = self.tmp / "fixture"
        self.bindir = self.tmp / "bin"
        self.fixture.mkdir()
        self.bindir.mkdir()
        _write_exec(self.bindir / "grim", GRIM_FAKE)
        _write_exec(self.bindir / "hyprctl", HYPRCTL_FAKE)
        _write_exec(self.bindir / "jq", JQ_FAKE)
        self.shot = self.tmp / "window.png"
        (self.fixture / "hyprctl-clients.json").write_text(
            HYPR_CLIENTS_HIT, encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._td.cleanup()

    def _env(self, *, with_grim: bool = True) -> dict[str, str]:
        env = os.environ.copy()
        if with_grim:
            env["PATH"] = f"{self.bindir}{os.pathsep}{env.get('PATH', '')}"
        else:
            empty = self.tmp / "empty-bin"
            empty.mkdir(exist_ok=True)
            _write_exec(empty / "hyprctl", HYPRCTL_FAKE)
            _write_exec(empty / "jq", JQ_FAKE)
            env["PATH"] = str(empty)
        env["SMOKE_FIXTURE"] = str(self.fixture)
        env["SMOKE_SCREENSHOT_PATH"] = str(self.shot)
        env["SMOKE_ORACLE"] = "hyprland"
        return env

    def _run(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        assert bash is not None
        return subprocess.run(
            [bash, str(SMOKE_DESKTOP)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_class_hit_writes_png(self) -> None:
        proc = self._run(self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertTrue(self.shot.is_file())
        self.assertTrue(self.shot.read_bytes().startswith(PNG_MAGIC))

    def test_class_miss_fails_after_screenshot(self) -> None:
        (self.fixture / "hyprctl-clients.json").write_text(
            HYPR_CLIENTS_MISS, encoding="utf-8"
        )
        proc = self._run(self._env())
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(self.shot.is_file())
        self.assertIn("io.github.lgse.Strata", proc.stderr)

    def test_missing_grim_is_golden_bug(self) -> None:
        proc = self._run(self._env(with_grim=False))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(
            proc.stderr.strip(),
            "screenshot tool missing; rebuild the golden",
        )
        self.assertNotIn("timeout", proc.stderr.lower())
        self.assertFalse(self.shot.exists())

    def test_hyprland_oracle_does_not_use_gnome_bus(self) -> None:
        text = SMOKE_DESKTOP.read_text(encoding="utf-8")
        self.assertIn("hyprctl clients", text)
        self.assertIn("grim", text)
        proc = self._run(self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        blob = proc.stdout + proc.stderr
        self.assertNotIn("NameHasOwner", blob)
        self.assertNotIn("gnome-screenshot", blob)


class SmokeInstallScriptTests(unittest.TestCase):
    def test_script_is_executable_and_not_curl_pipe(self) -> None:
        self.assertTrue(SMOKE_INSTALL.is_file())
        self.assertTrue(os.access(SMOKE_INSTALL, os.X_OK))
        text = SMOKE_INSTALL.read_text(encoding="utf-8")
        self.assertIn("--non-interactive", text)
        self.assertIn("--with-desktop-entry", text)
        self.assertIn("--without-file-chooser", text)
        self.assertIn("INSTALL_SH_SHA256", text)
        self.assertIn("--archive", text)
        commands = [
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertFalse(any("|" in line and "bash" in line for line in commands))
        self.assertNotIn("pexpect", text)
        self.assertNotIn("expect", text.lower().replace("expected", ""))

    def _run_with_fake_curl(
        self, extra_args: list[str] | None = None, extra_env: dict[str, str] | None = None
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bindir = tmp / "bin"
        bindir.mkdir()
        record = tmp / "install-argv"
        home = tmp / "home"
        home.mkdir()
        dest = tmp / "downloaded-install.sh"

        curl = bindir / "curl"
        curl.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "dest=\"\"\n"
            "while [[ $# -gt 0 ]]; do\n"
            "  if [[ \"$1\" == \"-o\" ]]; then dest=\"$2\"; shift 2; continue; fi\n"
            "  shift\n"
            "done\n"
            "cat > \"$dest\" <<'INNER'\n"
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$@\" > \"${INSTALL_ARGV_RECORD:?}\"\n"
            "mkdir -p \"$HOME/.local/bin\"\n"
            "echo stub > \"$HOME/.local/bin/strata\"\n"
            "chmod +x \"$HOME/.local/bin/strata\"\n"
            "INNER\n"
            "chmod +x \"$dest\"\n",
            encoding="utf-8",
        )
        curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
        for name in ("sha256sum", "awk", "bash", "chmod", "mkdir", "cat", "echo", "test"):
            found = shutil.which(name)
            self.assertIsNotNone(found, name)
            assert found is not None
            os.symlink(found, bindir / name)

        env = os.environ.copy()
        env["PATH"] = str(bindir)
        env["HOME"] = str(home)
        env["INSTALL_SH_DEST"] = str(dest)
        env["INSTALL_ARGV_RECORD"] = str(record)
        if extra_env:
            env.update(extra_env)
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        assert bash is not None
        argv = [bash, str(SMOKE_INSTALL)]
        if extra_args:
            argv.extend(extra_args)
        proc = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        return proc, dest, record, home

    def test_save_then_exec_contract_flags(self) -> None:
        proc, dest, record, home = self._run_with_fake_curl()
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        self.assertEqual(parse_install_sh_sha256(proc.stdout), digest)
        argv = record.read_text(encoding="utf-8").split()
        self.assertEqual(
            argv,
            [
                "--non-interactive",
                "--with-desktop-entry",
                "--without-file-chooser",
            ],
        )
        self.assertTrue((home / ".local/bin/strata").is_file())
        self.assertTrue(os.access(home / ".local/bin/strata", os.X_OK))
        self.assertNotIn("| bash", proc.stdout)
        self.assertNotIn("curl |", proc.stdout)

    def test_archive_flag_is_forwarded(self) -> None:
        proc, dest, record, home = self._run_with_fake_curl(
            extra_args=[
                "--non-interactive",
                "--with-desktop-entry",
                "--without-file-chooser",
                "--archive",
                "/tmp/strata-archive.tar.gz",
            ]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        self.assertEqual(parse_install_sh_sha256(proc.stdout), digest)
        argv = record.read_text(encoding="utf-8").split()
        self.assertEqual(
            argv,
            [
                "--non-interactive",
                "--with-desktop-entry",
                "--without-file-chooser",
                "--archive",
                "/tmp/strata-archive.tar.gz",
            ],
        )
        self.assertTrue((home / ".local/bin/strata").is_file())


def _share_omarchy_major() -> str:
    """Major that PR #743 would read from the host version file, if any."""
    path = Path("/usr/share/omarchy/version")
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    match = re.search(r"(^|[^0-9.])([34])[.][0-9]+", text)
    return match.group(2) if match else ""


class SmokeOmarchyDetectScriptTests(unittest.TestCase):
    def test_scripts_are_executable(self) -> None:
        self.assertTrue(SMOKE_OMARCHY_DETECT.is_file())
        self.assertTrue(os.access(SMOKE_OMARCHY_DETECT, os.X_OK))
        self.assertTrue(SMOKE_OMARCHY_BINDINGS.is_file())
        self.assertTrue(os.access(SMOKE_OMARCHY_BINDINGS, os.X_OK))

    def _detect(
        self, case: str, install_sh: Path, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "INSTALL_SH": str(install_sh),
            "SMOKE_OMARCHY_CASE": case,
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(SMOKE_OMARCHY_DETECT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_token_cases_against_pr743_fixture(self) -> None:
        for token, expected in OMARCHY_TOKEN_CASES:
            with self.subTest(token=token):
                completed = self._detect(
                    "token", FIXTURE_PR743, {"SMOKE_OMARCHY_OUTPUT": token}
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(
                    parse_smoke_kv(completed.stdout, "DETECTED_MAJOR"), expected
                )

    def test_token_case_fails_on_main_fixture(self) -> None:
        completed = self._detect(
            "token", FIXTURE_MAIN, {"SMOKE_OMARCHY_OUTPUT": "4.0.0-1"}
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("omarchy_major_from missing", completed.stderr)

    def test_command_hash_pr743_falls_back_to_version_file(self) -> None:
        completed = self._detect(
            "command",
            FIXTURE_PR743,
            {"SMOKE_OMARCHY_OUTPUT": OMARCHY_DEV_HASH_OUTPUT},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        got = parse_smoke_kv(completed.stdout, "DETECTED_MAJOR")
        self.assertEqual(got, _share_omarchy_major())

    def test_command_hash_main_fixture_is_three(self) -> None:
        completed = self._detect(
            "command",
            FIXTURE_MAIN,
            {"SMOKE_OMARCHY_OUTPUT": OMARCHY_DEV_HASH_OUTPUT},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(parse_smoke_kv(completed.stdout, "DETECTED_MAJOR"), "3")


class SmokeOmarchyBindingsScriptTests(unittest.TestCase):
    def _run(
        self, major: str, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "SMOKE_OMARCHY_MAJOR": major}
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(SMOKE_OMARCHY_BINDINGS)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_omarchy_3_write_uses_bindings_conf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".config" / "hypr").mkdir(parents=True)
            completed = self._run(
                "3",
                {
                    "HOME": str(home),
                    "INSTALL_SH": str(FIXTURE_PR743),
                    "SMOKE_WRITE_BINDINGS": "1",
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(parse_smoke_kv(completed.stdout, "BINDINGS_KIND"), "conf")
            self.assertTrue((home / ".config" / "hypr" / "bindings.conf").is_file())
            self.assertFalse((home / ".config" / "hypr" / "bindings.lua").exists())
            conf = (home / ".config" / "hypr" / "bindings.conf").read_text(
                encoding="utf-8"
            )
            self.assertIn("strata-installer: file-manager start", conf)

    def test_omarchy_4_write_uses_bindings_lua_not_conf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".config" / "hypr").mkdir(parents=True)
            completed = self._run(
                "4",
                {
                    "HOME": str(home),
                    "INSTALL_SH": str(FIXTURE_PR743),
                    "SMOKE_WRITE_BINDINGS": "1",
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(parse_smoke_kv(completed.stdout, "BINDINGS_KIND"), "lua")
            self.assertTrue((home / ".config" / "hypr" / "bindings.lua").is_file())
            self.assertFalse((home / ".config" / "hypr" / "bindings.conf").exists())
            lua = (home / ".config" / "hypr" / "bindings.lua").read_text(encoding="utf-8")
            self.assertIn("strata-installer: file-manager start", lua)


def _write_previous_archive(path: Path, version: str) -> None:
    """Minimal GitHub-style Strata release tarball for smoke-update.sh."""
    inner = f"strata-{version}-x86_64-unknown-linux-gnu"
    with tarfile.open(path, "w:gz") as tar:
        payload = f"stub-binary-{version}\n".encode()
        info = tarfile.TarInfo(name=f"{inner}/strata")
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, fileobj=io.BytesIO(payload))
        desktop = (
            "[Desktop Entry]\n"
            "Name=Strata\n"
            "Exec=strata %U\n"
            "Type=Application\n"
        ).encode()
        dinfo = tarfile.TarInfo(name=f"{inner}/io.github.lgse.Strata.desktop")
        dinfo.size = len(desktop)
        tar.addfile(dinfo, fileobj=io.BytesIO(desktop))


class SmokeUpdateScriptTests(unittest.TestCase):
    def test_script_is_executable(self) -> None:
        self.assertTrue(SMOKE_UPDATE.is_file())
        self.assertTrue(os.access(SMOKE_UPDATE, os.X_OK))

    def _bindir(self, tmp: Path) -> Path:
        bindir = tmp / "bin"
        bindir.mkdir()
        for name in (
            "sha256sum",
            "awk",
            "bash",
            "chmod",
            "mkdir",
            "cat",
            "echo",
            "test",
            "tar",
            "install",
            "cp",
            "mv",
            "sed",
            "uname",
            "mktemp",
            "rm",
            "dirname",
            "gzip",
            "gunzip",
        ):
            found = shutil.which(name)
            self.assertIsNotNone(found, name)
            assert found is not None
            os.symlink(found, bindir / name)
        return bindir

    def test_previous_phase_installs_each_configured_version_from_archive(self) -> None:
        for version in DEFAULT_UPDATE_FROM_VERSIONS:
            with self.subTest(version=version):
                tmp = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
                bindir = self._bindir(tmp)
                home = tmp / "home"
                home.mkdir()
                archive = tmp / previous_release_archive_name(version)
                _write_previous_archive(archive, version)
                env = os.environ.copy()
                env["PATH"] = str(bindir)
                env["HOME"] = str(home)
                env.pop("XDG_DATA_HOME", None)
                env["SMOKE_UPDATE_PHASE"] = "previous"
                env["UPDATE_FROM_VERSION"] = version
                env["UPDATE_FROM_ARCHIVE"] = str(archive)
                proc = subprocess.run(
                    ["bash", str(SMOKE_UPDATE)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertEqual(
                    parse_smoke_kv(proc.stdout, "FROM_VERSION"), version
                )
                installed = home / ".local" / "bin" / "strata"
                self.assertTrue(installed.is_file())
                self.assertTrue(os.access(installed, os.X_OK))
                self.assertEqual(
                    installed.read_text(encoding="utf-8"),
                    f"stub-binary-{version}\n",
                )
                desktop = (
                    home
                    / ".local"
                    / "share"
                    / "applications"
                    / "io.github.lgse.Strata.desktop"
                )
                self.assertTrue(desktop.is_file())
                self.assertIn(str(installed), desktop.read_text(encoding="utf-8"))

    def test_latest_phase_moves_previous_binary_and_runs_install_sh(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bindir = self._bindir(tmp)
        home = tmp / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        previous = home / ".local" / "bin" / "strata"
        previous.write_text("old-binary\n", encoding="utf-8")
        previous.chmod(0o755)
        dest = tmp / "downloaded-install.sh"
        record = tmp / "install-argv"
        curl = bindir / "curl"
        curl.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "dest=\"\"\n"
            "while [[ $# -gt 0 ]]; do\n"
            "  if [[ \"$1\" == \"-o\" ]]; then dest=\"$2\"; shift 2; continue; fi\n"
            "  shift\n"
            "done\n"
            "cat > \"$dest\" <<'INNER'\n"
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$@\" > \"${INSTALL_ARGV_RECORD:?}\"\n"
            "mkdir -p \"$HOME/.local/bin\"\n"
            "echo new-binary > \"$HOME/.local/bin/strata\"\n"
            "chmod +x \"$HOME/.local/bin/strata\"\n"
            "INNER\n"
            "chmod +x \"$dest\"\n",
            encoding="utf-8",
        )
        curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        env["PATH"] = str(bindir)
        env["HOME"] = str(home)
        env.pop("XDG_DATA_HOME", None)
        env["SMOKE_UPDATE_PHASE"] = "latest"
        env["INSTALL_SH_DEST"] = str(dest)
        env["INSTALL_ARGV_RECORD"] = str(record)
        proc = subprocess.run(
            ["bash", str(SMOKE_UPDATE)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        self.assertEqual(parse_install_sh_sha256(proc.stdout), digest)
        self.assertEqual(
            record.read_text(encoding="utf-8").split(),
            [
                "--non-interactive",
                "--with-desktop-entry",
                "--without-file-chooser",
            ],
        )
        self.assertEqual(
            previous.read_text(encoding="utf-8"), "new-binary\n"
        )
        self.assertEqual(
            (home / ".local" / "bin" / "strata.previous").read_text(
                encoding="utf-8"
            ),
            "old-binary\n",
        )

    def test_previous_phase_requires_version(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bindir = self._bindir(tmp)
        env = os.environ.copy()
        env["PATH"] = str(bindir)
        env["HOME"] = str(tmp / "home")
        env["SMOKE_UPDATE_PHASE"] = "previous"
        proc = subprocess.run(
            ["bash", str(SMOKE_UPDATE)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("UPDATE_FROM_VERSION", proc.stderr)

    def test_does_not_pipe_curl_into_bash(self) -> None:
        body = SMOKE_UPDATE.read_text(encoding="utf-8")
        self.assertNotIn("| bash", body)
        self.assertNotIn("curl |", body)
        self.assertIn("sha256sum --check", body)


class SmokeAboutScriptTests(unittest.TestCase):
    def test_script_is_executable(self) -> None:
        self.assertTrue(SMOKE_ABOUT.is_file())
        self.assertTrue(os.access(SMOKE_ABOUT, os.X_OK))

    def test_wtype_opens_settings_and_types_version(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bindir = tmp / "bin"
        bindir.mkdir()
        record = tmp / "wtype-argv"
        wtype = bindir / "wtype"
        wtype.write_text(
            "#!/bin/bash\n"
            "printf '%s\\n' \"$*\" >> \"${WTYPE_RECORD:?}\"\n",
            encoding="utf-8",
        )
        wtype.chmod(wtype.stat().st_mode | stat.S_IXUSR)
        sleep = shutil.which("sleep")
        self.assertIsNotNone(sleep)
        assert sleep is not None
        os.symlink(sleep, bindir / "sleep")
        env = os.environ.copy()
        env["PATH"] = str(bindir)
        env["WTYPE_RECORD"] = str(record)
        env["SMOKE_COMPOSITOR"] = "gnome-shell"
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        assert bash is not None
        proc = subprocess.run(
            [bash, str(SMOKE_ABOUT)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(parse_smoke_kv(proc.stdout, "INPUT"), "wtype")
        lines = record.read_text(encoding="utf-8").splitlines()
        self.assertTrue(any("comma" in line for line in lines))
        self.assertGreaterEqual(sum(1 for line in lines if "Tab" in line), 5)
        self.assertTrue(any("space" in line.lower() for line in lines))
        self.assertFalse(any(line.strip() == "version" for line in lines))

    def test_hyprland_without_wtype_reports_hyprctl_open(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bindir = tmp / "bin"
        bindir.mkdir()
        record = tmp / "hyprctl-argv"
        hyprctl = bindir / "hyprctl"
        hyprctl.write_text(
            "#!/bin/bash\n"
            "printf '%s\\n' \"$*\" >> \"${HYPRCTL_RECORD:?}\"\n",
            encoding="utf-8",
        )
        hyprctl.chmod(hyprctl.stat().st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        env["PATH"] = str(bindir)
        env["HYPRCTL_RECORD"] = str(record)
        env["SMOKE_COMPOSITOR"] = "Hyprland"
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        assert bash is not None
        proc = subprocess.run(
            [bash, str(SMOKE_ABOUT)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(parse_smoke_kv(proc.stdout, "INPUT"), "hyprctl-open")
        blob = record.read_text(encoding="utf-8")
        self.assertIn("sendshortcut", blob)
        self.assertIn("CTRL, comma", blob)
        self.assertIn("io.github.lgse.Strata", blob)

    def test_missing_input_tool_exits_two(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bindir = tmp / "bin"
        bindir.mkdir()
        env = os.environ.copy()
        env["PATH"] = str(bindir)
        env["SMOKE_COMPOSITOR"] = "gnome-shell"
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        assert bash is not None
        proc = subprocess.run(
            [bash, str(SMOKE_ABOUT)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(parse_smoke_kv(proc.stdout, "INPUT"), "missing")


if __name__ == "__main__":
    unittest.main()


SUDO_FAKE = """#!/usr/bin/env bash
[[ ${1:-} == -n ]] && shift
exec "$@"
"""

APT_GET_FAKE = """#!/usr/bin/env bash
printf 'DEBIAN_FRONTEND=%s %s\\n' "${DEBIAN_FRONTEND:-}" "$*" >>"$FAKE_LOG"
mkdir -p "$(dirname "$APPARMOR_EXTRA_PROFILE")"
printf 'profile bwrap /usr/bin/bwrap {}\\n' >"$APPARMOR_EXTRA_PROFILE"
"""

APPARMOR_PARSER_FAKE = """#!/usr/bin/env bash
printf 'apparmor_parser %s\\n' "$*" >>"$FAKE_LOG"
"""

# Drops the fixed sandbox options the smoke passes, then plays the command.
BWRAP_FAKE = """#!/usr/bin/env bash
shift 8
case "$1" in
  true) exit "${FAKE_PLAIN_RC:-0}" ;;
  cat) printf '%s\\0' "$FAKE_LABEL" ;;
  unshare)
    if [[ ${FAKE_NESTED:-eperm} == eperm ]]; then
      echo 'unshare: unshare failed: Operation not permitted' >&2
      exit 1
    fi
    ;;
esac
"""


class SmokeAppArmorBwrapScriptTests(unittest.TestCase):
    def _run(
        self,
        tmp: Path,
        *,
        profile_present: bool = True,
        profiles: str = (
            "bwrap (enforce)\nunpriv_bwrap (enforce)\nfirefox (unconfined)\n"
        ),
        extra_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        bin_dir = tmp / "bin"
        for name, text in (
            ("sudo", SUDO_FAKE),
            ("apt-get", APT_GET_FAKE),
            ("apparmor_parser", APPARMOR_PARSER_FAKE),
            ("bwrap", BWRAP_FAKE),
        ):
            _write_exec(bin_dir / name, text)
        src = tmp / "extra-profiles" / "bwrap-userns-restrict"
        if profile_present:
            src.parent.mkdir(parents=True)
            src.write_text("profile bwrap /usr/bin/bwrap {}\n", encoding="utf-8")
        (tmp / "sysctl").write_text("1\n", encoding="utf-8")
        (tmp / "profiles").write_text(profiles, encoding="utf-8")
        (tmp / "apparmor.d").mkdir()
        log = tmp / "fake.log"
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_LOG": str(log),
            "FAKE_LABEL": "bwrap//&unpriv_bwrap (enforce)",
            "APPARMOR_EXTRA_PROFILE": str(src),
            "APPARMOR_PROFILE_DEST": str(tmp / "apparmor.d" / "bwrap-userns-restrict"),
            "APPARMOR_SYSCTL": str(tmp / "sysctl"),
            "APPARMOR_PROFILES_LIST": str(tmp / "profiles"),
        }
        env.pop("DEBIAN_FRONTEND", None)
        if extra_env:
            env.update(extra_env)
        completed = subprocess.run(
            ["bash", str(SMOKE_APPARMOR_BWRAP)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed, log

    def test_enforced_profile_reports_denied_nested_userns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            completed, log = self._run(tmp)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((tmp / "apparmor.d" / "bwrap-userns-restrict").is_file())
            self.assertIn("apparmor_parser -r", log.read_text(encoding="utf-8"))
        out = completed.stdout
        self.assertEqual(parse_smoke_kv(out, "RESTRICT_UNPRIVILEGED_USERNS"), "1")
        self.assertEqual(parse_smoke_kv(out, "PROFILES"), "bwrap,unpriv_bwrap")
        self.assertEqual(parse_smoke_kv(out, "PLAIN_BWRAP_RC"), "0")
        self.assertEqual(
            parse_smoke_kv(out, "CHILD_LABEL"), "bwrap//&unpriv_bwrap (enforce)"
        )
        self.assertEqual(parse_smoke_kv(out, "NESTED_UNSHARE_RC"), "1")
        self.assertEqual(
            parse_smoke_kv(out, "NESTED_UNSHARE_ERR"),
            "unshare: unshare failed: Operation not permitted",
        )

    def test_missing_profile_installs_apparmor_profiles_noninteractively(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            completed, log = self._run(Path(td), profile_present=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(
                "DEBIAN_FRONTEND=noninteractive install -y -q apparmor-profiles",
                log.read_text(encoding="utf-8"),
            )

    def test_nested_userns_allowed_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            completed, _log = self._run(
                Path(td), extra_env={"FAKE_NESTED": "allowed"}
            )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(parse_smoke_kv(completed.stdout, "NESTED_UNSHARE_RC"), "0")
        self.assertIn("nested unshare -Urm inside bwrap succeeded", completed.stderr)

    def test_unconfined_bwrap_child_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            completed, _log = self._run(
                Path(td), extra_env={"FAKE_LABEL": "unconfined"}
            )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("not confined by unpriv_bwrap", completed.stderr)

    def test_profile_not_enforcing_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            completed, _log = self._run(
                Path(td), profiles="bwrap (complain)\nunpriv_bwrap (complain)\n"
            )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("not both enforcing", completed.stderr)

    def test_stock_ubuntu_plain_sandbox_failure_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            completed, _log = self._run(Path(td), extra_env={"FAKE_PLAIN_RC": "1"})
        self.assertEqual(completed.returncode, 1)
        self.assertIn("plain bwrap sandbox failed", completed.stderr)
