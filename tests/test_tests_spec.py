"""Host tests for tests_spec session / GNOME oracle functions. No QEMU VM."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from strataqemu.guest import load_guest
from strataqemu.qemu import Machine
from strataqemu import config
from strataqemu.tests_spec import (
    DEFAULT_UPDATE_FROM_VERSIONS,
    GDBUS_NAME_HAS_OWNER_ARGV,
    GUEST_ARCHIVE_REMOTE,
    INSTALL_FROM_RELEASE_STEPS,
    INSTALL_SH_FLAGS,
    OMARCHY_BINDINGS_FAIL_CLOSED,
    OMARCHY_BINDINGS_STEPS,
    OMARCHY_DEV_HASH_OUTPUT,
    OMARCHY_TOKEN_CASES,
    SCREENSHOT_TOOL_MISSING,
    SESSION_ONLY_FORBIDDEN,
    STRATA_BUS_NAME,
    STRATA_VERSION_COMMAND,
    SessionSmokeError,
    UPDATE_FROM_EMPTY_LIST,
    UPDATE_FROM_MISSING_VERSION,
    UPDATE_FROM_SAME_AS_LATEST,
    UPDATE_FROM_STEPS,
    ABOUT_AFTER_PNG_NAME,
    ABOUT_BEFORE_PNG_NAME,
    ABOUT_CLI_ALREADY_RECORDED,
    ABOUT_SIDEBAR_TABS,
    ABOUT_VERSION_PNG_NAME,
    assert_session_only_commands,
    capture_guest_screenshot,
    command_is_forbidden_for_session_only,
    compositor_process_name,
    extra_qmp_screendump,
    gdbus_name_has_owner_command,
    github_latest_version,
    hyprctl_class_oracle_command,
    install_arch_script,
    install_sh_argv,
    install_smoke_command,
    missing_golden_message,
    omarchy_bindings_command,
    omarchy_detect_command,
    omarchy_install_major,
    smoke_udiskie_unlock_script,
    supports_udiskie_unlock,
    udiskie_unlock_command,
    udiskie_unlock_install_sh_fixture,
    parse_archive_version,
    parse_install_sh_sha256,
    parse_name_has_owner,
    parse_observed_version,
    parse_session_exports,
    parse_smoke_kv,
    parse_update_from_version,
    parse_update_from_versions,
    previous_release_archive_name,
    previous_release_url,
    run_install_from_release_steps,
    UDISKIE_UNLOCK_FAIL_CLOSED,
    UDISKIE_UNLOCK_STEPS,
    run_omarchy_bindings_steps,
    run_session_only_steps,
    run_udiskie_unlock_steps,
    run_update_from_steps,
    screenshot_tool_for_compositor,
    sha256_file,
    smoke_desktop_script,
    smoke_install_script,
    smoke_omarchy_bindings_script,
    smoke_omarchy_detect_script,
    smoke_session_script,
    smoke_about_script,
    smoke_update_script,
    about_smoke_command,
    qmp_about_nav_chords,
    qmp_open_about_chords,
    run_about_version_step,
    strata_version_is_cli,
    supports_install_from_release,
    supports_update_from,
    update_from_versions,
    update_smoke_command,
    versions_match,
    wait_gnome_bus_name,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class _FakeRun:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.remote: list[str] = []
        self.which_screenshot = 0
        self.which_grim = 0
        self.session_stdout = (
            "SESSION_ID=c1\n"
            "XDG_RUNTIME_DIR=/run/user/1000\n"
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\n"
            "WAYLAND_DISPLAY=wayland-0\n"
        )
        self.session_code = 0
        self.gdbus_stdout = "(true,)\n"
        self.write_png_on_scp = True
        self.install_digest = hashlib.sha256(b"fixture-install-sh").hexdigest()
        self.hyprctl_code = 0
        self.exec_line = "Exec=/home/tester/.local/bin/strata %U\n"
        self.version_stdout = "0.9.0\n"
        self._version_i = 0
        self.from_version = "0.15.0"
        self.detected_major = "4"
        self.about_code = 0
        self.about_stdout = "INPUT=wtype\n"

    def _version_reply(self) -> str:
        value = self.version_stdout
        if isinstance(value, (list, tuple)):
            if not value:
                return "0.9.0\n"
            idx = min(self._version_i, len(value) - 1)
            self._version_i += 1
            return value[idx]
        return value

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        name = Path(str(argv[0])).name if argv else ""
        if name.startswith("qemu-system"):
            raise AssertionError(f"spawned qemu-system: {argv}")
        if name == "scp":
            dest = Path(str(argv[-1]))
            if self.write_png_on_scp and dest.suffix == ".png":
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(PNG)
            return _completed(0)
        remote = str(argv[-1]) if argv else ""
        self.remote.append(remote)
        if "command -v grim" in remote:
            return _completed(
                self.which_grim,
                stdout="" if self.which_grim else "/usr/bin/grim\n",
            )
        if "command -v gnome-screenshot" in remote:
            return _completed(
                self.which_screenshot,
                stdout="" if self.which_screenshot else "/usr/bin/gnome-screenshot\n",
            )
        if "smoke-session.sh" in remote:
            return _completed(self.session_code, stdout=self.session_stdout)
        if "smoke-omarchy-detect.sh" in remote:
            major = self.detected_major
            if "SMOKE_OMARCHY_CASE=token" in remote:
                if "4.0.0" in remote or "1:4.0.0-1" in remote:
                    major = "4"
                elif "3.8.5" in remote:
                    major = "3"
                else:
                    major = ""
            return _completed(
                0,
                stdout=f"SMOKE_OMARCHY_CASE=live\nDETECTED_MAJOR={major}\n",
            )
        if "smoke-omarchy-bindings.sh" in remote:
            kind = "lua" if self.detected_major == "4" else "conf"
            path = f"/home/tester/.config/hypr/bindings.{kind}"
            return _completed(
                0,
                stdout=f"BINDINGS_KIND={kind}\nBINDINGS_PATH={path}\n",
            )
        if "smoke-luks-hotplug.sh" in remote:
            if "SMOKE_LUKS_CASE=wrap" in remote:
                return _completed(
                    0,
                    stdout="WRAPPED=1\nHOOK_LOG=/tmp/strata-luks-hook.log\n",
                )
            return _completed(
                0,
                stdout=(
                    "STRATA_CALLED=1\n"
                    "LUKS_DEV=vdc\n"
                    "LUKS_SERIAL=strata-luks\n"
                    "HOOK_LINE=--udiskie-hook device_added crypto /dev/vdc uuid\n"
                ),
            )
        if "udiskie/config.yml" in remote:
            return _completed(0, stdout="HOOK_CONFIG=1\n")
        if "pacman -S --needed --noconfirm udiskie" in remote and "gtk4" not in remote:
            return _completed(0, stdout="UDISKIE=ok\n")
        if "printf 'UDISKIE=" in remote:
            return _completed(0, stdout="UDISKIE=ok\n")
        if "smoke-udiskie-unlock.sh" in remote:
            if "SMOKE_UDISKIE_CASE=parse-args" in remote:
                with_flag = (
                    "yes" if "--with-udiskie-unlock" in remote else "ask"
                )
                return _completed(
                    0,
                    stdout=(
                        "NON_INTERACTIVE=yes\n"
                        f"WITH_UDISKIE_UNLOCK={with_flag}\n"
                        "BIN_CALLS=0\n"
                    ),
                )
            calls = "0"
            on_path = "1" if "SMOKE_UDISKIE_STUB=1" in remote else "0"
            if (
                "SMOKE_PROMPT=yes" in remote
                and "SMOKE_UDISKIE_STUB=1" in remote
                and "SMOKE_MARKER=1" in remote
            ):
                calls = "1"
            stdout = (
                f"BIN_CALLS={calls}\n"
                f"UDISKIE_ON_PATH={on_path}\n"
                "RELEASE_SUPPORTS=1\n"
                "UDISKIE_RAN=0\n"
                "DECOY_CALLS=0\n"
            )
            if calls == "1":
                stdout += "BIN_ARGV=--install-udiskie-unlock\n"
            return _completed(0, stdout=stdout)
        if "gnome-screenshot" in remote:
            return _completed(0)
        if " grim " in f" {remote} " or remote.strip().startswith("grim "):
            return _completed(0)
        if "smoke-about.sh" in remote:
            return _completed(self.about_code, stdout=self.about_stdout)
        if "smoke-update.sh" in remote:
            if "SMOKE_UPDATE_PHASE=previous" in remote:
                return _completed(
                    0, stdout=f"FROM_VERSION={self.from_version}\n"
                )
            return _completed(
                0,
                stdout=(
                    f"INSTALL_SH_SHA256={self.install_digest}\n"
                    "Installed Strata v9.9.9\n"
                ),
            )
        if "smoke-install.sh" in remote or "install-arch.sh" in remote:
            return _completed(
                0,
                stdout=(
                    f"INSTALL_SH_SHA256={self.install_digest}\n"
                    "Installed Strata v9.9.9\n"
                ),
            )
        if "Exec=" in remote or STRATA_BUS_NAME + ".desktop" in remote:
            return _completed(0, stdout=self.exec_line)
        if "test -x" in remote:
            return _completed(0)
        if "--version" in remote and "strata" in remote:
            return _completed(0, stdout=self._version_reply())
        if "gtk-launch" in remote or "gio launch" in remote:
            return _completed(0)
        if "hyprctl clients" in remote:
            return _completed(self.hyprctl_code, stdout='{"class":"ok"}\n')
        if "NameHasOwner" in remote:
            return _completed(0, stdout=self.gdbus_stdout)
        return _completed(0)


class ParseOracleTests(unittest.TestCase):
    def test_name_has_owner_true_and_false(self) -> None:
        self.assertTrue(parse_name_has_owner("(true,)\n"))
        self.assertFalse(parse_name_has_owner("(false,)\n"))
        with self.assertRaises(SessionSmokeError):
            parse_name_has_owner("garbage")

    def test_screenshot_tool_missing_maps_to_golden_bug(self) -> None:
        self.assertIn("rebuild the golden", SCREENSHOT_TOOL_MISSING)

    def test_session_exports(self) -> None:
        env = parse_session_exports(
            "SESSION_ID=c1\nWAYLAND_DISPLAY=wayland-0\n# comment\n"
        )
        self.assertEqual(env["SESSION_ID"], "c1")
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")

    def test_ubuntu_compositor_is_gnome_shell(self) -> None:
        guest = load_guest("ubuntu-2404")
        self.assertEqual(compositor_process_name(guest), "gnome-shell")
        self.assertEqual(screenshot_tool_for_compositor("gnome-shell"), "gnome-screenshot")

    def test_fedora_compositor_is_gnome_shell(self) -> None:
        guest = load_guest("fedora-workstation")
        self.assertEqual(compositor_process_name(guest), "gnome-shell")
        self.assertTrue(supports_install_from_release(guest))

    def test_arch_compositor_is_hyprland_grim(self) -> None:
        guest = load_guest("arch")
        self.assertEqual(compositor_process_name(guest), "Hyprland")
        self.assertEqual(screenshot_tool_for_compositor("Hyprland"), "grim")
        self.assertTrue(supports_install_from_release(guest))
        self.assertIn("hyprctl clients", hyprctl_class_oracle_command())
        self.assertIn(STRATA_BUS_NAME, hyprctl_class_oracle_command())

    def test_omarchy4_compositor_is_hyprland_grim(self) -> None:
        guest = load_guest("omarchy-4")
        self.assertEqual(compositor_process_name(guest), "Hyprland")
        self.assertTrue(supports_install_from_release(guest))

    def test_omarchy3_compositor_is_hyprland_grim(self) -> None:
        guest = load_guest("omarchy-3")
        self.assertEqual(compositor_process_name(guest), "Hyprland")
        self.assertTrue(supports_install_from_release(guest))

    def test_missing_golden_message_matches_design(self) -> None:
        msg = missing_golden_message("ubuntu-2404")
        self.assertEqual(
            msg, "run `mise run image-build -- ubuntu-2404` first"
        )
        self.assertEqual(
            missing_golden_message("fedora-workstation"),
            "run `mise run image-build -- fedora-workstation` first",
        )

    def test_session_only_forbids_install_and_window_oracles(self) -> None:
        self.assertTrue(command_is_forbidden_for_session_only("gtk-launch io.github.lgse.Strata"))
        self.assertTrue(command_is_forbidden_for_session_only("bash install.sh"))
        self.assertTrue(
            command_is_forbidden_for_session_only("strata --version")
        )
        self.assertTrue(
            command_is_forbidden_for_session_only(
                "gdbus call --session --method org.freedesktop.DBus.NameHasOwner "
                "io.github.lgse.Strata"
            )
        )
        self.assertFalse(
            command_is_forbidden_for_session_only(
                "SMOKE_COMPOSITOR=gnome-shell bash /tmp/smoke-session.sh"
            )
        )


class SessionOnlyDriveTests(unittest.TestCase):
    def test_session_only_does_not_wait_on_bus_or_launch(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            dest = tmp / "screenshot.png"
            commands: list[str] = []
            steps = run_session_only_steps(
                machine,
                compositor="gnome-shell",
                screenshot_dest=dest,
                qmp_dest=tmp / "qmp.png",
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
            )
            names = [s["name"] for s in steps]
            self.assertEqual(names, ["session", "screenshot"])
            blob = "\n".join(commands)
            self.assertIn("smoke-session.sh", blob)
            self.assertIn("gnome-screenshot", blob)
            self.assertNotIn("gtk-launch", blob)
            self.assertNotIn("NameHasOwner", blob)
            self.assertNotIn(STRATA_BUS_NAME, blob)
            self.assertNotIn("install.sh", blob)
            self.assertNotIn("strata --version", blob)
            assert_session_only_commands(commands)
            self.assertTrue(dest.is_file())
            self.assertTrue(dest.read_bytes().startswith(b"\x89PNG"))

    def test_gnome_screenshot_timeout_falls_back_to_vnc(self) -> None:
        fake = _FakeRun()

        def timeout_run(argv, **kwargs):
            remote = str(argv[-1]) if argv else ""
            if "gnome-screenshot" in remote and "command -v" not in remote:
                raise subprocess.TimeoutExpired(argv, 15)
            return fake(argv, **kwargs)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                vnc_port=5901,
                identity=identity,
            )
            dest = tmp / "shot.png"

            def vnc_ok(path, *, display):
                self.assertEqual(display, 5901)
                dest_path = Path(path)
                dest_path.write_bytes(PNG)
                return True

            capture_guest_screenshot(
                machine,
                {"WAYLAND_DISPLAY": "wayland-0"},
                dest,
                run=timeout_run,
                vnc_capture_fn=vnc_ok,
            )
            self.assertTrue(dest.is_file())
            self.assertTrue(dest.read_bytes().startswith(b"\x89PNG"))

    def test_missing_screenshot_tool_golden_bug_string(self) -> None:
        fake = _FakeRun()
        fake.which_screenshot = 1
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            with self.assertRaises(SessionSmokeError) as ctx:
                capture_guest_screenshot(
                    machine,
                    {"WAYLAND_DISPLAY": "wayland-0"},
                    tmp / "shot.png",
                    run=fake,
                )
        self.assertEqual(str(ctx.exception), SCREENSHOT_TOOL_MISSING)

    def test_wait_gnome_bus_is_separate_from_session_only(self) -> None:
        fake = _FakeRun()
        fake.gdbus_stdout = "(true,)\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            wait_gnome_bus_name(
                machine,
                timeout=2,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
            )
        self.assertTrue(any("NameHasOwner" in c for c in commands))
        self.assertIn(STRATA_BUS_NAME, gdbus_name_has_owner_command())
        self.assertTrue(
            any("NameHasOwner" in part for part in GDBUS_NAME_HAS_OWNER_ARGV)
        )

    def test_qmp_no_surface_does_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            machine = Machine(tmp / "o.qcow2", tmp, ssh_port=1)
            with patch(
                "strataqemu.tests_spec.qmp_screendump", return_value=False
            ) as dump:
                ok = extra_qmp_screendump(machine, tmp / "q.png")
            self.assertFalse(ok)
            dump.assert_called()

    def test_scripts_exist(self) -> None:
        self.assertTrue(smoke_session_script().is_file())
        self.assertTrue(smoke_desktop_script().is_file())
        self.assertTrue(smoke_install_script().is_file())
        self.assertTrue(os.access(smoke_install_script(), os.X_OK))
        self.assertTrue(smoke_update_script().is_file())
        self.assertTrue(os.access(smoke_update_script(), os.X_OK))
        self.assertTrue(smoke_about_script().is_file())
        self.assertTrue(os.access(smoke_about_script(), os.X_OK))
        desktop = smoke_desktop_script().read_text(encoding="utf-8")
        self.assertIn("NameHasOwner", desktop)
        self.assertIn("grim", desktop)
        self.assertIn("hyprctl clients", desktop)
        body = [
            line
            for line in desktop.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertFalse(any("gtk-launch" in line for line in body))

    def test_session_only_arch_uses_grim_not_install(self) -> None:
        fake = _FakeRun()
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            dest = tmp / "screenshot.png"
            commands: list[str] = []
            steps = run_session_only_steps(
                machine,
                compositor="Hyprland",
                screenshot_dest=dest,
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
            )
            names = [s["name"] for s in steps]
            self.assertEqual(names, ["session", "screenshot"])
            blob = "\n".join(commands)
            self.assertIn("grim", blob)
            self.assertNotIn("gnome-screenshot", blob)
            self.assertNotIn("install.sh", blob)
            self.assertNotIn("strata --version", blob)
            self.assertNotIn("gtk-launch", blob)
            self.assertNotIn("NameHasOwner", blob)
            assert_session_only_commands(commands)

    def test_missing_grim_golden_bug_string(self) -> None:
        fake = _FakeRun()
        fake.which_grim = 1
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            with self.assertRaises(SessionSmokeError) as ctx:
                capture_guest_screenshot(
                    machine,
                    {"WAYLAND_DISPLAY": "wayland-0"},
                    tmp / "shot.png",
                    tool="grim",
                    run=fake,
                )
        self.assertEqual(str(ctx.exception), SCREENSHOT_TOOL_MISSING)

    def test_install_from_release_steps_include_version_hyprland(self) -> None:
        fake = _FakeRun()
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            dest = tmp / "screenshot.png"
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("arch"),
                screenshot_dest=dest,
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                intended_version="0.9.0",
            )
        names = [s["name"] for s in steps]
        self.assertEqual(list(names), list(INSTALL_FROM_RELEASE_STEPS))
        self.assertEqual(extras["install_sh_sha256"], fake.install_digest)
        self.assertEqual(extras["intended_version"], "0.9.0")
        self.assertEqual(extras["observed_version"], "0.9.0")
        self.assertNotEqual(extras["observed_version"], "9.9.9")
        self.assertEqual(extras["install_method"], "install.sh")
        blob = "\n".join(commands)
        self.assertIn("smoke-install.sh", blob)
        self.assertIn("--non-interactive", blob)
        self.assertIn("--with-desktop-entry", blob)
        self.assertIn("--without-file-chooser", blob)
        self.assertIn("gtk-launch", blob)
        self.assertIn("hyprctl clients", blob)
        self.assertIn("grim", blob)
        self.assertIn(STRATA_VERSION_COMMAND, blob)
        self.assertNotIn("NameHasOwner", blob)
        self.assertNotIn("--archive", blob)
        helper = smoke_install_script().read_text(encoding="utf-8")
        self.assertIn("--non-interactive", helper)
        self.assertIn("--with-desktop-entry", helper)
        self.assertIn("--without-file-chooser", helper)
        self.assertIn(fake.install_digest, extras["install_sh_sha256"])

    def test_install_from_missing_grim_before_window_wait(self) -> None:
        fake = _FakeRun()
        fake.which_grim = 1
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            with self.assertRaises(SessionSmokeError) as ctx:
                run_install_from_release_steps(
                    machine,
                    guest=load_guest("arch"),
                    screenshot_dest=tmp / "shot.png",
                    session_timeout=5,
                    run=fake,
                    commands=commands,
                    sleep=lambda _s: None,
                    intended_version="0.9.0",
                )
        self.assertEqual(str(ctx.exception), SCREENSHOT_TOOL_MISSING)
        blob = "\n".join(commands)
        self.assertNotIn("gtk-launch", blob)
        self.assertNotIn("hyprctl clients", blob)


    def test_ubuntu_install_from_release_uses_gnome_bus_name(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            dest = tmp / "screenshot.png"
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("ubuntu-2404"),
                screenshot_dest=dest,
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                intended_version="0.9.0",
            )
        names = [s["name"] for s in steps]
        self.assertEqual(list(names), list(INSTALL_FROM_RELEASE_STEPS))
        blob = "\n".join(commands)
        self.assertIn("smoke-install.sh", blob)
        self.assertIn("--non-interactive", blob)
        self.assertIn("--with-desktop-entry", blob)
        self.assertIn("--without-file-chooser", blob)
        self.assertIn(STRATA_VERSION_COMMAND, blob)
        self.assertIn("NameHasOwner", blob)
        self.assertIn(STRATA_BUS_NAME, blob)
        self.assertIn("gtk-launch", blob)
        self.assertIn("gnome-screenshot", blob)
        self.assertNotIn("hyprctl clients", blob)
        self.assertNotIn("--archive", blob)
        self.assertEqual(extras["observed_version"], "0.9.0")
        self.assertEqual(extras["intended_version"], "0.9.0")
        self.assertNotIn("archive_sha256", extras)

    def test_fedora_install_from_release_uses_gnome_bus_name(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("fedora-workstation"),
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                intended_version="v0.9.0",
            )
        names = [s["name"] for s in steps]
        self.assertEqual(list(names), list(INSTALL_FROM_RELEASE_STEPS))
        blob = "\n".join(commands)
        self.assertIn("NameHasOwner", blob)
        self.assertIn("gnome-screenshot", blob)
        self.assertNotIn("hyprctl clients", blob)
        self.assertEqual(extras["intended_version"], "0.9.0")
        self.assertEqual(extras["observed_version"], "0.9.0")

    def test_local_archive_scps_host_path_and_passes_archive_flag(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            archive = tmp / "strata-0.9.0-x86_64-unknown-linux-gnu.tar.gz"
            payload = b"fixture-archive-bytes"
            archive.write_bytes(payload)
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("ubuntu-2404"),
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                install_from="local-archive",
                archive_path=archive,
            )
            names = [s["name"] for s in steps]
            self.assertEqual(list(names), list(INSTALL_FROM_RELEASE_STEPS))
            blob = "\n".join(commands)
            self.assertIn("--archive", blob)
            self.assertIn(GUEST_ARCHIVE_REMOTE, blob)
            self.assertIn(str(archive), blob)
            self.assertIn("smoke-install.sh", blob)
            self.assertEqual(extras["archive_sha256"], sha256_file(archive))
            self.assertEqual(
                extras["archive_sha256"], hashlib.sha256(payload).hexdigest()
            )
            self.assertEqual(extras["intended_version"], "0.9.0")
            self.assertEqual(extras["observed_version"], "0.9.0")

    def test_local_archive_missing_path_fail_closed(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2", tmp, ssh_port=22022, identity=identity
            )
            with self.assertRaises(SessionSmokeError) as ctx:
                run_install_from_release_steps(
                    machine,
                    guest=load_guest("ubuntu-2404"),
                    screenshot_dest=tmp / "shot.png",
                    run=fake,
                    install_from="local-archive",
                    intended_version="0.9.0",
                )
        self.assertIn("archive", str(ctx.exception).lower())
        self.assertNotIn("not implemented", str(ctx.exception))

    def test_version_mismatch_fails(self) -> None:
        fake = _FakeRun()
        fake.version_stdout = "0.1.0\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2", tmp, ssh_port=22022, identity=identity
            )
            with self.assertRaises(SessionSmokeError) as ctx:
                run_install_from_release_steps(
                    machine,
                    guest=load_guest("arch"),
                    screenshot_dest=tmp / "shot.png",
                    session_timeout=5,
                    run=fake,
                    sleep=lambda _s: None,
                    intended_version="0.9.0",
                )
        self.assertIn("version mismatch", str(ctx.exception))
        self.assertIn("0.9.0", str(ctx.exception))
        self.assertIn("0.1.0", str(ctx.exception))


class VersionParseTests(unittest.TestCase):
    def test_observed_version_strips_prefix(self) -> None:
        self.assertEqual(parse_observed_version("0.9.0\n"), "0.9.0")
        self.assertEqual(parse_observed_version("strata 0.9.0\n"), "0.9.0")
        self.assertEqual(parse_observed_version("v0.9.0\n"), "0.9.0")
        self.assertTrue(versions_match("v0.9.0", "0.9.0"))
        with self.assertRaises(SessionSmokeError):
            parse_observed_version("\n")

    def test_archive_version_from_filename(self) -> None:
        self.assertEqual(
            parse_archive_version(
                "/tmp/strata-0.9.0-x86_64-unknown-linux-gnu.tar.gz"
            ),
            "0.9.0",
        )
        self.assertEqual(
            parse_archive_version("strata-v1.2.3-aarch64-unknown-linux-gnu.tar.gz"),
            "1.2.3",
        )
        self.assertEqual(
            parse_archive_version("strata-0.9.0-rc.1-x86_64-unknown-linux-gnu.tar.gz"),
            "0.9.0-rc.1",
        )
        with self.assertRaises(SessionSmokeError):
            parse_archive_version("/tmp/not-an-archive.tar.gz")

    def test_update_from_version_normalizes_and_rejects_junk(self) -> None:
        self.assertEqual(parse_update_from_version("v0.15.0"), "0.15.0")
        self.assertEqual(parse_update_from_version("0.14.0"), "0.14.0")
        with self.assertRaises(SessionSmokeError) as ctx:
            parse_update_from_version("")
        self.assertEqual(str(ctx.exception), UPDATE_FROM_MISSING_VERSION)
        with self.assertRaises(SessionSmokeError) as ctx:
            parse_update_from_version("latest")
        self.assertIn("not a Strata release tag", str(ctx.exception))

    def test_update_from_versions_default_and_env_override(self) -> None:
        self.assertEqual(parse_update_from_versions(None), DEFAULT_UPDATE_FROM_VERSIONS)
        self.assertEqual(parse_update_from_versions("  "), DEFAULT_UPDATE_FROM_VERSIONS)
        self.assertGreaterEqual(len(DEFAULT_UPDATE_FROM_VERSIONS), 1)
        self.assertEqual(
            parse_update_from_versions("0.15.0, 0.14.0,0.15.0"),
            ("0.15.0", "0.14.0"),
        )
        self.assertEqual(
            update_from_versions(environ={config.UPDATE_FROM_ENV: "v0.13.0,0.12.0"}),
            ("0.13.0", "0.12.0"),
        )
        with self.assertRaises(SessionSmokeError) as ctx:
            parse_update_from_versions(",")
        self.assertEqual(str(ctx.exception), UPDATE_FROM_EMPTY_LIST)

    def test_configured_previous_versions_have_pinned_release_urls(self) -> None:
        for version in update_from_versions(environ={}):
            with self.subTest(version=version):
                parsed = parse_update_from_version(version)
                url = previous_release_url(parsed)
                self.assertIn(f"/download/v{parsed}/", url)
                self.assertNotIn("/latest/", url)
                self.assertNotIn("/current/", url)
                self.assertEqual(
                    previous_release_archive_name(parsed),
                    f"strata-{parsed}-x86_64-unknown-linux-gnu.tar.gz",
                )
                self.assertTrue(url.endswith(previous_release_archive_name(parsed)))

    def test_github_latest_version_uses_injected_opener(self) -> None:
        class _Resp:
            def __init__(self) -> None:
                self._url = (
                    "https://github.com/lgse/strata/releases/tag/v1.2.3"
                )

            def read(self) -> bytes:
                return b'{"tag_name":"v1.2.3"}'

            def geturl(self) -> str:
                return self._url

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> bool:
                return False

        def opener(_req, timeout=15):
            del timeout
            return _Resp()

        self.assertEqual(github_latest_version(opener=opener), "1.2.3")

    def test_install_argv_helpers(self) -> None:
        self.assertEqual(
            install_sh_argv(),
            list(INSTALL_SH_FLAGS),
        )
        argv = install_sh_argv(archive=GUEST_ARCHIVE_REMOTE)
        self.assertEqual(argv[:3], list(INSTALL_SH_FLAGS))
        self.assertEqual(argv[3:], ["--archive", GUEST_ARCHIVE_REMOTE])
        cmd = install_smoke_command(archive=GUEST_ARCHIVE_REMOTE)
        self.assertIn("--archive", cmd)
        self.assertIn(GUEST_ARCHIVE_REMOTE, cmd)
        self.assertIn("smoke-install.sh", cmd)
        arch_cmd = install_smoke_command(forbid_omarchy=True)
        self.assertIn("SMOKE_FORBID_OMARCHY=1", arch_cmd)
        omarchy_cmd = install_smoke_command(forbid_omarchy=False)
        self.assertNotIn("SMOKE_FORBID_OMARCHY", omarchy_cmd)
        self.assertEqual(
            list(INSTALL_SH_FLAGS),
            [
                "--non-interactive",
                "--with-desktop-entry",
                "--without-file-chooser",
            ],
        )
        self.assertNotIn("--with-omarchy-keybinds", INSTALL_SH_FLAGS)
        self.assertNotIn("--with-omarchy-keybinds", install_sh_argv())
        self.assertTrue(strata_version_is_cli(0, "0.9.0\n"))
        self.assertTrue(strata_version_is_cli(0, "strata 1.2.3\n"))
        self.assertFalse(strata_version_is_cli(0, ""))
        self.assertFalse(
            strata_version_is_cli(0, "Gtk-Message: Failed to connect\n")
        )


class InstallArchHelperTests(unittest.TestCase):
    def test_helper_saves_then_execs_existing_flags(self) -> None:
        helper = install_arch_script()
        self.assertTrue(helper.is_file())
        text = helper.read_text(encoding="utf-8")
        self.assertIn("https://raw.githubusercontent.com/lgse/strata/main/install.sh", text)
        self.assertIn("--non-interactive", text)
        self.assertIn("--with-desktop-entry", text)
        self.assertIn("--without-file-chooser", text)
        self.assertIn("INSTALL_SH_SHA256", text)
        commands = [
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertFalse(any("|" in line and "bash" in line for line in commands))
        self.assertNotIn("strata --version", text)
        self.assertIn("omarchy unexpectedly present", text)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
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
            for name in ("sha256sum", "awk", "bash", "chmod", "mkdir", "cat", "echo"):
                found = shutil.which(name)
                self.assertIsNotNone(found, name)
                assert found is not None
                os.symlink(found, bindir / name)

            env = os.environ.copy()
            env["PATH"] = str(bindir)
            env["HOME"] = str(home)
            env["INSTALL_SH_DEST"] = str(dest)
            env["INSTALL_ARGV_RECORD"] = str(record)
            env["OMARCHY_SHARE"] = str(tmp / "no-omarchy-share")
            env["OMARCHY_LOCAL"] = str(tmp / "no-omarchy-local")
            bash = shutil.which("bash")
            self.assertIsNotNone(bash)
            assert bash is not None
            proc = subprocess.run(
                [bash, str(helper)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
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
                ],
            )
            self.assertTrue((home / ".local/bin/strata").is_file())


class Omarchy4InstallFromTests(unittest.TestCase):
    def test_install_from_release_hyprland_oracle_existing_flags(self) -> None:
        fake = _FakeRun()
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            dest = tmp / "screenshot.png"
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("omarchy-4"),
                screenshot_dest=dest,
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                intended_version="0.9.0",
            )
        names = [s["name"] for s in steps]
        self.assertEqual(list(names), list(INSTALL_FROM_RELEASE_STEPS))
        blob = "\n".join(commands)
        self.assertIn("smoke-install.sh", blob)
        self.assertNotIn("smoke-omarchy-detect.sh", blob)
        self.assertNotIn("smoke-omarchy-bindings.sh", blob)
        self.assertIn("--non-interactive", blob)
        self.assertIn("--with-desktop-entry", blob)
        self.assertIn("--without-file-chooser", blob)
        self.assertNotIn("--with-omarchy-keybinds", blob)
        self.assertNotIn("SMOKE_FORBID_OMARCHY", blob)
        self.assertIn("hyprctl clients", blob)
        self.assertIn("grim", blob)
        self.assertIn("gtk-launch", blob)
        self.assertNotIn("NameHasOwner", blob)
        self.assertEqual(extras["install_method"], "install.sh")
        self.assertNotIn("omarchy_major", extras)

    def test_version_step_skipped_when_not_cli(self) -> None:
        fake = _FakeRun()
        fake.version_stdout = "Gtk-Message: Failed to open display\n"
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("omarchy-4"),
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                intended_version="0.9.0",
            )
        version = [s for s in steps if s["name"] == "version"][0]
        self.assertEqual(version["status"], "skip")
        self.assertIn("not a CLI", version["reason"])
        self.assertNotIn("observed_version", extras)
        self.assertIn("session", [s["name"] for s in steps])
        self.assertIn("install", [s["name"] for s in steps])
        self.assertIn("window", [s["name"] for s in steps])
        self.assertNotIn("omarchy-detect", [s["name"] for s in steps])


class Omarchy3InstallFromTests(unittest.TestCase):
    def test_install_from_release_hyprland_oracle_existing_flags(self) -> None:
        fake = _FakeRun()
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            dest = tmp / "screenshot.png"
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("omarchy-3"),
                screenshot_dest=dest,
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                intended_version="0.9.0",
            )
        names = [s["name"] for s in steps]
        self.assertEqual(list(names), list(INSTALL_FROM_RELEASE_STEPS))
        blob = "\n".join(commands)
        self.assertIn("smoke-install.sh", blob)
        self.assertNotIn("smoke-omarchy-detect.sh", blob)
        self.assertNotIn("smoke-omarchy-bindings.sh", blob)
        self.assertNotIn("SMOKE_FORBID_OMARCHY", blob)
        self.assertIn("hyprctl clients", blob)
        self.assertEqual(extras["install_method"], "install.sh")

    def test_version_step_skipped_when_not_cli(self) -> None:
        fake = _FakeRun()
        fake.version_stdout = "Gtk-Message: Failed to open display\n"
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("omarchy-3"),
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                intended_version="0.9.0",
            )
        version = [s for s in steps if s["name"] == "version"][0]
        self.assertEqual(version["status"], "skip")
        self.assertIn("not a CLI", version["reason"])
        self.assertNotIn("observed_version", extras)
        self.assertNotIn("omarchy-detect", [s["name"] for s in steps])


class OmarchyBindingsFlowTests(unittest.TestCase):
    def test_omarchy4_detect_and_lua_bindings(self) -> None:
        fake = _FakeRun()
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            install_sh = tmp / "install.sh"
            install_sh.write_text("#!/bin/bash\n", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            steps, extras = run_omarchy_bindings_steps(
                machine,
                guest=load_guest("omarchy-4"),
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                install_sh_path=install_sh,
            )
        self.assertEqual([s["name"] for s in steps], list(OMARCHY_BINDINGS_STEPS))
        blob = "\n".join(commands)
        self.assertNotIn("smoke-install.sh", blob)
        self.assertIn("smoke-omarchy-detect.sh", blob)
        self.assertIn("smoke-omarchy-bindings.sh", blob)
        self.assertIn("SMOKE_WRITE_BINDINGS=1", blob)
        self.assertIn("SMOKE_OMARCHY_CASE=token", blob)
        self.assertIn("SMOKE_OMARCHY_CASE=command", blob)
        self.assertIn(OMARCHY_DEV_HASH_OUTPUT, blob)
        for output, _want in OMARCHY_TOKEN_CASES:
            self.assertIn(output, blob)
        self.assertEqual(extras["omarchy_major"], "4")
        self.assertEqual(extras["omarchy_bindings"], "lua")
        self.assertEqual(extras["omarchy_pr743_probes"], "pass")
        detect = [s for s in steps if s["name"] == "omarchy-detect"][0]
        self.assertEqual(detect["detected_major"], "4")
        bindings = [s for s in steps if s["name"] == "omarchy-bindings"][0]
        self.assertEqual(bindings["kind"], "lua")

    def test_omarchy3_writes_conf_bindings(self) -> None:
        fake = _FakeRun()
        fake.detected_major = "3"
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            steps, extras = run_omarchy_bindings_steps(
                machine,
                guest=load_guest("omarchy-3"),
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
            )
        self.assertEqual(extras["omarchy_major"], "3")
        self.assertEqual(extras["omarchy_bindings"], "conf")
        blob = "\n".join(commands)
        self.assertIn("curl -fsSL", blob)
        self.assertIn("SMOKE_OMARCHY_MAJOR=3", blob)

    def test_wrong_live_major_fails_closed(self) -> None:
        fake = _FakeRun()
        fake.detected_major = "3"
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            with self.assertRaises(SessionSmokeError) as ctx:
                run_omarchy_bindings_steps(
                    machine,
                    guest=load_guest("omarchy-4"),
                    screenshot_dest=tmp / "screenshot.png",
                    session_timeout=5,
                    run=fake,
                    sleep=lambda _s: None,
                )
        self.assertIn("live major '3'", str(ctx.exception))

    def test_arch_guest_is_not_supported(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            with self.assertRaises(SessionSmokeError) as ctx:
                run_omarchy_bindings_steps(
                    machine,
                    guest=load_guest("arch"),
                    screenshot_dest=tmp / "screenshot.png",
                    session_timeout=5,
                    run=fake,
                    sleep=lambda _s: None,
                )
        self.assertEqual(str(ctx.exception), OMARCHY_BINDINGS_FAIL_CLOSED)


class OmarchyHelperTests(unittest.TestCase):
    def test_install_major_and_commands(self) -> None:
        self.assertEqual(omarchy_install_major("omarchy-4"), 4)
        self.assertEqual(omarchy_install_major(load_guest("omarchy-3")), 3)
        self.assertIsNone(omarchy_install_major("arch"))
        self.assertIn("SMOKE_OMARCHY_CASE=live", omarchy_detect_command())
        self.assertIn(
            "SMOKE_OMARCHY_OUTPUT=",
            omarchy_detect_command(case="token", output="dev (b280f130)"),
        )
        self.assertEqual(
            omarchy_bindings_command(4),
            "SMOKE_OMARCHY_MAJOR=4 bash /tmp/smoke-omarchy-bindings.sh",
        )
        self.assertIn("SMOKE_WRITE_BINDINGS=1", omarchy_bindings_command(4, write=True))
        self.assertTrue(smoke_omarchy_detect_script().is_file())
        self.assertTrue(smoke_omarchy_bindings_script().is_file())
        self.assertEqual(parse_smoke_kv("DETECTED_MAJOR=\n", "DETECTED_MAJOR"), "")
        self.assertEqual(parse_smoke_kv("DETECTED_MAJOR=4\n", "DETECTED_MAJOR"), "4")


class UdiskieUnlockFlowTests(unittest.TestCase):
    def test_omarchy4_uploads_smoke_and_runs_cases(self) -> None:
        fake = _FakeRun()
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            install_sh = tmp / "install.sh"
            install_sh.write_text("#!/bin/bash\n", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            steps, extras = run_udiskie_unlock_steps(
                machine,
                guest=load_guest("omarchy-4"),
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                install_sh_path=install_sh,
            )
        self.assertEqual([s["name"] for s in steps], list(UDISKIE_UNLOCK_STEPS))
        blob = "\n".join(commands)
        self.assertIn("smoke-udiskie-unlock.sh", blob)
        self.assertNotIn("smoke-install.sh", blob)
        self.assertIn("SMOKE_UDISKIE_CASE=parse-args", blob)
        self.assertIn("--with-udiskie-unlock", blob)
        self.assertIn("SMOKE_UDISKIE_STUB=1", blob)
        self.assertIn("SMOKE_MARKER=1", blob)
        self.assertIn("SMOKE_PROMPT=yes", blob)
        self.assertIn("SMOKE_OMARCHY_MAJOR=4", blob)
        self.assertIn("eligible-prompt-yes", extras["udiskie_unlock_cases"])
        oracle = [s for s in steps if s["name"] == "udiskie-unlock"][0]
        self.assertEqual(oracle["status"], "pass")

    def test_arch_runs_host_udiskie_ignored_case(self) -> None:
        fake = _FakeRun()
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            install_sh = tmp / "install.sh"
            install_sh.write_text("#!/bin/bash\n", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            steps, extras = run_udiskie_unlock_steps(
                machine,
                guest=load_guest("arch"),
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                install_sh_path=install_sh,
            )
        blob = "\n".join(commands)
        self.assertIn("SMOKE_ARCH_BASED=yes", blob)
        self.assertIn("SMOKE_HOST_UDISKIE=1", blob)
        self.assertNotIn("SMOKE_OMARCHY_MAJOR=", blob)
        self.assertIn("arch-no-stub-ask", extras["udiskie_unlock_cases"])
        self.assertEqual([s["name"] for s in steps], list(UDISKIE_UNLOCK_STEPS))

    def test_ubuntu_guest_is_not_supported(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            with self.assertRaises(SessionSmokeError) as ctx:
                run_udiskie_unlock_steps(
                    machine,
                    guest=load_guest("ubuntu-2404"),
                    screenshot_dest=tmp / "screenshot.png",
                    session_timeout=5,
                    run=fake,
                    sleep=lambda _s: None,
                    install_sh_path=tmp / "install.sh",
                )
        self.assertEqual(str(ctx.exception), UDISKIE_UNLOCK_FAIL_CLOSED)

    def test_command_and_fixture_helpers(self) -> None:
        self.assertTrue(supports_udiskie_unlock("arch"))
        self.assertTrue(supports_udiskie_unlock(load_guest("omarchy-3")))
        self.assertFalse(supports_udiskie_unlock("ubuntu-2404"))
        self.assertTrue(smoke_udiskie_unlock_script().is_file())
        self.assertTrue(udiskie_unlock_install_sh_fixture().is_file())
        remote = udiskie_unlock_command(
            case="configure",
            omarchy_major="4",
            arch_based="yes",
            udiskie_stub=True,
            marker=True,
            prompt="yes",
        )
        self.assertIn("SMOKE_UDISKIE_CASE=configure", remote)
        self.assertIn("SMOKE_OMARCHY_MAJOR=4", remote)
        self.assertIn("SMOKE_UDISKIE_STUB=1", remote)
        self.assertIn("SMOKE_MARKER=1", remote)
        self.assertIn("bash /tmp/smoke-udiskie-unlock.sh", remote)
        self.assertNotIn("--with-udiskie-unlock", INSTALL_SH_FLAGS)


class UpdateFromStepsTests(unittest.TestCase):
    def test_update_smoke_command_quotes_phase_and_version(self) -> None:
        remote = update_smoke_command(phase="previous", from_version="0.15.0")
        self.assertIn("SMOKE_UPDATE_PHASE=previous", remote)
        self.assertIn("UPDATE_FROM_VERSION=0.15.0", remote)
        self.assertIn("bash /tmp/smoke-update.sh", remote)
        self.assertNotIn("SMOKE_FORBID_OMARCHY", remote)
        arch = update_smoke_command(
            phase="latest", forbid_omarchy=True, archive="/tmp/prev.tar.gz"
        )
        self.assertIn("SMOKE_FORBID_OMARCHY=1", arch)
        self.assertIn("UPDATE_FROM_ARCHIVE=/tmp/prev.tar.gz", arch)
        self.assertIn("SMOKE_UPDATE_PHASE=latest", arch)

    def test_supports_same_guests_as_install_from(self) -> None:
        for guest_id in (
            "arch",
            "ubuntu-2404",
            "fedora-workstation",
            "omarchy-4",
            "omarchy-3",
        ):
            with self.subTest(guest=guest_id):
                self.assertTrue(supports_update_from(guest_id))
                self.assertTrue(supports_install_from_release(guest_id))

    def _drive(
        self,
        guest_id: str,
        *,
        from_version: str,
        latest: str,
        fake: _FakeRun | None = None,
    ) -> tuple[list[dict], dict, list[str]]:
        responder = fake if fake is not None else _FakeRun()
        responder.from_version = from_version
        responder.version_stdout = [f"{from_version}\n", f"{latest}\n"]
        if guest_id in {"arch", "omarchy-4", "omarchy-3"}:
            responder.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            steps, extras = run_update_from_steps(
                machine,
                guest=load_guest(guest_id),
                from_version=from_version,
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=responder,
                commands=commands,
                sleep=lambda _s: None,
                intended_version=latest,
            )
        return steps, extras, commands

    def test_each_configured_previous_version_records_update_steps(self) -> None:
        latest = "0.16.0"
        for from_version in DEFAULT_UPDATE_FROM_VERSIONS:
            with self.subTest(from_version=from_version):
                steps, extras, commands = self._drive(
                    "ubuntu-2404", from_version=from_version, latest=latest
                )
                self.assertEqual([s["name"] for s in steps], list(UPDATE_FROM_STEPS))
                blob = "\n".join(commands)
                self.assertIn("smoke-update.sh", blob)
                self.assertIn("SMOKE_UPDATE_PHASE=previous", blob)
                self.assertIn(f"UPDATE_FROM_VERSION={from_version}", blob)
                self.assertIn("SMOKE_UPDATE_PHASE=latest", blob)
                self.assertNotIn("SMOKE_FORBID_OMARCHY", blob)
                self.assertIn("NameHasOwner", blob)
                self.assertIn("gtk-launch", blob)
                self.assertEqual(extras["from_version"], from_version)
                self.assertEqual(extras["intended_version"], latest)
                self.assertEqual(extras["observed_previous_version"], from_version)
                self.assertEqual(extras["observed_version"], latest)
                self.assertEqual(extras["install_method"], "install.sh")
                self.assertIn("about_version_before", extras)
                self.assertIn("about_version_after", extras)
                self.assertTrue(extras["about_version_before"].endswith(ABOUT_BEFORE_PNG_NAME))
                self.assertTrue(extras["about_version_after"].endswith(ABOUT_AFTER_PNG_NAME))
                prev = [s for s in steps if s["name"] == "version-previous"][0]
                self.assertEqual(prev["status"], "pass")
                self.assertEqual(prev["observed"], from_version)
                latest_step = [s for s in steps if s["name"] == "version"][0]
                self.assertEqual(latest_step["status"], "pass")
                self.assertEqual(latest_step["observed"], latest)

    def test_arch_update_from_forbids_omarchy_and_uses_hyprland(self) -> None:
        from_version = DEFAULT_UPDATE_FROM_VERSIONS[0]
        steps, extras, commands = self._drive(
            "arch", from_version=from_version, latest="0.16.0"
        )
        blob = "\n".join(commands)
        self.assertIn("SMOKE_FORBID_OMARCHY=1", blob)
        self.assertIn("hyprctl clients", blob)
        self.assertIn("grim", blob)
        self.assertNotIn("NameHasOwner", blob)
        self.assertEqual([s["name"] for s in steps], list(UPDATE_FROM_STEPS))
        self.assertEqual(extras["from_version"], from_version)

    def test_env_override_is_the_matrix_the_suite_iterates(self) -> None:
        override = ("0.13.0", "0.12.1")
        parsed = update_from_versions(
            environ={config.UPDATE_FROM_ENV: ",".join(override)}
        )
        self.assertEqual(parsed, override)
        for from_version in parsed:
            with self.subTest(from_version=from_version):
                steps, extras, _commands = self._drive(
                    "fedora-workstation",
                    from_version=from_version,
                    latest="0.16.0",
                )
                self.assertEqual(extras["from_version"], from_version)
                self.assertEqual([s["name"] for s in steps], list(UPDATE_FROM_STEPS))

    def test_from_equals_latest_fails_closed_before_guest_commands(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2", tmp, ssh_port=22022, identity=identity
            )
            with self.assertRaises(SessionSmokeError) as ctx:
                run_update_from_steps(
                    machine,
                    guest=load_guest("ubuntu-2404"),
                    from_version="0.16.0",
                    screenshot_dest=tmp / "shot.png",
                    run=fake,
                    intended_version="0.16.0",
                )
        self.assertIn(UPDATE_FROM_SAME_AS_LATEST, str(ctx.exception))
        self.assertEqual(fake.remote, [])

    def test_previous_version_mismatch_fails(self) -> None:
        fake = _FakeRun()
        fake.from_version = "0.14.0"
        fake.version_stdout = ["0.14.0\n", "0.16.0\n"]
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2", tmp, ssh_port=22022, identity=identity
            )
            with self.assertRaises(SessionSmokeError) as ctx:
                run_update_from_steps(
                    machine,
                    guest=load_guest("ubuntu-2404"),
                    from_version="0.15.0",
                    screenshot_dest=tmp / "shot.png",
                    session_timeout=5,
                    run=fake,
                    sleep=lambda _s: None,
                    intended_version="0.16.0",
                )
        self.assertIn("install-previous", str(ctx.exception))
        self.assertIn("0.15.0", str(ctx.exception))

    def test_latest_version_mismatch_fails(self) -> None:
        fake = _FakeRun()
        fake.from_version = "0.15.0"
        fake.version_stdout = ["0.15.0\n", "0.15.0\n"]
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2", tmp, ssh_port=22022, identity=identity
            )
            with self.assertRaises(SessionSmokeError) as ctx:
                run_update_from_steps(
                    machine,
                    guest=load_guest("ubuntu-2404"),
                    from_version="0.15.0",
                    screenshot_dest=tmp / "shot.png",
                    session_timeout=5,
                    run=fake,
                    sleep=lambda _s: None,
                    intended_version="0.16.0",
                )
        self.assertIn("version mismatch", str(ctx.exception))
        self.assertIn("0.16.0", str(ctx.exception))
        self.assertIn("0.15.0", str(ctx.exception))


class AboutVersionTests(unittest.TestCase):
    def test_sidebar_nav_is_five_tabs_then_space(self) -> None:
        self.assertEqual(ABOUT_SIDEBAR_TABS, 5)
        chords = qmp_open_about_chords()
        self.assertEqual(chords[0], ["ctrl", "comma"])
        self.assertEqual(chords[1:-1], [["tab"]] * ABOUT_SIDEBAR_TABS)
        self.assertEqual(chords[-1], ["spc"])
        self.assertEqual(
            qmp_about_nav_chords(),
            [["tab"]] * ABOUT_SIDEBAR_TABS + [["spc"]],
        )
        remote = about_smoke_command(compositor="Hyprland")
        self.assertIn("SMOKE_COMPOSITOR=Hyprland", remote)
        self.assertIn("SMOKE_ABOUT_TABS=5", remote)

    def test_skips_when_cli_version_already_recorded(self) -> None:
        fake = _FakeRun()
        sent: list[list[str]] = []
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2", tmp, ssh_port=22022, identity=identity
            )
            step = run_about_version_step(
                machine,
                env={"WAYLAND_DISPLAY": "wayland-0"},
                compositor="gnome-shell",
                screenshot_dest=tmp / ABOUT_VERSION_PNG_NAME,
                cli_version_passed=True,
                intended="0.16.0",
                run=fake,
                send_key=lambda *_a, **_k: sent.append(["x"]) or True,
            )
        self.assertEqual(step["status"], "skip")
        self.assertEqual(step["reason"], ABOUT_CLI_ALREADY_RECORDED)
        self.assertEqual(sent, [])
        self.assertFalse(any("smoke-about.sh" in c for c in fake.remote))

    def test_wtype_path_screenshots_about_page(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2", tmp, ssh_port=22022, identity=identity
            )
            dest = tmp / ABOUT_VERSION_PNG_NAME
            step = run_about_version_step(
                machine,
                env={"WAYLAND_DISPLAY": "wayland-0"},
                compositor="gnome-shell",
                screenshot_dest=dest,
                cli_version_passed=False,
                intended="0.16.0",
                run=fake,
                sleep=lambda _s: None,
                send_key=lambda *_a, **_k: False,
            )
            self.assertEqual(step["status"], "pass")
            self.assertEqual(step["oracle"], "settings-about")
            self.assertEqual(step["input"], "wtype")
            self.assertEqual(step["intended"], "0.16.0")
            self.assertTrue(dest.is_file())
            blob = "\n".join(fake.remote)
            self.assertIn("smoke-about.sh", blob)
            self.assertIn("gnome-screenshot", blob)

    def test_qmp_fallback_tabs_to_about_when_guest_has_no_wtype(self) -> None:
        fake = _FakeRun()
        fake.about_code = 2
        fake.about_stdout = "INPUT=missing\n"
        sent: list[list[str]] = []

        def send_key(_sock, keys, **_kwargs):
            sent.append(list(keys))
            return True

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2", tmp, ssh_port=22022, identity=identity
            )
            dest = tmp / ABOUT_VERSION_PNG_NAME
            step = run_about_version_step(
                machine,
                env={"WAYLAND_DISPLAY": "wayland-0", "HYPRLAND_INSTANCE_SIGNATURE": "s"},
                compositor="Hyprland",
                screenshot_dest=dest,
                cli_version_passed=False,
                intended="0.15.0",
                run=fake,
                sleep=lambda _s: None,
                send_key=send_key,
            )
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["input"], "qmp")
        self.assertEqual(sent[0], ["ctrl", "comma"])
        self.assertEqual(sent.count(["tab"]), ABOUT_SIDEBAR_TABS)
        self.assertEqual(sent[-1], ["spc"])
        self.assertNotIn(["v"], sent)
        blob = "\n".join(fake.remote)
        self.assertIn("grim", blob)

    def test_install_from_skips_about_when_cli_version_passes(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2", tmp, ssh_port=22022, identity=identity
            )
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("ubuntu-2404"),
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=fake,
                sleep=lambda _s: None,
                intended_version="0.9.0",
            )
        names = [s["name"] for s in steps]
        self.assertEqual(names, list(INSTALL_FROM_RELEASE_STEPS))
        about = [s for s in steps if s["name"] == "about-version"][0]
        self.assertEqual(about["status"], "skip")
        self.assertEqual(about["reason"], ABOUT_CLI_ALREADY_RECORDED)
        self.assertNotIn("about_version", extras)
        self.assertFalse(any("smoke-about.sh" in c for c in fake.remote))

    def test_install_from_uses_about_when_cli_version_skips(self) -> None:
        fake = _FakeRun()
        fake.version_stdout = "Gtk-Message: Failed to open display\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("omarchy-4"),
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=fake,
                sleep=lambda _s: None,
                intended_version="0.9.0",
            )
        version = [s for s in steps if s["name"] == "version"][0]
        about = [s for s in steps if s["name"] == "about-version"][0]
        self.assertEqual(version["status"], "skip")
        self.assertEqual(about["status"], "pass")
        self.assertEqual(about["oracle"], "settings-about")
        self.assertIn("about_version", extras)
        self.assertTrue(any("smoke-about.sh" in c for c in fake.remote))


if __name__ == "__main__":
    unittest.main()
