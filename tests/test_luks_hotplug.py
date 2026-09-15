"""LUKS hotplug: host image, QMP attach, guest smoke. No qemu-system-*."""

from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from strataqemu.cli import main
from strataqemu.guest import load_guest
from strataqemu.qemu import (
    LUKS_HOTPLUG_BUS,
    LUKS_HOTPLUG_SERIAL,
    LUKS_IMAGE_PASSPHRASE,
    LUKS_IMAGE_TOOLS_MISSING,
    Machine,
    build_qemu_argv,
    create_luks_image,
    qmp_hotplug_raw_disk,
)
from strataqemu.tests_spec import (
    LUKS_HOTPLUG_EXCLUSIVE,
    LUKS_HOTPLUG_FAIL_CLOSED,
    LUKS_HOTPLUG_HOOK_MISSING,
    LUKS_HOTPLUG_MISSING_PATH,
    LUKS_HOTPLUG_STEPS,
    SessionSmokeError,
    luks_hotplug_command,
    run_luks_hotplug_steps,
    smoke_luks_hotplug_script,
    supports_luks_hotplug,
)

from tests.test_run_test import (
    _DummyProc,
    _FakeRun,
    _cache_with_golden,
    _host_ok,
    _refuse_qemu_system,
)
from tests.test_tests_spec import _FakeRun as _SpecFakeRun
from strataqemu.run_test import run_run_test

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE = REPO_ROOT / "guest-tests" / "smoke-luks-hotplug.sh"


def _write_exec(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class CreateLuksImageTests(unittest.TestCase):
    def test_injected_run_calls_qemu_img_then_cryptsetup(self) -> None:
        calls: list[list[str]] = []

        inputs: list[str] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if "input" in kwargs and kwargs["input"] is not None:
                inputs.append(str(kwargs["input"]))
            dest = Path(str(argv[-2] if argv[0] == "qemu-img" else argv[-1]))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"luks-bytes")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "disk.img"
            got = create_luks_image(dest, run=fake_run)
        self.assertEqual(got, dest)
        self.assertEqual(calls[0][:4], ["qemu-img", "create", "-f", "raw"])
        self.assertEqual(calls[1][0], "cryptsetup")
        self.assertIn("--batch-mode", calls[1])
        self.assertIn("luks1", calls[1])
        self.assertEqual(inputs, [LUKS_IMAGE_PASSPHRASE])

    def test_missing_qemu_img_uses_tools_message(self) -> None:
        def fake_run(argv, **kwargs):
            raise FileNotFoundError("qemu-img")

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError) as ctx:
                create_luks_image(Path(td) / "disk.img", run=fake_run)
        self.assertEqual(str(ctx.exception), LUKS_IMAGE_TOOLS_MISSING)

    def test_real_cryptsetup_writes_luks_header(self) -> None:
        if shutil.which("cryptsetup") is None or shutil.which("qemu-img") is None:
            self.skipTest("qemu-img or cryptsetup missing")
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "disk.img"
            create_luks_image(dest)
            blob = dest.read_bytes()[:4]
        self.assertEqual(blob, b"LUKS")


class QmpHotplugTests(unittest.TestCase):
    def test_blockdev_add_then_device_add(self) -> None:
        calls: list[tuple] = []

        def execute(socket_path, command, arguments=None, **kwargs):
            calls.append((command, arguments))
            return {"return": {}}

        with tempfile.TemporaryDirectory() as td:
            image = Path(td) / "luks.img"
            image.write_bytes(b"x")
            ok = qmp_hotplug_raw_disk(
                Path(td) / "qmp.sock", image, execute=execute
            )
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], "blockdev-add")
        self.assertEqual(calls[0][1]["file"]["filename"], str(image.resolve()))
        self.assertEqual(calls[1][0], "device_add")
        self.assertEqual(calls[1][1]["serial"], LUKS_HOTPLUG_SERIAL)
        self.assertEqual(calls[1][1]["driver"], "virtio-blk-pci")
        self.assertEqual(calls[1][1]["bus"], LUKS_HOTPLUG_BUS)

    def test_hotplug_port_is_opt_in_on_qemu_argv(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            overlay = Path(td) / "overlay.qcow2"
            overlay.write_bytes(b"x")
            plain = build_qemu_argv(
                overlay=overlay, run_dir=td, ssh_port=22022, vnc_port=1
            )
            extra = build_qemu_argv(
                overlay=overlay,
                run_dir=td,
                ssh_port=22022,
                vnc_port=1,
                hotplug_port=True,
            )
        self.assertFalse(any("pcie-root-port" in t for t in plain))
        self.assertTrue(any(f"id={LUKS_HOTPLUG_BUS}" in t for t in extra))

    def test_device_add_error_is_false(self) -> None:
        def execute(socket_path, command, arguments=None, **kwargs):
            if command == "device_add":
                return {"error": {"desc": "no slot"}}
            return {"return": {}}

        with tempfile.TemporaryDirectory() as td:
            image = Path(td) / "luks.img"
            image.write_bytes(b"x")
            ok = qmp_hotplug_raw_disk(
                Path(td) / "qmp.sock", image, execute=execute
            )
        self.assertFalse(ok)


class SmokeLuksHotplugTests(unittest.TestCase):
    def test_script_is_executable(self) -> None:
        self.assertTrue(SMOKE.is_file())
        self.assertTrue(os.access(SMOKE, os.X_OK))
        self.assertEqual(smoke_luks_hotplug_script(), SMOKE)

    def test_wrap_logs_argv_then_execs_real_binary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            real_body = tmp / "seen"
            strata = bin_dir / "strata"
            strata.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SEEN\"\n",
                encoding="utf-8",
            )
            strata.chmod(0o755)
            env = {
                **os.environ,
                "HOME": str(tmp),
                "SMOKE_LUKS_CASE": "wrap",
                "SMOKE_STRATA_BIN": str(strata),
                "SMOKE_HOOK_LOG": str(tmp / "hook.log"),
                "SEEN": str(real_body),
            }
            wrap = subprocess.run(
                ["bash", str(SMOKE)],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(wrap.returncode, 0, wrap.stderr)
            self.assertIn("WRAPPED=1", wrap.stdout)
            logged = subprocess.run(
                [str(strata), "--udiskie-hook", "device_added", "crypto", "/dev/vdc", "uuid"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(logged.returncode, 0, logged.stderr)
            hook = (tmp / "hook.log").read_text(encoding="utf-8")
            self.assertIn("--udiskie-hook", hook)
            self.assertIn("/dev/vdc", hook)
            self.assertTrue((bin_dir / "strata.real").is_file())
            self.assertIn("--udiskie-hook", real_body.read_text(encoding="utf-8"))

    def test_wait_prints_called_when_device_and_log_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bindir = tmp / "bin"
            bindir.mkdir()
            log_path = tmp / "hook.log"
            log_path.write_text(
                "--udiskie-hook device_added crypto /dev/vdc deadbeef\n",
                encoding="utf-8",
            )
            _write_exec(
                bindir / "lsblk",
                "#!/bin/sh\nprintf 'vdc crypto_LUKS strata-luks\\n'\n",
            )
            env = {
                **os.environ,
                "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                "SMOKE_LUKS_CASE": "wait",
                "SMOKE_HOOK_LOG": str(log_path),
                "SMOKE_SLEEP": "0",
                "SMOKE_DEVICE_TIMEOUT_S": "2",
                "SMOKE_HOOK_TIMEOUT_S": "2",
            }
            proc = subprocess.run(
                ["bash", str(SMOKE)],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("STRATA_CALLED=1", proc.stdout)
        self.assertIn("LUKS_DEV=vdc", proc.stdout)
        self.assertIn("--udiskie-hook", proc.stdout)

    def test_wait_fails_without_hook_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bindir = tmp / "bin"
            bindir.mkdir()
            _write_exec(
                bindir / "lsblk",
                "#!/bin/sh\nprintf 'vdc crypto_LUKS strata-luks\\n'\n",
            )
            _write_exec(bindir / "pgrep", "#!/bin/sh\nexit 1\n")
            env = {
                **os.environ,
                "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                "SMOKE_LUKS_CASE": "wait",
                "SMOKE_HOOK_LOG": str(tmp / "missing.log"),
                "SMOKE_SLEEP": "0",
                "SMOKE_DEVICE_TIMEOUT_S": "1",
                "SMOKE_HOOK_TIMEOUT_S": "1",
            }
            proc = subprocess.run(
                ["bash", str(SMOKE)],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Strata was not called", proc.stderr)


class LuksHotplugStepsTests(unittest.TestCase):
    def test_omarchy4_records_steps_and_hotplug(self) -> None:
        fake = _SpecFakeRun()
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        plugged: list[Path] = []

        def hotplug(path: Path) -> bool:
            plugged.append(path)
            return True

        def make_image(path: Path) -> Path:
            path.write_bytes(b"LUKS")
            return path

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            binary = tmp / "strata"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            steps, extras = run_luks_hotplug_steps(
                machine,
                guest=load_guest("omarchy-4"),
                local_path=binary,
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                create_luks_fn=make_image,
                hotplug_fn=hotplug,
            )
        self.assertEqual([s["name"] for s in steps], list(LUKS_HOTPLUG_STEPS))
        blob = "\n".join(commands)
        self.assertIn("--install-udiskie-unlock", blob)
        self.assertIn("SMOKE_LUKS_CASE=wrap", blob)
        self.assertIn("SMOKE_LUKS_CASE=wait", blob)
        self.assertIn("udiskie", blob)
        self.assertEqual(len(plugged), 1)
        self.assertEqual(extras["luks_device"], "vdc")
        self.assertIn("--udiskie-hook", extras["luks_hook"])

    def test_ubuntu_is_not_supported(self) -> None:
        fake = _SpecFakeRun()
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
                run_luks_hotplug_steps(
                    machine,
                    guest=load_guest("ubuntu-2404"),
                    local_path=tmp / "strata",
                    screenshot_dest=tmp / "screenshot.png",
                    session_timeout=5,
                    run=fake,
                    sleep=lambda _s: None,
                    create_luks_fn=lambda p: p,
                    hotplug_fn=lambda _p: True,
                )
        self.assertEqual(str(ctx.exception), LUKS_HOTPLUG_FAIL_CLOSED)

    def test_missing_hook_config_fails(self) -> None:
        fake = _SpecFakeRun()
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"

        def run(argv, **kwargs):
            remote = str(argv[-1]) if argv else ""
            if "udiskie/config.yml" in remote:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return fake(argv, **kwargs)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            binary = tmp / "strata"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            with self.assertRaises(SessionSmokeError) as ctx:
                run_luks_hotplug_steps(
                    machine,
                    guest=load_guest("arch"),
                    local_path=binary,
                    screenshot_dest=tmp / "screenshot.png",
                    session_timeout=5,
                    run=run,
                    sleep=lambda _s: None,
                    create_luks_fn=lambda p: p,
                    hotplug_fn=lambda _p: True,
                )
        self.assertEqual(str(ctx.exception), LUKS_HOTPLUG_HOOK_MISSING)

    def test_helpers(self) -> None:
        self.assertTrue(supports_luks_hotplug("arch"))
        self.assertTrue(supports_luks_hotplug(load_guest("omarchy-3")))
        self.assertFalse(supports_luks_hotplug("ubuntu-2404"))
        remote = luks_hotplug_command(case="wait", bin_path="/home/tester/.local/bin/strata")
        self.assertIn("SMOKE_LUKS_CASE=wait", remote)
        self.assertIn("bash /tmp/smoke-luks-hotplug.sh", remote)


class LuksHotplugRunTestTests(unittest.TestCase):
    def test_uploads_smoke_without_qemu_system(self) -> None:
        fake = _FakeRun()
        recorded_argv: list[list[str]] = []
        plugged: list[Path] = []

        def make_image(path: Path) -> Path:
            path.write_bytes(b"LUKS")
            return path

        def hotplug(path: Path) -> bool:
            plugged.append(path)
            return True

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
            binary = tmp / "strata"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
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
                    luks_hotplug=binary,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                    create_luks_fn=make_image,
                    hotplug_fn=hotplug,
                )
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn("luks-hotplug ok", buf.getvalue())
            blob = " ".join(str(c) for c in fake.calls)
            self.assertIn("smoke-luks-hotplug.sh", blob)
            self.assertIn("--install-udiskie-unlock", blob)
            result_files = list((cache / "runs").glob("*/result.json"))
            self.assertTrue(result_files)
            text = result_files[0].read_text(encoding="utf-8")
            for name in LUKS_HOTPLUG_STEPS:
                self.assertIn(f'"name": "{name}"', text)
            self.assertTrue(plugged)
            del golden

    def test_rejects_other_guests(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / "strata"
            binary.write_text("x", encoding="utf-8")
            err = io.StringIO()
            with (
                redirect_stderr(err),
                patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
                patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
            ):
                code = main(
                    ["run-test", "--", "ubuntu-2404", "--luks-hotplug", str(binary)]
                )
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn(LUKS_HOTPLUG_FAIL_CLOSED, err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()

    def test_missing_path_fail_closed_no_qemu(self) -> None:
        err = io.StringIO()
        with (
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(["run-test", "--", "omarchy-4", "--luks-hotplug"])
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn(LUKS_HOTPLUG_MISSING_PATH, err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()

    def test_exclusive_with_udiskie_unlock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / "strata"
            binary.write_text("x", encoding="utf-8")
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
                        "--luks-hotplug",
                        str(binary),
                        "--udiskie-unlock",
                    ]
                )
        self.assertEqual(code, 2, err.getvalue())
        self.assertTrue(
            LUKS_HOTPLUG_EXCLUSIVE in err.getvalue()
            or "cannot be combined" in err.getvalue()
        )
        popen.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
