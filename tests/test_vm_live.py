"""vm-live: tagged or local Strata plus sample fixtures. No KVM."""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from strataqemu import config
from strataqemu.qemu import Machine
from strataqemu.sample_tree import (
    build_fixtures_archive,
    pack_sample_tree,
    png_1x1,
    write_sample_tree,
)
from strataqemu.tests_spec import (
    APPARMOR_BWRAP_RESTRICT_UNSUPPORTED,
    missing_golden_message,
)
from strataqemu.vm_live import (
    ARCH_RUNTIME_PACKAGES,
    VM_LIVE_LOCAL_EMPTY,
    VM_LIVE_LOCAL_MISSING,
    VM_LIVE_SOURCE_REQUIRED,
    VM_LIVE_TAG_BAD,
    ensure_runtime_deps_command,
    guest_fixtures_dir,
    hyprland_exec_lua,
    launch_strata_at_command,
    parse_from_tag,
    resolve_local_strata,
    run_vm_live,
)

from tests.test_run_test import (
    _DummyProc,
    _FakeRun,
    _after,
    _cache_with_golden,
    _host_ok,
    _refuse_qemu_system,
)


def _popen_recorder(recorded: list[list[str]]):
    def popen(argv, **kwargs):
        name = Path(str(argv[0])).name if argv else ""
        if name.startswith("qemu-system"):
            recorded.append(list(argv))
            return _DummyProc()
        raise AssertionError(f"unexpected Popen: {argv}")

    return popen


def _fake_overlay(golden, overlay, **kwargs):
    dest = Path(overlay)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"overlay")
    return dest


class SampleTreeTests(unittest.TestCase):
    def test_write_sample_tree_has_varied_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = write_sample_tree(Path(td) / "fixtures")
            self.assertTrue((root / "readme.md").is_file())
            self.assertTrue((root / "documents" / "notes.txt").is_file())
            self.assertTrue((root / "documents" / "spreadsheet.csv").is_file())
            self.assertTrue((root / "pictures" / "pixel.png").is_file())
            png = (root / "pictures" / "pixel.png").read_bytes()
            self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(png, png_1x1())
            zpath = root / "archive" / "sample.zip"
            with zipfile.ZipFile(zpath) as zf:
                self.assertIn("hello.txt", zf.namelist())
            self.assertTrue((root / "nested" / "deep" / "file.txt").is_file())
            self.assertTrue((root / "empty").is_dir())
            self.assertTrue((root / ".hidden.txt").is_file())
            self.assertTrue((root / "file with spaces.txt").is_file())
            self.assertTrue((root / "link-to-readme.md").is_symlink())
            self.assertEqual(os.readlink(root / "link-to-readme.md"), "readme.md")
            self.assertTrue((root / "broken-link").is_symlink())
            self.assertTrue((root / "script.sh").stat().st_mode & 0o111)

    def test_pack_and_extract_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            archive = build_fixtures_archive(tmp / "fixtures.tar.gz")
            self.assertTrue(archive.is_file())
            dest = tmp / "out"
            dest.mkdir()
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(dest)
            self.assertTrue((dest / "readme.md").is_file())
            self.assertTrue((dest / "pictures" / "pixel.png").is_file())
            self.assertTrue((dest / "empty").is_dir())

    def test_pack_sample_tree_preserves_empty_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            root = write_sample_tree(tmp / "tree")
            archive = pack_sample_tree(root, tmp / "t.tar.gz")
            names: list[str] = []
            with tarfile.open(archive, "r:gz") as tar:
                names = [m.name.rstrip("/") for m in tar.getmembers()]
            self.assertIn("empty", names)
            self.assertIn(".hidden.txt", names)


class LaunchCommandTests(unittest.TestCase):
    def test_hyprland_exec_lua_quotes_path_as_string(self) -> None:
        lua = hyprland_exec_lua(
            "/home/tester/.local/bin/strata /home/tester/fixtures"
        )
        self.assertEqual(
            lua,
            'hl.dsp.exec_cmd("/home/tester/.local/bin/strata /home/tester/fixtures")',
        )

    def test_hyprland_uses_lua_exec_cmd_not_legacy_dispatch_exec(self) -> None:
        cmd = launch_strata_at_command(
            {
                "XDG_RUNTIME_DIR": "/run/user/1000",
                "HYPRLAND_INSTANCE_SIGNATURE": "sig",
            },
            "/home/tester/fixtures",
            compositor="Hyprland",
            user="tester",
        )
        self.assertIn("hl.dsp.exec_cmd", cmd)
        self.assertIn("hyprctl dispatch", cmd)
        self.assertIn("/home/tester/.local/bin/strata", cmd)
        self.assertIn("/home/tester/fixtures", cmd)
        self.assertNotIn("bash -lc", cmd)
        # Legacy `dispatch exec /path` is invalid Lua on 0.55+ (`.` in `.local`).
        self.assertNotRegex(cmd, r"hyprctl dispatch exec ['\"]/")

    def test_gnome_uses_nohup_not_hyprctl(self) -> None:
        cmd = launch_strata_at_command(
            {"WAYLAND_DISPLAY": "wayland-0"},
            "/home/tester/fixtures",
            compositor="gnome-shell",
            user="tester",
        )
        self.assertIn("nohup", cmd)
        self.assertIn("bash -c", cmd)
        self.assertNotIn("bash -lc", cmd)
        self.assertNotIn("hyprctl", cmd)
        self.assertIn("/home/tester/.local/bin/strata", cmd)

    def test_runtime_deps_install_gtk4_via_pacman(self) -> None:
        cmd = ensure_runtime_deps_command()
        self.assertIn("pacman -Q gtk4", cmd)
        self.assertIn("pacman -S", cmd)
        self.assertIn("gtk4", cmd)
        for pkg in ARCH_RUNTIME_PACKAGES:
            with self.subTest(pkg=pkg):
                self.assertIn(pkg, cmd)


class ResolveLocalStrataTests(unittest.TestCase):
    def test_missing_path(self) -> None:
        with self.assertRaises(Exception) as ctx:
            resolve_local_strata("/no/such/strata-binary")
        self.assertIn(VM_LIVE_LOCAL_MISSING, str(ctx.exception))

    def test_binary_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / "strata"
            binary.write_bytes(b"\x7fELF")
            local = resolve_local_strata(binary)
        self.assertEqual(local.kind, "binary")
        self.assertEqual(local.path, binary.resolve())

    def test_archive_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "strata-0.15.0-x86_64-unknown-linux-gnu.tar.gz"
            archive.write_bytes(b"tar")
            local = resolve_local_strata(archive)
        self.assertEqual(local.kind, "archive")

    def test_checkout_release_binary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / "target" / "release" / "strata"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"\x7fELF")
            local = resolve_local_strata(td)
        self.assertEqual(local.kind, "binary")
        self.assertEqual(local.path, binary.resolve())

    def test_directory_named_strata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / "strata"
            binary.write_bytes(b"\x7fELF")
            local = resolve_local_strata(td)
        self.assertEqual(local.kind, "binary")

    def test_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(Exception) as ctx:
                resolve_local_strata(td)
        self.assertIn(VM_LIVE_LOCAL_EMPTY, str(ctx.exception))

    def test_parse_from_tag_strips_v(self) -> None:
        self.assertEqual(parse_from_tag("v0.15.0"), "0.15.0")
        self.assertEqual(parse_from_tag("0.14.0"), "0.14.0")

    def test_parse_from_tag_rejects_junk(self) -> None:
        with self.assertRaises(Exception) as ctx:
            parse_from_tag("latest")
        self.assertIn(VM_LIVE_TAG_BAD, str(ctx.exception))


class VmLiveWiringTests(unittest.TestCase):
    def test_missing_source_is_usage_error(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = run_vm_live("ubuntu-2404")
        self.assertEqual(code, 2)
        self.assertIn(VM_LIVE_SOURCE_REQUIRED, err.getvalue())

    def test_missing_golden_does_not_spawn_qemu(self) -> None:
        msg = missing_golden_message("ubuntu-2404")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "empty-cache"
            cache.mkdir()
            err = io.StringIO()
            with (
                redirect_stderr(err),
                redirect_stdout(io.StringIO()),
                patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
                patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
            ):
                code = run_vm_live(
                    "ubuntu-2404",
                    from_tag="0.15.0",
                    cache_dir=cache,
                )
        self.assertEqual(code, 1)
        self.assertIn(msg, err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()

    def test_missing_local_path_before_check_host(self) -> None:
        err = io.StringIO()
        with (
            redirect_stderr(err),
            redirect_stdout(io.StringIO()),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
        ):
            code = run_vm_live(
                "ubuntu-2404",
                from_local="/no/such/strata",
            )
        self.assertEqual(code, 2)
        self.assertIn(VM_LIVE_LOCAL_MISSING, err.getvalue())
        popen.assert_not_called()

    def test_bad_tag_is_usage_error(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = run_vm_live("ubuntu-2404", from_tag="not-a-version")
        self.assertEqual(code, 2)
        self.assertIn(VM_LIVE_TAG_BAD, err.getvalue())

    def test_from_tag_graphical_installs_and_seeds_fixtures(self) -> None:
        fake = _FakeRun()
        recorded: list[list[str]] = []
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
                code = run_vm_live(
                    "ubuntu-2404",
                    from_tag="0.15.0",
                    graphical=True,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=_popen_recorder(recorded),
                    create_overlay_fn=_fake_overlay,
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
            blob = " ".join(str(c) for c in fake.calls)
            self.assertIn("smoke-session.sh", blob)
            self.assertIn("smoke-update.sh", blob)
            self.assertIn("SMOKE_UPDATE_PHASE=previous", blob)
            self.assertIn("UPDATE_FROM_VERSION=0.15.0", blob)
            self.assertIn("/tmp/strata-fixtures.tar.gz", blob)
            self.assertIn("/home/tester/fixtures", blob)
            self.assertIn(".local/bin/strata", blob)
            self.assertIn("pacman -Q gtk4", blob)
            self.assertNotIn("hyprctl dispatch exec", blob)
            out = buf.getvalue()
            self.assertIn("vm-live: ubuntu-2404 ssh_port=", out)
            self.assertIn("tag 0.15.0", out)
            self.assertIn(guest_fixtures_dir("tester"), out)
            runs = list((cache / "runs").iterdir())
            self.assertEqual(len(runs), 1)
            self.assertTrue((runs[0] / "fixtures.tar.gz").is_file())
            self.assertTrue(runs[0].name.endswith("-ubuntu-2404-live") or "-live" in runs[0].name)

    def test_arch_from_tag_installs_gtk_and_launches_via_hyprctl(self) -> None:
        fake = _FakeRun()
        recorded: list[list[str]] = []
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, _golden = _cache_with_golden(tmp, "arch")
            host = _host_ok(tmp)
            err = io.StringIO()
            with (
                redirect_stdout(io.StringIO()),
                redirect_stderr(err),
                patch.object(Machine, "shutdown", return_value="kill"),
            ):
                code = run_vm_live(
                    "arch",
                    from_tag="0.15.0",
                    graphical=True,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=_popen_recorder(recorded),
                    create_overlay_fn=_fake_overlay,
                    settle_s=0,
                    graphical_ui="gtk",
                    wait=False,
                )
            self.assertEqual(code, 0, err.getvalue())
            blob = " ".join(str(c) for c in fake.calls)
            self.assertIn("pacman -S", blob)
            self.assertIn("gtk4", blob)
            self.assertIn("hl.dsp.exec_cmd", blob)
            self.assertIn("hyprctl dispatch", blob)
            self.assertIn("/home/tester/.local/bin/strata", blob)
            self.assertIn("/home/tester/fixtures", blob)
            self.assertNotIn("bash -lc", blob)

    def test_from_local_binary_does_not_use_update_script(self) -> None:
        fake = _FakeRun()
        recorded: list[list[str]] = []
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            binary = tmp / "strata"
            binary.write_bytes(b"\x7fELF")
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
                code = run_vm_live(
                    "ubuntu-2404",
                    from_local=binary,
                    graphical=True,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=_popen_recorder(recorded),
                    create_overlay_fn=_fake_overlay,
                    settle_s=0,
                    graphical_ui="gtk",
                    wait=False,
                )
            self.assertEqual(code, 0, err.getvalue())
            blob = " ".join(str(c) for c in fake.calls)
            self.assertNotIn("smoke-update.sh", blob)
            self.assertIn("/tmp/strata-bin", blob)
            self.assertIn("install -Dm755", blob)
            self.assertIn("io.github.lgse.Strata.desktop", blob)
            self.assertIn("local ", buf.getvalue())
            runs = list((cache / "runs").iterdir())
            desktop = runs[0] / "io.github.lgse.Strata.desktop"
            self.assertTrue(desktop.is_file())
            self.assertIn("/home/tester/.local/bin/strata", desktop.read_text(encoding="utf-8"))

    def test_from_local_archive_uploads_tarball(self) -> None:
        fake = _FakeRun()
        recorded: list[list[str]] = []
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            archive = tmp / "strata-0.14.0-x86_64-unknown-linux-gnu.tar.gz"
            archive.write_bytes(b"tar")
            cache, _golden = _cache_with_golden(tmp)
            vars_template = tmp / "OVMF_VARS.4m.fd"
            vars_template.write_bytes(b"vars")
            host = _host_ok(tmp)
            err = io.StringIO()
            with (
                redirect_stdout(io.StringIO()),
                redirect_stderr(err),
                patch(
                    "strataqemu.run_test.find_ovmf_vars",
                    return_value=vars_template,
                ),
                patch.object(Machine, "shutdown", return_value="kill"),
            ):
                code = run_vm_live(
                    "ubuntu-2404",
                    from_local=archive,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=_popen_recorder(recorded),
                    create_overlay_fn=_fake_overlay,
                    settle_s=0,
                    graphical_ui="gtk",
                    wait=False,
                )
            self.assertEqual(code, 0, err.getvalue())
            blob = " ".join(str(c) for c in fake.calls)
            self.assertIn("smoke-update.sh", blob)
            self.assertIn("/tmp/strata-archive.tar.gz", blob)
            self.assertIn("UPDATE_FROM_ARCHIVE=", blob)

    def test_headless_uses_egl_headless(self) -> None:
        fake = _FakeRun()
        recorded: list[list[str]] = []
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, _golden = _cache_with_golden(tmp)
            vars_template = tmp / "OVMF_VARS.4m.fd"
            vars_template.write_bytes(b"vars")
            host = _host_ok(tmp)
            err = io.StringIO()
            with (
                redirect_stdout(io.StringIO()),
                redirect_stderr(err),
                patch(
                    "strataqemu.run_test.find_ovmf_vars",
                    return_value=vars_template,
                ),
                patch.object(Machine, "shutdown", return_value="kill"),
            ):
                code = run_vm_live(
                    "ubuntu-2404",
                    from_tag="0.15.0",
                    graphical=False,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=_popen_recorder(recorded),
                    create_overlay_fn=_fake_overlay,
                    settle_s=0,
                    wait=False,
                )
            self.assertEqual(code, 0, err.getvalue())
            argv = recorded[0]
            self.assertIn("virtio-gpu-gl-pci", argv)
            self.assertEqual(_after(argv, "-display"), "egl-headless,gl=on")
            self.assertIn("-vnc", argv)

    def test_success_without_keep_deletes_run_dir(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, _golden = _cache_with_golden(tmp)
            vars_template = tmp / "OVMF_VARS.4m.fd"
            vars_template.write_bytes(b"vars")
            host = _host_ok(tmp)
            err = io.StringIO()
            with (
                redirect_stdout(io.StringIO()),
                redirect_stderr(err),
                patch(
                    "strataqemu.run_test.find_ovmf_vars",
                    return_value=vars_template,
                ),
                patch.object(Machine, "shutdown", return_value="kill"),
            ):
                code = run_vm_live(
                    "ubuntu-2404",
                    from_tag="0.15.0",
                    keep=False,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=_popen_recorder([]),
                    create_overlay_fn=_fake_overlay,
                    settle_s=0,
                    graphical_ui="gtk",
                    wait=False,
                )
            self.assertEqual(code, 0, err.getvalue())
            runs = cache / "runs"
            self.assertTrue(runs.is_dir())
            self.assertEqual(list(runs.iterdir()), [])


class VmLiveSessionPrepTests(unittest.TestCase):
    def _run(
        self,
        fake: _FakeRun,
        *,
        guest_id: str = "ubuntu-2404",
        apparmor: bool = False,
        run=None,
    ):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, _golden = _cache_with_golden(tmp, guest_id)
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
                code = run_vm_live(
                    guest_id,
                    from_tag="0.15.0",
                    graphical=False,
                    keep=False,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=run or fake,
                    popen=_popen_recorder([]),
                    create_overlay_fn=_fake_overlay,
                    settle_s=0,
                    wait=False,
                    apparmor_bwrap_restrict=apparmor,
                )
        remotes = [str(argv[-1]) for argv in fake.calls]
        return code, buf.getvalue(), err.getvalue(), remotes

    @staticmethod
    def _index(remotes: list[str], needle: str) -> int:
        return next(i for i, r in enumerate(remotes) if needle in r)

    def test_apparmor_restriction_is_enforced_before_strata_launches(self) -> None:
        code, out, err, remotes = self._run(_FakeRun(), apparmor=True)
        self.assertEqual(code, 0, err)
        apparmor = self._index(remotes, "bash /tmp/smoke-apparmor-bwrap.sh")
        self.assertLess(apparmor, self._index(remotes, "smoke-update.sh"))
        self.assertLess(apparmor, self._index(remotes, "nohup"))
        self.assertIn("AppArmor bwrap-userns-restrict enforced", out)
        self.assertIn("Operation not permitted", out)

    def test_apparmor_flag_rejected_on_non_ubuntu_guest(self) -> None:
        err = io.StringIO()
        with (
            redirect_stderr(err),
            redirect_stdout(io.StringIO()),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
        ):
            code = run_vm_live(
                "arch", from_tag="0.15.0", apparmor_bwrap_restrict=True
            )
        self.assertEqual(code, 2)
        self.assertIn(APPARMOR_BWRAP_RESTRICT_UNSUPPORTED, err.getvalue())
        popen.assert_not_called()

    def test_without_flag_no_apparmor_changes(self) -> None:
        code, _out, err, remotes = self._run(_FakeRun())
        self.assertEqual(code, 0, err)
        self.assertFalse(any("apparmor" in r for r in remotes))

    def test_gnome_idle_lock_is_disabled_before_strata_launches(self) -> None:
        code, _out, err, remotes = self._run(_FakeRun())
        self.assertEqual(code, 0, err)
        idle = self._index(remotes, "idle-delay")
        command = remotes[idle]
        self.assertIn("gsettings set org.gnome.desktop.session idle-delay 0", command)
        self.assertIn("org.gnome.desktop.screensaver lock-enabled false", command)
        self.assertIn("loginctl unlock-sessions", command)
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=", command)
        self.assertLess(idle, self._index(remotes, "nohup"))

    def test_idle_lock_failure_warns_but_keeps_session(self) -> None:
        fake = _FakeRun()

        def run(argv, **kwargs):
            if "gsettings" in str(argv[-1]):
                fake.calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 1, "", "no schema\n")
            return fake(argv, **kwargs)

        code, _out, err, _remotes = self._run(fake, run=run)
        self.assertEqual(code, 0, err)
        self.assertIn("could not disable GNOME idle lock: no schema", err)

    def test_hyprland_guest_skips_gnome_idle_settings(self) -> None:
        code, _out, err, remotes = self._run(_FakeRun(), guest_id="arch")
        self.assertEqual(code, 0, err)
        self.assertFalse(any("gsettings" in r for r in remotes))
