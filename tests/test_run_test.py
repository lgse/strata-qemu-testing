"""run-test --session-only and vm-run. No KVM; refuse qemu-system-*."""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from strataqemu import config
from strataqemu.cli import CheckHostResult, build_parser, main
from strataqemu.guest import load_guest
from strataqemu.image_build import golden_qcow2
from strataqemu.overlay import create_overlay_argv
from strataqemu.qemu import Machine
from strataqemu.run_test import (
    choose_graphical_ui,
    discard_throwaway_disks,
    require_golden,
    run_run_test,
    run_test_qemu_argv,
    run_vm_run,
    vm_run_qemu_argv,
)
from strataqemu.tests_spec import (
    DEFAULT_UPDATE_FROM_VERSIONS,
    INSTALL_FROM_FAIL_CLOSED,
    INSTALL_SH_MISSING_PATH,
    LOCAL_ARCHIVE_MISSING_PATH,
    OMARCHY_BINDINGS_EXCLUSIVE,
    OMARCHY_BINDINGS_FAIL_CLOSED,
    OMARCHY_BINDINGS_STEPS,
    UDISKIE_UNLOCK_EXCLUSIVE,
    UDISKIE_UNLOCK_FAIL_CLOSED,
    UDISKIE_UNLOCK_STEPS,
    UPDATE_FROM_EXCLUSIVE,
    UPDATE_FROM_STEPS,
    missing_golden_message,
    sha256_file,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256


def _after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _all_after(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, tok in enumerate(argv) if tok == flag]


def _refuse_qemu_system(cmd, *args, **kwargs):
    name = Path(str(cmd[0])).name if cmd else ""
    if name.startswith("qemu-system"):
        raise AssertionError(f"spawned qemu-system: {cmd}")
    raise AssertionError(f"unexpected subprocess: {cmd}")


class _DummyProc:
    def __init__(self) -> None:
        self.returncode = 0

    def poll(self) -> None:
        return None

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:
        return None


class _FakeRun:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.version_stdout: str | list[str] = "0.9.0\n"
        self._version_i = 0
        self.from_version = "0.15.0"

    def _version_reply(self) -> str:
        value = self.version_stdout
        if isinstance(value, list):
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
            if dest.suffix == ".png":
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(PNG)
            return subprocess.CompletedProcess(argv, 0, "", "")
        remote = str(argv[-1]) if argv else ""
        if "command -v grim" in remote:
            return subprocess.CompletedProcess(argv, 0, "/usr/bin/grim\n", "")
        if "command -v gnome-screenshot" in remote:
            return subprocess.CompletedProcess(
                argv, 0, "/usr/bin/gnome-screenshot\n", ""
            )
        if "smoke-session.sh" in remote:
            return subprocess.CompletedProcess(
                argv,
                0,
                "SESSION_ID=c1\n"
                "XDG_RUNTIME_DIR=/run/user/1000\n"
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\n"
                "WAYLAND_DISPLAY=wayland-0\n"
                "HYPRLAND_INSTANCE_SIGNATURE=sig\n",
                "",
            )
        if "smoke-omarchy-detect.sh" in remote:
            major = "4"
            if "SMOKE_OMARCHY_CASE=token" in remote:
                if "4.0.0" in remote:
                    major = "4"
                elif "3.8.5" in remote:
                    major = "3"
                else:
                    major = ""
            return subprocess.CompletedProcess(
                argv,
                0,
                f"SMOKE_OMARCHY_CASE=live\nDETECTED_MAJOR={major}\n",
                "",
            )
        if "smoke-omarchy-bindings.sh" in remote:
            kind = "lua" if "SMOKE_OMARCHY_MAJOR=4" in remote else "conf"
            return subprocess.CompletedProcess(
                argv,
                0,
                f"BINDINGS_KIND={kind}\nBINDINGS_PATH=/home/tester/.config/hypr/bindings.{kind}\n",
                "",
            )
        if "smoke-luks-hotplug.sh" in remote:
            if "SMOKE_LUKS_CASE=wrap" in remote:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "WRAPPED=1\nHOOK_LOG=/tmp/strata-luks-hook.log\n",
                    "",
                )
            return subprocess.CompletedProcess(
                argv,
                0,
                "STRATA_CALLED=1\n"
                "LUKS_DEV=vdc\n"
                "LUKS_SERIAL=strata-luks\n"
                "HOOK_LINE=--udiskie-hook device_added crypto /dev/vdc uuid\n",
                "",
            )
        if "udiskie/config.yml" in remote:
            return subprocess.CompletedProcess(argv, 0, "HOOK_CONFIG=1\n", "")
        if "pacman -S --needed --noconfirm udiskie" in remote and "gtk4" not in remote:
            return subprocess.CompletedProcess(argv, 0, "UDISKIE=ok\n", "")
        if "printf 'UDISKIE=" in remote:
            return subprocess.CompletedProcess(argv, 0, "UDISKIE=ok\n", "")
        if "smoke-udiskie-unlock.sh" in remote:
            if "SMOKE_UDISKIE_CASE=parse-args" in remote:
                with_flag = (
                    "yes" if "--with-udiskie-unlock" in remote else "ask"
                )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "NON_INTERACTIVE=yes\n"
                    f"WITH_UDISKIE_UNLOCK={with_flag}\n"
                    "BIN_CALLS=0\n",
                    "",
                )
            calls = "0"
            on_path = "1" if "SMOKE_UDISKIE_STUB=1" in remote else "0"
            if (
                "SMOKE_PROMPT=yes" in remote
                and "SMOKE_UDISKIE_STUB=1" in remote
                and "SMOKE_MARKER=1" in remote
            ):
                calls = "1"
            body = (
                f"BIN_CALLS={calls}\n"
                f"UDISKIE_ON_PATH={on_path}\n"
                "RELEASE_SUPPORTS=1\n"
                "UDISKIE_RAN=0\n"
                "DECOY_CALLS=0\n"
            )
            if calls == "1":
                body += "BIN_ARGV=--install-udiskie-unlock\n"
            return subprocess.CompletedProcess(argv, 0, body, "")
        if "smoke-about.sh" in remote:
            return subprocess.CompletedProcess(argv, 0, "INPUT=wtype\n", "")
        if "smoke-update.sh" in remote:
            digest = "ab" * 32
            if "SMOKE_UPDATE_PHASE=previous" in remote:
                return subprocess.CompletedProcess(
                    argv, 0, f"FROM_VERSION={self.from_version}\n", ""
                )
            return subprocess.CompletedProcess(
                argv,
                0,
                f"INSTALL_SH_SHA256={digest}\nInstalled Strata v9.9.9\n",
                "",
            )
        if "smoke-install.sh" in remote or "install-arch.sh" in remote:
            digest = "ab" * 32
            return subprocess.CompletedProcess(
                argv,
                0,
                f"INSTALL_SH_SHA256={digest}\nInstalled Strata v9.9.9\n",
                "",
            )
        if "Exec=" in remote or "Strata.desktop" in remote:
            return subprocess.CompletedProcess(
                argv, 0, "Exec=/home/tester/.local/bin/strata %U\n", ""
            )
        if "--version" in remote and "strata" in remote:
            return subprocess.CompletedProcess(argv, 0, self._version_reply(), "")
        if "gtk-launch" in remote or "gio launch" in remote:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "hyprctl clients" in remote:
            return subprocess.CompletedProcess(argv, 0, "{}\n", "")
        if "NameHasOwner" in remote:
            return subprocess.CompletedProcess(argv, 0, "(true,)\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def _host_ok(tmp: Path) -> CheckHostResult:
    code = tmp / "OVMF_CODE.4m.fd"
    code.write_bytes(b"fw")
    key = tmp / "id_ed25519"
    key.write_text("k", encoding="utf-8")
    key.with_name(key.name + ".pub").write_text("p", encoding="utf-8")
    return CheckHostResult(ok=True, errors=(), ovmf_code=code, ssh_key=key)


def _cache_with_golden(tmp: Path, guest_id: str = "ubuntu-2404") -> tuple[Path, Path]:
    guest = load_guest(guest_id)
    cache = tmp / "cache"
    golden = golden_qcow2(guest, cache)
    golden.parent.mkdir(parents=True, exist_ok=True)
    golden.write_bytes(b"golden-bytes")
    golden.chmod(0o444)
    return cache, golden


class MissingGoldenTests(unittest.TestCase):
    def test_run_test_session_only_missing_golden_twice(self) -> None:
        guest = load_guest("ubuntu-2404")
        msg = missing_golden_message("ubuntu-2404")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "empty-cache"
            cache.mkdir()
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            texts = []
            try:
                for _ in range(2):
                    buf = io.StringIO()
                    err = io.StringIO()
                    with (
                        redirect_stdout(buf),
                        redirect_stderr(err),
                        patch(
                            "subprocess.Popen", side_effect=_refuse_qemu_system
                        ) as popen,
                        patch(
                            "subprocess.run", side_effect=_refuse_qemu_system
                        ) as run,
                        patch(
                            "strataqemu.image_build.run_image_build"
                        ) as build,
                        patch(
                            "strataqemu.image_build.build_live"
                        ) as live,
                    ):
                        code = main(
                            [
                                "run-test",
                                "--",
                                "ubuntu-2404",
                                "--session-only",
                            ]
                        )
                    self.assertEqual(code, 1)
                    text = buf.getvalue() + err.getvalue()
                    texts.append(text)
                    self.assertIn(msg, text)
                    self.assertNotIn("not implemented", text)
                    popen.assert_not_called()
                    run.assert_not_called()
                    build.assert_not_called()
                    live.assert_not_called()
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(texts[0], texts[1])
        self.assertFalse(golden_qcow2(guest, cache).exists())

    def test_fedora_session_only_missing_golden_twice(self) -> None:
        guest = load_guest("fedora-workstation")
        msg = missing_golden_message("fedora-workstation")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "empty-cache"
            cache.mkdir()
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            texts = []
            try:
                for _ in range(2):
                    buf = io.StringIO()
                    err = io.StringIO()
                    with (
                        redirect_stdout(buf),
                        redirect_stderr(err),
                        patch(
                            "subprocess.Popen", side_effect=_refuse_qemu_system
                        ) as popen,
                        patch(
                            "subprocess.run", side_effect=_refuse_qemu_system
                        ) as run,
                        patch(
                            "strataqemu.image_build.run_image_build"
                        ) as build,
                        patch(
                            "strataqemu.image_build.build_live"
                        ) as live,
                    ):
                        code = main(
                            [
                                "run-test",
                                "--",
                                "fedora-workstation",
                                "--session-only",
                            ]
                        )
                    self.assertEqual(code, 1)
                    text = buf.getvalue() + err.getvalue()
                    texts.append(text)
                    self.assertIn(msg, text)
                    self.assertNotIn("not implemented", text)
                    popen.assert_not_called()
                    run.assert_not_called()
                    build.assert_not_called()
                    live.assert_not_called()
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(texts[0], texts[1])
        self.assertFalse(golden_qcow2(guest, cache).exists())

    def test_vm_run_missing_golden_same_message(self) -> None:
        msg = missing_golden_message("ubuntu-2404")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "empty-cache"
            cache.mkdir()
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            err = io.StringIO()
            try:
                with (
                    redirect_stderr(err),
                    patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
                    patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
                    patch("strataqemu.image_build.run_image_build") as build,
                ):
                    code = main(["vm-run", "ubuntu-2404", "--graphical"])
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(code, 1)
        self.assertIn(msg, err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()
        build.assert_not_called()

    def test_require_golden_does_not_build(self) -> None:
        guest = load_guest("ubuntu-2404")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            with self.assertRaises(Exception) as ctx:
                require_golden(guest, cache)
        self.assertIn("mise run image-build", str(ctx.exception))


class ArgvTests(unittest.TestCase):
    def test_run_test_argv_is_frozen_gl_unsafe_no_vnc_on_second_display(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            overlay = tmp / "run" / "overlay.qcow2"
            argv = run_test_qemu_argv(
                overlay=overlay,
                run_dir=tmp / "run",
                ssh_port=22022,
                vnc_port=5901,
                cpus=4,
                memory_mib=8192,
                ovmf_code=tmp / "OVMF_CODE.4m.fd",
                ovmf_vars=tmp / "run" / "OVMF_VARS.fd",
            )
        self.assertEqual(argv[0], "qemu-system-x86_64")
        self.assertEqual(_after(argv, "-vga"), "none")
        self.assertIn("virtio-gpu-gl-pci", argv)
        self.assertNotIn("virtio-vga-gl", argv)
        self.assertEqual(_after(argv, "-display"), "egl-headless,gl=on")
        self.assertEqual(_after(argv, "-vnc"), "127.0.0.1:5901")
        drives = _all_after(argv, "-drive")
        drive0 = [d for d in drives if "id=drive0" in d][0]
        self.assertIn("cache=unsafe", drive0)
        self.assertNotIn("cache=writeback", drive0)
        self.assertFalse(any("cidata" in tok for tok in argv))

    def test_vm_run_graphical_gtk_no_vnc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            argv = vm_run_qemu_argv(
                overlay=tmp / "overlay.qcow2",
                run_dir=tmp / "run",
                ssh_port=22022,
                vnc_port=5901,
                cpus=4,
                memory_mib=8192,
                graphical=True,
                graphical_ui="gtk",
            )
        self.assertEqual(_after(argv, "-vga"), "none")
        self.assertIn("virtio-vga-gl", argv)
        self.assertNotIn("virtio-gpu-gl-pci", argv)
        self.assertEqual(_after(argv, "-display"), "gtk,gl=on")
        self.assertNotIn("-vnc", argv)
        self.assertEqual(argv.count("-vnc"), 0)
        drives = _all_after(argv, "-drive")
        drive0 = [d for d in drives if "id=drive0" in d][0]
        self.assertIn("cache=unsafe", drive0)

    def test_vm_run_graphical_sdl_fallback_no_vnc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            argv = vm_run_qemu_argv(
                overlay=tmp / "overlay.qcow2",
                run_dir=tmp / "run",
                ssh_port=22022,
                cpus=4,
                memory_mib=8192,
                graphical=True,
                graphical_ui="sdl",
            )
        self.assertEqual(_after(argv, "-display"), "sdl,gl=on")
        self.assertIn("virtio-vga-gl", argv)
        self.assertNotIn("-vnc", argv)

    def test_choose_graphical_ui_prefers_gtk(self) -> None:
        self.assertEqual(
            choose_graphical_ui("gtk            GTK display\nsdl            SDL2\n"),
            "gtk",
        )
        self.assertEqual(choose_graphical_ui("sdl            SDL2\n"), "sdl")

    def test_overlay_argv_strict_absolute_backing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            golden = tmp / "images" / "ubuntu-2404-deadbeef.qcow2"
            golden.parent.mkdir(parents=True)
            golden.write_bytes(b"g")
            overlay = tmp / "runs" / "r1" / "overlay.qcow2"
            argv = create_overlay_argv(golden, overlay)
        self.assertIn("backing_file_strict=on", argv)
        self.assertTrue(Path(argv[argv.index("-b") + 1]).is_absolute())
        self.assertEqual(Path(argv[argv.index("-b") + 1]), golden.resolve())

    def test_parser_has_no_maintain(self) -> None:
        parser = build_parser()
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            parser.parse_args(["vm-run", "ubuntu-2404", "--maintain"])
        self.assertIn("maintain", err.getvalue())


class SessionOnlyWiringTests(unittest.TestCase):
    def test_session_only_with_golden_does_not_install_or_spawn_real_qemu(self) -> None:
        fake = _FakeRun()
        recorded_argv: list[list[str]] = []

        def popen(argv, **kwargs):
            name = Path(str(argv[0])).name if argv else ""
            if name.startswith("qemu-system"):
                recorded_argv.append(list(argv))
                return _DummyProc()
            raise AssertionError(f"unexpected Popen: {argv}")

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            self.assertTrue(Path(golden).is_absolute() or Path(golden).exists())
            src = Path(golden)
            self.assertEqual(src.stat().st_mode & 0o222, 0)
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, golden = _cache_with_golden(tmp)
            vars_template = tmp / "OVMF_VARS.4m.fd"
            vars_template.write_bytes(b"vars")
            host = _host_ok(tmp)
            mtime = golden.stat().st_mtime_ns
            contents = golden.read_bytes()
            buf = io.StringIO()
            err = io.StringIO()
            with (
                redirect_stdout(buf),
                redirect_stderr(err),
                patch(
                    "strataqemu.run_test.find_ovmf_vars",
                    return_value=vars_template,
                ),
                patch.object(Machine, "shutdown", return_value="kill"),
                patch(
                    "strataqemu.tests_spec.qmp_screendump", return_value=False
                ),
            ):
                code = run_run_test(
                    "ubuntu-2404",
                    session_only=True,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                )
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(golden.read_bytes(), contents)
            self.assertEqual(golden.stat().st_mtime_ns, mtime)
            self.assertTrue(recorded_argv)
            qemu_argv = recorded_argv[0]
            self.assertEqual(qemu_argv[0], "qemu-system-x86_64")
            self.assertIn("virtio-gpu-gl-pci", qemu_argv)
            self.assertEqual(_after(qemu_argv, "-display"), "egl-headless,gl=on")
            self.assertIn("-vnc", qemu_argv)
            self.assertNotIn("virtio-vga-gl", qemu_argv)
            blob = " ".join(str(c) for c in fake.calls)
            self.assertNotIn("install.sh", blob)
            self.assertNotIn("strata --version", blob)
            self.assertNotIn("gtk-launch", blob)
            self.assertNotIn("NameHasOwner", blob)
            self.assertNotIn("io.github.lgse.Strata", blob)
            runs = cache / "runs"
            result_files = list(runs.glob("*/result.json"))
            self.assertTrue(result_files, list(runs.rglob("*")))
            text = result_files[0].read_text(encoding="utf-8")
            self.assertIn('"name": "session"', text)
            self.assertIn('"status": "pass"', text)
            shots = list(runs.glob("*/screenshot.png"))
            self.assertTrue(shots)
            self.assertTrue(shots[0].read_bytes().startswith(b"\x89PNG"))

    def test_success_without_keep_saves_screenshot_drops_overlay(self) -> None:
        fake = _FakeRun()

        def popen(argv, **kwargs):
            name = Path(str(argv[0])).name if argv else ""
            if name.startswith("qemu-system"):
                return _DummyProc()
            raise AssertionError(f"unexpected Popen: {argv}")

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, _golden = _cache_with_golden(tmp)
            vars_template = tmp / "OVMF_VARS.4m.fd"
            vars_template.write_bytes(b"vars")
            host = _host_ok(tmp)
            buf = io.StringIO()
            err = io.StringIO()
            with (
                redirect_stdout(buf),
                redirect_stderr(err),
                patch(
                    "strataqemu.run_test.find_ovmf_vars",
                    return_value=vars_template,
                ),
                patch.object(Machine, "shutdown", return_value="kill"),
                patch(
                    "strataqemu.tests_spec.qmp_screendump", return_value=False
                ),
            ):
                code = run_run_test(
                    "ubuntu-2404",
                    session_only=True,
                    keep=False,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                )
            self.assertEqual(code, 0, err.getvalue())
            runs = list((cache / "runs").iterdir())
            self.assertEqual(len(runs), 1, runs)
            run_dir = runs[0]
            self.assertTrue((run_dir / "screenshot.png").is_file())
            self.assertTrue(
                (run_dir / "screenshot.png").read_bytes().startswith(b"\x89PNG")
            )
            self.assertTrue((run_dir / "result.json").is_file())
            self.assertFalse((run_dir / "overlay.qcow2").exists())
            self.assertFalse((run_dir / "OVMF_VARS.fd").exists())
            out = buf.getvalue()
            self.assertIn("screenshot:", out)
            self.assertIn(str(run_dir / "screenshot.png"), out)
            self.assertIn(str(run_dir), out)
            self.assertTrue((run_dir / "screenshot.png").exists())

    def test_discard_throwaway_keeps_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            run_dir.mkdir()
            (run_dir / "overlay.qcow2").write_bytes(b"disk")
            (run_dir / "OVMF_VARS.fd").write_bytes(b"vars")
            (run_dir / "qmp.sock").write_bytes(b"")
            (run_dir / "qga.sock").write_bytes(b"")
            (run_dir / "screenshot.png").write_bytes(PNG)
            (run_dir / "result.json").write_text("{}\n", encoding="utf-8")
            (run_dir / "serial.log").write_text("boot\n", encoding="utf-8")
            discard_throwaway_disks(run_dir)
            self.assertFalse((run_dir / "overlay.qcow2").exists())
            self.assertFalse((run_dir / "OVMF_VARS.fd").exists())
            self.assertTrue((run_dir / "screenshot.png").is_file())
            self.assertTrue((run_dir / "result.json").is_file())
            self.assertTrue((run_dir / "serial.log").is_file())

    def test_vm_run_graphical_records_gtk_argv(self) -> None:
        recorded: list[list[str]] = []

        def popen(argv, **kwargs):
            recorded.append(list(argv))
            return _DummyProc()

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, _golden = _cache_with_golden(tmp)
            vars_template = tmp / "OVMF_VARS.4m.fd"
            vars_template.write_bytes(b"vars")
            host = _host_ok(tmp)
            buf = io.StringIO()
            err = io.StringIO()
            with (
                redirect_stdout(buf),
                redirect_stderr(err),
                patch(
                    "strataqemu.run_test.find_ovmf_vars",
                    return_value=vars_template,
                ),
                patch.object(Machine, "shutdown", return_value="kill"),
            ):
                code = run_vm_run(
                    "ubuntu-2404",
                    graphical=True,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                    graphical_ui="gtk",
                    wait=False,
                )
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue(recorded)
        argv = recorded[0]
        self.assertIn("virtio-vga-gl", argv)
        self.assertEqual(_after(argv, "-display"), "gtk,gl=on")
        self.assertNotIn("-vnc", argv)


class HelpAndMiseTests(unittest.TestCase):
    def test_run_test_help_has_session_only(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["run-test", "--help"])
        self.assertEqual(code, 0)
        text = buf.getvalue() + err.getvalue()
        self.assertIn("--session-only", text)
        self.assertIn("--keep", text)
        self.assertIn("--install-from", text)
        self.assertIn("release", text)
        self.assertIn("local-archive", text)
        self.assertIn("--omarchy-bindings", text)
        self.assertIn("--update-from", text)
        self.assertIn("--udiskie-unlock", text)
        self.assertIn("--luks-hotplug", text)
        self.assertNotIn("not implemented", text)

    def test_vm_run_help_has_graphical(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["vm-run", "--help"])
        self.assertEqual(code, 0)
        text = buf.getvalue() + err.getvalue()
        self.assertIn("--graphical", text)
        self.assertIn("--keep", text)
        self.assertNotIn("not implemented", text)
        self.assertNotIn("--maintain", text)

    def test_vm_live_help_has_from_tag_and_from_local(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["vm-live", "--help"])
        self.assertEqual(code, 0)
        text = buf.getvalue() + err.getvalue()
        self.assertIn("--from-tag", text)
        self.assertIn("--from-local", text)
        self.assertIn("--headless", text)
        self.assertIn("--keep", text)
        self.assertNotIn("not implemented", text)


class ArchInstallFromTests(unittest.TestCase):
    def test_missing_golden_install_from_release_twice(self) -> None:
        guest = load_guest("arch")
        msg = missing_golden_message("arch")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "empty-cache"
            cache.mkdir()
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            texts = []
            try:
                for _ in range(2):
                    buf = io.StringIO()
                    err = io.StringIO()
                    with (
                        redirect_stdout(buf),
                        redirect_stderr(err),
                        patch(
                            "subprocess.Popen", side_effect=_refuse_qemu_system
                        ) as popen,
                        patch(
                            "subprocess.run", side_effect=_refuse_qemu_system
                        ) as run,
                        patch(
                            "strataqemu.image_build.run_image_build"
                        ) as build,
                        patch(
                            "strataqemu.image_build.build_live"
                        ) as live,
                    ):
                        code = main(
                            [
                                "run-test",
                                "--",
                                "arch",
                                "--install-from",
                                "release",
                            ]
                        )
                    self.assertEqual(code, 1)
                    text = buf.getvalue() + err.getvalue()
                    texts.append(text)
                    self.assertIn(msg, text)
                    self.assertNotIn("not implemented", text)
                    popen.assert_not_called()
                    run.assert_not_called()
                    build.assert_not_called()
                    live.assert_not_called()
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(texts[0], texts[1])
        self.assertFalse(golden_qcow2(guest, cache).exists())

    def test_ubuntu_install_from_release_missing_golden_twice(self) -> None:
        guest = load_guest("ubuntu-2404")
        msg = missing_golden_message("ubuntu-2404")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "empty-cache"
            cache.mkdir()
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            texts = []
            try:
                for _ in range(2):
                    buf = io.StringIO()
                    err = io.StringIO()
                    with (
                        redirect_stdout(buf),
                        redirect_stderr(err),
                        patch(
                            "subprocess.Popen", side_effect=_refuse_qemu_system
                        ) as popen,
                        patch(
                            "subprocess.run", side_effect=_refuse_qemu_system
                        ) as run,
                        patch(
                            "strataqemu.image_build.run_image_build"
                        ) as build,
                    ):
                        code = main(
                            [
                                "run-test",
                                "--",
                                "ubuntu-2404",
                                "--install-from",
                                "release",
                            ]
                        )
                    self.assertEqual(code, 1)
                    text = buf.getvalue() + err.getvalue()
                    texts.append(text)
                    self.assertIn(msg, text)
                    self.assertNotIn("not implemented", text)
                    popen.assert_not_called()
                    run.assert_not_called()
                    build.assert_not_called()
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(texts[0], texts[1])
        self.assertFalse(golden_qcow2(guest, cache).exists())

    def test_fedora_install_from_release_missing_golden(self) -> None:
        msg = missing_golden_message("fedora-workstation")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "empty-cache"
            cache.mkdir()
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            err = io.StringIO()
            try:
                with (
                    redirect_stderr(err),
                    patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
                    patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
                    patch("strataqemu.image_build.run_image_build") as build,
                ):
                    code = main(
                        [
                            "run-test",
                            "--",
                            "fedora-workstation",
                            "--install-from",
                            "release",
                        ]
                    )
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(code, 1)
        self.assertIn(msg, err.getvalue())
        self.assertNotIn("not implemented", err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()
        build.assert_not_called()

    def test_omarchy4_install_from_release_missing_golden_twice(self) -> None:
        guest = load_guest("omarchy-4")
        msg = missing_golden_message("omarchy-4")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "empty-cache"
            cache.mkdir()
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            texts = []
            try:
                for _ in range(2):
                    buf = io.StringIO()
                    err = io.StringIO()
                    with (
                        redirect_stdout(buf),
                        redirect_stderr(err),
                        patch(
                            "subprocess.Popen", side_effect=_refuse_qemu_system
                        ) as popen,
                        patch(
                            "subprocess.run", side_effect=_refuse_qemu_system
                        ) as run,
                        patch(
                            "strataqemu.image_build.run_image_build"
                        ) as build,
                        patch(
                            "strataqemu.image_build.build_live"
                        ) as live,
                    ):
                        code = main(
                            [
                                "run-test",
                                "--",
                                "omarchy-4",
                                "--install-from",
                                "release",
                            ]
                        )
                    self.assertEqual(code, 1)
                    text = buf.getvalue() + err.getvalue()
                    texts.append(text)
                    self.assertIn(msg, text)
                    self.assertNotIn("not implemented", text)
                    popen.assert_not_called()
                    run.assert_not_called()
                    build.assert_not_called()
                    live.assert_not_called()
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(texts[0], texts[1])
        self.assertFalse(golden_qcow2(guest, cache).exists())

    def test_omarchy3_install_from_release_missing_golden_twice(self) -> None:
        guest = load_guest("omarchy-3")
        msg = missing_golden_message("omarchy-3")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "empty-cache"
            cache.mkdir()
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            texts = []
            try:
                for _ in range(2):
                    buf = io.StringIO()
                    err = io.StringIO()
                    with (
                        redirect_stdout(buf),
                        redirect_stderr(err),
                        patch(
                            "subprocess.Popen", side_effect=_refuse_qemu_system
                        ) as popen,
                        patch(
                            "subprocess.run", side_effect=_refuse_qemu_system
                        ) as run,
                        patch(
                            "strataqemu.image_build.run_image_build"
                        ) as build,
                        patch(
                            "strataqemu.image_build.build_live"
                        ) as live,
                    ):
                        code = main(
                            [
                                "run-test",
                                "--",
                                "omarchy-3",
                                "--install-from",
                                "release",
                            ]
                        )
                    self.assertEqual(code, 1)
                    text = buf.getvalue() + err.getvalue()
                    texts.append(text)
                    self.assertIn(msg, text)
                    self.assertNotIn("not implemented", text)
                    popen.assert_not_called()
                    run.assert_not_called()
                    build.assert_not_called()
                    live.assert_not_called()
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(texts[0], texts[1])
        self.assertFalse(golden_qcow2(guest, cache).exists())

    def test_missing_install_sh_env_fail_closed_no_qemu(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        missing = Path("/tmp/does-not-exist-strata-install.sh")
        old = os.environ.get(config.INSTALL_SH_ENV)
        os.environ[config.INSTALL_SH_ENV] = str(missing)
        try:
            with (
                redirect_stdout(buf),
                redirect_stderr(err),
                patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
                patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
            ):
                code = main(
                    ["run-test", "--", "omarchy-4", "--omarchy-bindings"]
                )
            self.assertEqual(code, 2, err.getvalue())
            self.assertIn(INSTALL_SH_MISSING_PATH, err.getvalue())
            popen.assert_not_called()
            run.assert_not_called()
        finally:
            if old is None:
                os.environ.pop(config.INSTALL_SH_ENV, None)
            else:
                os.environ[config.INSTALL_SH_ENV] = old

    def test_local_archive_missing_path_fail_closed_no_qemu(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with (
            redirect_stdout(buf),
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(
                ["run-test", "--", "ubuntu-2404", "--install-from", "local-archive"]
            )
        self.assertEqual(code, 2)
        text = buf.getvalue() + err.getvalue()
        self.assertIn(LOCAL_ARCHIVE_MISSING_PATH, text)
        self.assertNotIn(INSTALL_FROM_FAIL_CLOSED, text)
        popen.assert_not_called()
        run.assert_not_called()

    def test_local_archive_missing_file_fail_closed_no_qemu(self) -> None:
        missing = "/no/such/strata-archive-missing.tar.gz"
        buf = io.StringIO()
        err = io.StringIO()
        with (
            redirect_stdout(buf),
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(
                [
                    "run-test",
                    "--",
                    "fedora-workstation",
                    "--install-from",
                    "local-archive",
                    missing,
                ]
            )
        self.assertEqual(code, 2)
        text = buf.getvalue() + err.getvalue()
        self.assertIn("archive not found", text)
        self.assertIn(missing, text)
        self.assertNotIn(INSTALL_FROM_FAIL_CLOSED, text)
        popen.assert_not_called()
        run.assert_not_called()

    def test_install_from_release_records_five_steps_and_version(self) -> None:
        fake = _FakeRun()
        recorded_argv: list[list[str]] = []

        def popen(argv, **kwargs):
            name = Path(str(argv[0])).name if argv else ""
            if name.startswith("qemu-system"):
                recorded_argv.append(list(argv))
                return _DummyProc()
            raise AssertionError(f"unexpected Popen: {argv}")

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, golden = _cache_with_golden(tmp, "arch")
            host = _host_ok(tmp)
            buf = io.StringIO()
            err = io.StringIO()
            with (
                redirect_stdout(buf),
                redirect_stderr(err),
                patch.object(Machine, "shutdown", return_value="kill"),
                patch(
                    "strataqemu.tests_spec.qmp_screendump", return_value=False
                ),
            ):
                code = run_run_test(
                    "arch",
                    install_from="release",
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                    intended_version="0.9.0",
                )
            self.assertEqual(code, 0, err.getvalue())
            self.assertTrue(recorded_argv)
            qemu_argv = recorded_argv[0]
            self.assertNotIn("if=pflash", " ".join(qemu_argv))
            blob = " ".join(str(c) for c in fake.calls)
            self.assertIn("smoke-install.sh", blob)
            self.assertIn("--non-interactive", blob)
            self.assertIn("--with-desktop-entry", blob)
            self.assertIn("--without-file-chooser", blob)
            self.assertIn("gtk-launch", blob)
            self.assertIn("hyprctl clients", blob)
            self.assertIn("$HOME/.local/bin/strata --version", blob)
            self.assertNotIn("NameHasOwner", blob)
            helper = (
                Path(__file__).resolve().parents[1]
                / "guest-tests"
                / "smoke-install.sh"
            ).read_text(encoding="utf-8")
            self.assertIn("--non-interactive", helper)
            self.assertIn("--with-desktop-entry", helper)
            self.assertIn("--without-file-chooser", helper)
            result_files = list((cache / "runs").glob("*/result.json"))
            self.assertTrue(result_files)
            text = result_files[0].read_text(encoding="utf-8")
            self.assertIn('"name": "session"', text)
            self.assertIn('"name": "install"', text)
            self.assertIn('"name": "version"', text)
            self.assertIn('"name": "desktop-entry"', text)
            self.assertIn('"name": "window"', text)
            self.assertIn("install_sh_sha256", text)
            self.assertIn("intended_version", text)
            self.assertIn("observed_version", text)
            self.assertIn("0.9.0", text)
            self.assertIn("abababab", text)
            self.assertNotIn("9.9.9", text)
            del golden

    def test_omarchy_bindings_records_detect_and_bindings(self) -> None:
        fake = _FakeRun()
        recorded_argv: list[list[str]] = []

        def popen(argv, **kwargs):
            name = Path(str(argv[0])).name if argv else ""
            if name.startswith("qemu-system"):
                recorded_argv.append(list(argv))
                return _DummyProc()
            raise AssertionError(f"unexpected Popen: {argv}")

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, golden = _cache_with_golden(tmp, "omarchy-4")
            host = _host_ok(tmp)
            install_sh = tmp / "install.sh"
            install_sh.write_text("#!/bin/bash\n", encoding="utf-8")
            buf = io.StringIO()
            err = io.StringIO()
            with (
                redirect_stdout(buf),
                redirect_stderr(err),
                patch.object(Machine, "shutdown", return_value="kill"),
                patch(
                    "strataqemu.tests_spec.qmp_screendump", return_value=False
                ),
            ):
                code = run_run_test(
                    "omarchy-4",
                    omarchy_bindings=True,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                    install_sh_path=install_sh,
                )
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn("omarchy-bindings ok", buf.getvalue())
            blob = " ".join(str(c) for c in fake.calls)
            self.assertIn("smoke-omarchy-detect.sh", blob)
            self.assertIn("smoke-omarchy-bindings.sh", blob)
            self.assertNotIn("smoke-install.sh", blob)
            result_files = list((cache / "runs").glob("*/result.json"))
            self.assertTrue(result_files)
            text = result_files[0].read_text(encoding="utf-8")
            for name in OMARCHY_BINDINGS_STEPS:
                self.assertIn(f'"name": "{name}"', text)
            self.assertIn("omarchy_major", text)
            self.assertIn("omarchy_bindings", text)
            del golden

    def test_udiskie_unlock_uploads_smoke_without_qemu_system(self) -> None:
        fake = _FakeRun()
        recorded_argv: list[list[str]] = []

        def popen(argv, **kwargs):
            name = Path(str(argv[0])).name if argv else ""
            if name.startswith("qemu-system"):
                recorded_argv.append(list(argv))
                return _DummyProc()
            raise AssertionError(f"unexpected Popen: {argv}")

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, golden = _cache_with_golden(tmp, "omarchy-4")
            host = _host_ok(tmp)
            install_sh = tmp / "install.sh"
            install_sh.write_text("#!/bin/bash\n", encoding="utf-8")
            buf = io.StringIO()
            err = io.StringIO()
            with (
                redirect_stdout(buf),
                redirect_stderr(err),
                patch.object(Machine, "shutdown", return_value="kill"),
                patch(
                    "strataqemu.tests_spec.qmp_screendump", return_value=False
                ),
            ):
                code = run_run_test(
                    "omarchy-4",
                    udiskie_unlock=True,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                    install_sh_path=install_sh,
                )
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn("udiskie-unlock ok", buf.getvalue())
            blob = " ".join(str(c) for c in fake.calls)
            self.assertIn("smoke-udiskie-unlock.sh", blob)
            self.assertNotIn("smoke-install.sh", blob)
            result_files = list((cache / "runs").glob("*/result.json"))
            self.assertTrue(result_files)
            text = result_files[0].read_text(encoding="utf-8")
            for name in UDISKIE_UNLOCK_STEPS:
                self.assertIn(f'"name": "{name}"', text)
            self.assertIn("udiskie_unlock_cases", text)
            del golden

    def test_udiskie_unlock_rejects_other_guests(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with (
            redirect_stdout(buf),
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(["run-test", "--", "ubuntu-2404", "--udiskie-unlock"])
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn(UDISKIE_UNLOCK_FAIL_CLOSED, err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()

    def test_udiskie_unlock_exclusive_with_session_only(self) -> None:
        err = io.StringIO()
        with (
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(
                [
                    "run-test",
                    "--",
                    "arch",
                    "--udiskie-unlock",
                    "--session-only",
                ]
            )
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn(UDISKIE_UNLOCK_EXCLUSIVE, err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()

    def test_udiskie_unlock_exclusive_with_install_from(self) -> None:
        err = io.StringIO()
        with (
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(
                [
                    "run-test",
                    "--",
                    "arch",
                    "--udiskie-unlock",
                    "--install-from",
                    "release",
                ]
            )
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn(UDISKIE_UNLOCK_EXCLUSIVE, err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()

    def test_udiskie_unlock_exclusive_with_omarchy_bindings(self) -> None:
        err = io.StringIO()
        with (
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(
                [
                    "run-test",
                    "--",
                    "omarchy-4",
                    "--udiskie-unlock",
                    "--omarchy-bindings",
                ]
            )
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn(UDISKIE_UNLOCK_EXCLUSIVE, err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()

    def test_udiskie_unlock_exclusive_with_update_from(self) -> None:
        err = io.StringIO()
        with (
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(
                [
                    "run-test",
                    "--",
                    "arch",
                    "--udiskie-unlock",
                    "--update-from",
                    "0.15.0",
                ]
            )
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn(UDISKIE_UNLOCK_EXCLUSIVE, err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()

    def test_udiskie_unlock_missing_install_sh_env_fail_closed_no_qemu(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        missing = Path("/tmp/does-not-exist-strata-install.sh")
        old = os.environ.get(config.INSTALL_SH_ENV)
        os.environ[config.INSTALL_SH_ENV] = str(missing)
        try:
            with (
                redirect_stdout(buf),
                redirect_stderr(err),
                patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
                patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
            ):
                code = main(
                    ["run-test", "--", "omarchy-4", "--udiskie-unlock"]
                )
            self.assertEqual(code, 2, err.getvalue())
            self.assertIn(INSTALL_SH_MISSING_PATH, err.getvalue())
            popen.assert_not_called()
            run.assert_not_called()
        finally:
            if old is None:
                os.environ.pop(config.INSTALL_SH_ENV, None)
            else:
                os.environ[config.INSTALL_SH_ENV] = old

    def test_omarchy_bindings_rejects_other_guests(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with (
            redirect_stdout(buf),
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(["run-test", "--", "arch", "--omarchy-bindings"])
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn(OMARCHY_BINDINGS_FAIL_CLOSED, err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()

    def test_omarchy_bindings_exclusive_with_install_from(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with (
            redirect_stdout(buf),
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(
                [
                    "run-test",
                    "--",
                    "omarchy-4",
                    "--omarchy-bindings",
                    "--install-from",
                    "release",
                ]
            )
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn(OMARCHY_BINDINGS_EXCLUSIVE, err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()

    def test_update_from_exclusive_with_install_from(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with (
            redirect_stdout(buf),
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(
                [
                    "run-test",
                    "--",
                    "ubuntu-2404",
                    "--update-from",
                    "0.15.0",
                    "--install-from",
                    "release",
                ]
            )
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn(UPDATE_FROM_EXCLUSIVE, err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()

    def test_update_from_exclusive_with_session_only(self) -> None:
        err = io.StringIO()
        with (
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(
                [
                    "run-test",
                    "--",
                    "arch",
                    "--session-only",
                    "--update-from",
                    "0.15.0",
                ]
            )
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn(UPDATE_FROM_EXCLUSIVE, err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()

    def test_update_from_missing_version_fail_closed_no_qemu(self) -> None:
        err = io.StringIO()
        with (
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(["run-test", "--", "ubuntu-2404", "--update-from"])
        self.assertNotEqual(code, 0)
        text = err.getvalue()
        self.assertTrue(
            "update-from" in text.lower() or "VERSION" in text or "required" in text.lower()
        )
        popen.assert_not_called()
        run.assert_not_called()

    def test_update_from_junk_version_fail_closed_no_qemu(self) -> None:
        err = io.StringIO()
        with (
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(
                ["run-test", "--", "ubuntu-2404", "--update-from", "latest"]
            )
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn("not a Strata release tag", err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()

    def test_update_from_missing_golden_twice(self) -> None:
        msg = missing_golden_message("ubuntu-2404")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "empty-cache"
            cache.mkdir()
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            texts = []
            try:
                for _ in range(2):
                    err = io.StringIO()
                    with (
                        redirect_stderr(err),
                        patch(
                            "subprocess.Popen", side_effect=_refuse_qemu_system
                        ) as popen,
                        patch(
                            "subprocess.run", side_effect=_refuse_qemu_system
                        ) as run,
                        patch("strataqemu.image_build.run_image_build") as build,
                    ):
                        code = main(
                            [
                                "run-test",
                                "--",
                                "ubuntu-2404",
                                "--update-from",
                                DEFAULT_UPDATE_FROM_VERSIONS[0],
                            ]
                        )
                    self.assertEqual(code, 1)
                    texts.append(err.getvalue())
                    self.assertIn(msg, err.getvalue())
                    popen.assert_not_called()
                    run.assert_not_called()
                    build.assert_not_called()
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(texts[0], texts[1])

    def test_update_from_records_previous_and_latest_versions(self) -> None:
        fake = _FakeRun()
        from_version = DEFAULT_UPDATE_FROM_VERSIONS[0]
        fake.from_version = from_version
        fake.version_stdout = [f"{from_version}\n", "0.16.0\n"]
        recorded_argv: list[list[str]] = []

        def popen(argv, **kwargs):
            name = Path(str(argv[0])).name if argv else ""
            if name.startswith("qemu-system"):
                recorded_argv.append(list(argv))
                return _DummyProc()
            raise AssertionError(f"unexpected Popen: {argv}")

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, golden = _cache_with_golden(tmp, "ubuntu-2404")
            host = _host_ok(tmp)
            buf = io.StringIO()
            err = io.StringIO()
            with (
                redirect_stdout(buf),
                redirect_stderr(err),
                patch.object(Machine, "shutdown", return_value="kill"),
                patch(
                    "strataqemu.tests_spec.qmp_screendump", return_value=False
                ),
            ):
                code = run_run_test(
                    "ubuntu-2404",
                    update_from=from_version,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                    intended_version="0.16.0",
                )
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn(f"update-from {from_version} ok", buf.getvalue())
            blob = " ".join(str(c) for c in fake.calls)
            self.assertIn("smoke-update.sh", blob)
            self.assertIn("SMOKE_UPDATE_PHASE=previous", blob)
            self.assertIn("SMOKE_UPDATE_PHASE=latest", blob)
            self.assertNotIn("smoke-install.sh", blob)
            result_files = list((cache / "runs").glob("*/result.json"))
            self.assertTrue(result_files)
            text = result_files[0].read_text(encoding="utf-8")
            for name in UPDATE_FROM_STEPS:
                self.assertIn(f'"name": "{name}"', text)
            self.assertIn(from_version, text)
            self.assertIn("0.16.0", text)
            self.assertIn("from_version", text)
            self.assertIn("observed_previous_version", text)
            del golden

    def test_ubuntu_install_from_release_records_gnome_bus_and_version(self) -> None:
        fake = _FakeRun()
        recorded_argv: list[list[str]] = []

        def popen(argv, **kwargs):
            name = Path(str(argv[0])).name if argv else ""
            if name.startswith("qemu-system"):
                recorded_argv.append(list(argv))
                return _DummyProc()
            raise AssertionError(f"unexpected Popen: {argv}")

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, _golden = _cache_with_golden(tmp, "ubuntu-2404")
            vars_template = tmp / "OVMF_VARS.4m.fd"
            vars_template.write_bytes(b"vars")
            host = _host_ok(tmp)
            err = io.StringIO()
            with (
                redirect_stderr(err),
                patch(
                    "strataqemu.run_test.find_ovmf_vars",
                    return_value=vars_template,
                ),
                patch.object(Machine, "shutdown", return_value="kill"),
                patch(
                    "strataqemu.tests_spec.qmp_screendump", return_value=False
                ),
            ):
                code = run_run_test(
                    "ubuntu-2404",
                    install_from="release",
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                    intended_version="0.9.0",
                )
            self.assertEqual(code, 0, err.getvalue())
            blob = " ".join(str(c) for c in fake.calls)
            self.assertIn("smoke-install.sh", blob)
            self.assertIn("--non-interactive", blob)
            self.assertIn("--with-desktop-entry", blob)
            self.assertIn("--without-file-chooser", blob)
            self.assertIn("$HOME/.local/bin/strata --version", blob)
            self.assertIn("NameHasOwner", blob)
            self.assertIn("io.github.lgse.Strata", blob)
            self.assertNotIn("hyprctl clients", blob)
            result_files = list((cache / "runs").glob("*/result.json"))
            self.assertTrue(result_files)
            text = result_files[0].read_text(encoding="utf-8")
            for name in (
                "session",
                "install",
                "version",
                "desktop-entry",
                "window",
            ):
                self.assertIn(f'"name": "{name}"', text)
            self.assertIn("install_sh_sha256", text)
            self.assertIn("intended_version", text)
            self.assertIn("observed_version", text)

    def test_local_archive_scps_and_records_archive_sha(self) -> None:
        fake = _FakeRun()

        def popen(argv, **kwargs):
            name = Path(str(argv[0])).name if argv else ""
            if name.startswith("qemu-system"):
                return _DummyProc()
            raise AssertionError(f"unexpected Popen: {argv}")

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, _golden = _cache_with_golden(tmp, "fedora-workstation")
            vars_template = tmp / "OVMF_VARS.4m.fd"
            vars_template.write_bytes(b"vars")
            host = _host_ok(tmp)
            archive = tmp / "strata-0.9.0-x86_64-unknown-linux-gnu.tar.gz"
            payload = b"local-archive-fixture"
            archive.write_bytes(payload)
            err = io.StringIO()
            with (
                redirect_stderr(err),
                patch(
                    "strataqemu.run_test.find_ovmf_vars",
                    return_value=vars_template,
                ),
                patch.object(Machine, "shutdown", return_value="kill"),
                patch(
                    "strataqemu.tests_spec.qmp_screendump", return_value=False
                ),
            ):
                code = run_run_test(
                    "fedora-workstation",
                    install_from="local-archive",
                    archive_path=archive,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                )
            self.assertEqual(code, 0, err.getvalue())
            blob = " ".join(str(c) for c in fake.calls)
            self.assertIn("--archive", blob)
            self.assertIn(str(archive), blob)
            self.assertIn("smoke-install.sh", blob)
            self.assertIn("NameHasOwner", blob)
            result_files = list((cache / "runs").glob("*/result.json"))
            self.assertTrue(result_files)
            text = result_files[0].read_text(encoding="utf-8")
            digest = sha256_file(archive)
            self.assertIn("archive_sha256", text)
            self.assertIn(digest, text)
            self.assertIn('"intended_version": "0.9.0"', text)
            self.assertIn('"observed_version": "0.9.0"', text)

    def test_session_only_arch_does_not_install(self) -> None:
        fake = _FakeRun()

        def popen(argv, **kwargs):
            name = Path(str(argv[0])).name if argv else ""
            if name.startswith("qemu-system"):
                return _DummyProc()
            raise AssertionError(f"unexpected Popen: {argv}")

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, _golden = _cache_with_golden(tmp, "arch")
            host = _host_ok(tmp)
            err = io.StringIO()
            with (
                redirect_stderr(err),
                patch.object(Machine, "shutdown", return_value=None),
                patch(
                    "strataqemu.tests_spec.qmp_screendump", return_value=False
                ),
            ):
                code = run_run_test(
                    "arch",
                    session_only=True,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                )
            self.assertEqual(code, 0, err.getvalue())
            blob = " ".join(str(c) for c in fake.calls)
            self.assertIn("grim", blob)
            self.assertNotIn("install-arch.sh", blob)
            self.assertNotIn("install.sh", blob)
            self.assertNotIn("strata --version", blob)
            self.assertNotIn("gtk-launch", blob)
            self.assertNotIn("NameHasOwner", blob)

    def test_session_only_fedora_uses_gnome_screenshot_not_install(self) -> None:
        fake = _FakeRun()

        def popen(argv, **kwargs):
            name = Path(str(argv[0])).name if argv else ""
            if name.startswith("qemu-system"):
                return _DummyProc()
            raise AssertionError(f"unexpected Popen: {argv}")

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, _golden = _cache_with_golden(tmp, "fedora-workstation")
            vars_template = tmp / "OVMF_VARS.4m.fd"
            vars_template.write_bytes(b"vars")
            host = _host_ok(tmp)
            err = io.StringIO()
            with (
                redirect_stderr(err),
                patch(
                    "strataqemu.run_test.find_ovmf_vars",
                    return_value=vars_template,
                ),
                patch.object(Machine, "shutdown", return_value=None),
                patch(
                    "strataqemu.tests_spec.qmp_screendump", return_value=False
                ),
            ):
                code = run_run_test(
                    "fedora-workstation",
                    session_only=True,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                )
            self.assertEqual(code, 0, err.getvalue())
            blob = " ".join(str(c) for c in fake.calls)
            self.assertIn("gnome-screenshot", blob)
            self.assertNotIn("install.sh", blob)
            self.assertNotIn("strata --version", blob)
            self.assertNotIn("gtk-launch", blob)
            self.assertNotIn("NameHasOwner", blob)


if __name__ == "__main__":
    unittest.main()
