"""Host-side unit tests for the shipped CLI and check-host.

No KVM, no real QEMU process. Probes are injected.
"""

from __future__ import annotations

import io
import os
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from strataqemu import config
from strataqemu.cli import (
    GENERIC_MEM_FLOOR_MIB,
    REQUIRED_BINARIES,
    CheckHostEnv,
    build_parser,
    check_host,
    find_ovmf_code,
    main,
    ovmf_code_candidates,
    parse_install_from,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ARGPARSE_PROXY_TASKS = (
    "check-host",
    "image-build",
    "run-test",
    "vm-run",
    "vm-live",
    "image-prune",
    "spike-wayland-ubuntu",
)
QEMU_TASKS = (
    "image-build",
    "run-test",
    "vm-run",
    "vm-live",
    "spike-wayland-ubuntu",
)
SUBCOMMANDS = (
    "check-host",
    "image-build",
    "run-test",
    "vm-run",
    "vm-live",
    "image-prune",
    "spike-wayland-ubuntu",
)
# Hidden spike is a mise/python -m command, not a scripts/ shim.
SHIM_COMMANDS = (
    "check-host",
    "image-build",
    "run-test",
    "vm-run",
    "vm-live",
    "image-prune",
)


def _write_exec(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _bindir_with(tmp: Path, names: tuple[str, ...]) -> Path:
    bindir = tmp / "bin"
    for name in names:
        _write_exec(bindir, name)
    return bindir


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fw")
    return path


def _success_env(
    tmp: Path,
    *,
    binaries: tuple[str, ...] | None = None,
    virgl_ok: bool = True,
    mem: int | None = None,
    euid: int = 1000,
    include_ovmf: bool = True,
    kvm_exists: bool = True,
    kvm_accessible: bool = True,
) -> CheckHostEnv:
    names = binaries if binaries is not None else REQUIRED_BINARIES
    bindir = _bindir_with(tmp, names)
    kvm = tmp / "dev" / "kvm"
    if kvm_exists:
        kvm.parent.mkdir(parents=True, exist_ok=True)
        kvm.write_bytes(b"")
        kvm.chmod(0o666)
    share = tmp / "usr" / "share"
    if include_ovmf:
        _touch(share / "edk2" / "x64" / "OVMF_CODE.4m.fd")
    cache = tmp / "cache"
    cache.mkdir()
    return CheckHostEnv(
        path=str(bindir),
        kvm_path=kvm,
        kvm_accessible=lambda _p: kvm_accessible,
        cache_dir=cache,
        firmware_share_roots=(share,),
        virgl_ok=virgl_ok,
        mem_available_mib=GENERIC_MEM_FLOOR_MIB if mem is None else mem,
        euid=euid,
    )


class CliHelpTests(unittest.TestCase):
    def test_help_names_subcommands(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--help"])
        self.assertEqual(code, 0)
        text = buf.getvalue()
        for name in SUBCOMMANDS:
            with self.subTest(name=name):
                self.assertIn(name, text)

    def test_parser_accepts_each_subcommand(self) -> None:
        parser = build_parser()
        for name in SUBCOMMANDS:
            with self.subTest(name=name):
                args = parser.parse_args([name])
                self.assertEqual(args.command, name)

    def test_run_test_and_vm_run_help_are_not_stubs(self) -> None:
        for name, flag in (("run-test", "--session-only"), ("vm-run", "--graphical")):
            buf = io.StringIO()
            err = io.StringIO()
            with self.subTest(name=name), redirect_stdout(buf), redirect_stderr(err):
                code = main([name, "--help"])
            self.assertEqual(code, 0)
            text = buf.getvalue() + err.getvalue()
            self.assertIn(name, text)
            self.assertIn(flag, text)
            self.assertNotIn("not implemented", text)
            self.assertNotIn("SystemExit", text)

    def test_vm_live_help_names_sources(self) -> None:
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
        self.assertIn("fixtures", text.lower())
        self.assertNotIn("not implemented", text)

    def test_vm_live_from_tag_binds_version(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["vm-live", "ubuntu-2404", "--from-tag", "0.15.0"]
        )
        self.assertEqual(args.command, "vm-live")
        self.assertEqual(args.from_tag, "0.15.0")
        self.assertIsNone(args.from_local)
        self.assertFalse(args.headless)

    def test_vm_live_from_local_binds_path(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["vm-live", "arch", "--from-local", "/tmp/strata"]
        )
        self.assertEqual(args.from_local, "/tmp/strata")
        self.assertIsNone(args.from_tag)

    def test_vm_live_from_tag_and_from_local_rejected(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(
                [
                    "vm-live",
                    "ubuntu-2404",
                    "--from-tag",
                    "0.15.0",
                    "--from-local",
                    "/tmp/strata",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("not allowed", err.getvalue().lower())

    def test_vm_live_graphical_and_headless_rejected(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(
                [
                    "vm-live",
                    "ubuntu-2404",
                    "--from-tag",
                    "0.15.0",
                    "--graphical",
                    "--headless",
                ]
            )
        self.assertEqual(code, 2)
        text = err.getvalue()
        self.assertIn("--graphical", text)
        self.assertIn("--headless", text)

    def test_vm_live_without_guest_is_not_stub(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["vm-live", "--from-tag", "0.15.0"])
        self.assertEqual(code, 2)
        text = buf.getvalue() + err.getvalue()
        self.assertNotIn("not implemented", text)
        self.assertIn("guest", text.lower())

    def test_run_test_help_includes_omarchy_bindings(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["run-test", "--help"])
        self.assertEqual(code, 0)
        text = buf.getvalue() + err.getvalue()
        self.assertIn("--omarchy-bindings", text)

    def test_run_test_help_includes_udiskie_unlock(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["run-test", "--help"])
        self.assertEqual(code, 0)
        text = buf.getvalue() + err.getvalue()
        self.assertIn("--udiskie-unlock", text)
        self.assertIn("STRATA_QEMU_INSTALL_SH", text)

    def test_run_test_help_includes_luks_hotplug(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["run-test", "--help"])
        self.assertEqual(code, 0)
        text = buf.getvalue() + err.getvalue()
        self.assertIn("--luks-hotplug", text)
        self.assertIn("PATH", text)

    def test_luks_hotplug_binds_path(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["run-test", "omarchy-4", "--luks-hotplug", "/tmp/strata"]
        )
        self.assertEqual(args.luks_hotplug, "/tmp/strata")
        self.assertFalse(args.udiskie_unlock)

    def test_run_test_help_includes_update_from(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["run-test", "--help"])
        self.assertEqual(code, 0)
        text = buf.getvalue() + err.getvalue()
        self.assertIn("--update-from", text)
        self.assertIn("STRATA_QEMU_UPDATE_FROM", text)

    def test_update_from_binds_version(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["run-test", "ubuntu-2404", "--update-from", "0.15.0"]
        )
        self.assertEqual(args.update_from, "0.15.0")
        self.assertFalse(args.session_only)
        self.assertIsNone(args.install_from)

    def test_install_from_local_archive_binds_path(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "run-test",
                "ubuntu-2404",
                "--install-from",
                "local-archive",
                "/tmp/strata-0.9.0-x86_64-unknown-linux-gnu.tar.gz",
            ]
        )
        source, path, err = parse_install_from(args.install_from)
        self.assertIsNone(err)
        self.assertEqual(source, "local-archive")
        self.assertEqual(
            path, "/tmp/strata-0.9.0-x86_64-unknown-linux-gnu.tar.gz"
        )
        source, path, err = parse_install_from(["local-archive"])
        self.assertIsNone(err)
        self.assertEqual(source, "local-archive")
        self.assertIsNone(path)
        source, path, err = parse_install_from(["release"])
        self.assertEqual((source, path, err), ("release", None, None))

    def test_run_test_without_guest_is_not_stub(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["run-test", "--session-only"])
        self.assertEqual(code, 2)
        text = buf.getvalue() + err.getvalue()
        self.assertNotIn("not implemented", text)
        self.assertIn("guest", text.lower())

    def test_image_build_help_is_not_exit2_stub(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["image-build", "--help"])
        self.assertEqual(code, 0)
        text = buf.getvalue() + err.getvalue()
        self.assertIn("image-build", text)
        self.assertIn("--force", text)
        self.assertIn("guest", text.lower())
        self.assertNotIn("not implemented", text)
        self.assertNotIn("SystemExit", text)

    def test_image_build_without_guest_is_not_stub(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["image-build"])
        self.assertEqual(code, 2)
        text = buf.getvalue() + err.getvalue()
        self.assertNotIn("not implemented", text)
        self.assertIn("guest", text.lower())

    def test_spike_wayland_ubuntu_help_is_not_exit2_stub(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["spike-wayland-ubuntu", "--help"])
        self.assertEqual(code, 0)
        text = buf.getvalue() + err.getvalue()
        self.assertIn("spike-wayland-ubuntu", text)
        self.assertIn("wayland", text.lower())
        self.assertNotIn("not implemented", text)
        self.assertNotIn("SystemExit", text)

    def test_image_prune_is_not_a_stub(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = td
            err = io.StringIO()
            try:
                with redirect_stderr(err):
                    code = main(["image-prune"])
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(code, 0)
        self.assertNotIn("not implemented", err.getvalue())


class MiseTomlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = tomllib.loads(
            (REPO_ROOT / "mise.toml").read_text(encoding="utf-8")
        )

    def test_python_pin(self) -> None:
        self.assertEqual(self.data["tools"]["python"], "3.11")

    def test_min_version_is_bootstrap_floor_not_strata(self) -> None:
        min_version = self.data["min_version"]
        self.assertIsInstance(min_version, str)
        self.assertNotEqual(min_version, "2026.9.0")
        self.assertTrue(min_version)

    def test_run_test_depends_is_check_host_only(self) -> None:
        depends = self.data["tasks"]["run-test"]["depends"]
        self.assertEqual(depends, ["check-host"])

    def test_test_task_is_unittest_discover_without_check_host(self) -> None:
        task = self.data["tasks"]["test"]
        self.assertEqual(
            task["run"],
            "python -m unittest discover -s tests -t . -v",
        )
        self.assertNotIn("depends", task)

    def test_argparse_proxy_tasks_have_raw_args(self) -> None:
        for name in ARGPARSE_PROXY_TASKS:
            with self.subTest(name=name):
                self.assertIs(
                    self.data["tasks"][name]["raw_args"],
                    True,
                )

    def test_qemu_tasks_are_interactive(self) -> None:
        for name in QEMU_TASKS:
            with self.subTest(name=name):
                self.assertIs(
                    self.data["tasks"][name]["interactive"],
                    True,
                )

    def test_spike_wayland_ubuntu_is_hidden_argparse_proxy(self) -> None:
        task = self.data["tasks"]["spike-wayland-ubuntu"]
        self.assertIs(task["hide"], True)
        self.assertIs(task["interactive"], True)
        self.assertIs(task["raw_args"], True)
        self.assertEqual(task["depends"], ["check-host"])
        self.assertEqual(
            task["run"],
            "python -m strataqemu spike-wayland-ubuntu",
        )

    def test_bootstrap_gl_package_keys(self) -> None:
        packages = self.data["bootstrap"]["packages"]
        for key in (
            "pacman:qemu-ui-egl-headless",
            "pacman:qemu-hw-display-virtio-gpu-pci-gl",
            "dnf:qemu-device-display-virtio-gpu-pci-gl",
            "apt:qemu-system-gui",
        ):
            with self.subTest(key=key):
                self.assertIn(key, packages)

    def test_bootstrap_omarchy_vfat_package_keys(self) -> None:
        packages = self.data["bootstrap"]["packages"]
        for key in (
            "pacman:dosfstools",
            "apt:dosfstools",
            "dnf:dosfstools",
            "pacman:mtools",
            "apt:mtools",
            "dnf:mtools",
        ):
            with self.subTest(key=key):
                self.assertIn(key, packages)

    def test_lockfile_records_cpython_311_with_checksums(self) -> None:
        lock_path = REPO_ROOT / "mise.lock"
        self.assertTrue(lock_path.is_file(), lock_path)
        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        python_entries = lock["tools"]["python"]
        if isinstance(python_entries, dict):
            python_entries = [python_entries]
        versions = [entry["version"] for entry in python_entries]
        self.assertTrue(
            any(v.startswith("3.11.") for v in versions),
            versions,
        )
        checksums = []
        for entry in python_entries:
            for key, value in entry.items():
                if key.startswith("platforms.") and isinstance(value, dict):
                    checksums.append(value.get("checksum", ""))
        self.assertTrue(checksums)
        self.assertTrue(all(c.startswith("sha256:") for c in checksums), checksums)


class PyprojectTests(unittest.TestCase):
    def test_no_paramiko_or_pexpect(self) -> None:
        data = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = data["project"]
        self.assertNotIn("dependencies", project)
        self.assertNotIn("optional-dependencies", project)

    def test_requires_python_311(self) -> None:
        data = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertIn("3.11", data["project"]["requires-python"])


class ScriptShimTests(unittest.TestCase):
    def test_shims_exec_module_and_do_not_invoke_mise(self) -> None:
        for name in SHIM_COMMANDS:
            path = REPO_ROOT / "scripts" / name
            with self.subTest(name=name):
                self.assertTrue(path.is_file(), path)
                text = path.read_text(encoding="utf-8")
                self.assertIn("-m", text)
                self.assertIn("strataqemu", text)
                self.assertIn(name, text)
                self.assertNotIn("mise", text)


class ConfigTests(unittest.TestCase):
    def test_cache_dir_honors_strata_qemu_cache(self) -> None:
        with self._temp_env("/tmp/strata-qemu-cache-test-xyz") as expected:
            self.assertEqual(config.cache_dir(), Path(expected))

    def test_default_uses_xdg_cache_home(self) -> None:
        xdg = "/tmp/xdg-cache-test-xyz"
        old_cache = os.environ.pop(config.CACHE_ENV, None)
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = xdg
        try:
            self.assertEqual(
                config.cache_dir(),
                Path(xdg) / config.CACHE_DIRNAME,
            )
        finally:
            if old_cache is not None:
                os.environ[config.CACHE_ENV] = old_cache
            os.environ.pop("XDG_CACHE_HOME", None)
            if old_xdg is not None:
                os.environ["XDG_CACHE_HOME"] = old_xdg

    def _temp_env(self, value: str):
        class _Guard:
            def __enter__(self_inner):
                self_inner.old = os.environ.get(config.CACHE_ENV)
                os.environ[config.CACHE_ENV] = value
                return value

            def __exit__(self_inner, *exc):
                if self_inner.old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = self_inner.old

        return _Guard()


class CheckHostTests(unittest.TestCase):
    def test_required_binaries_include_vfat_tools(self) -> None:
        self.assertIn("mkfs.vfat", REQUIRED_BINARIES)
        self.assertIn("mcopy", REQUIRED_BINARIES)
        self.assertIn("xorriso", REQUIRED_BINARIES)

    def test_missing_qemu_names_mise_bootstrap(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            names = tuple(b for b in REQUIRED_BINARIES if b != "qemu-system-x86_64")
            env = _success_env(tmp, binaries=names)
            result = check_host(env)
        self.assertFalse(result.ok)
        joined = "\n".join(result.errors)
        self.assertIn("mise bootstrap", joined)
        self.assertIn("qemu-system-x86_64", joined)
        self.assertNotIn("install qemu", joined.lower().replace("mise bootstrap", ""))

    def test_missing_kvm_hints_group_and_no_tcg(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = _success_env(tmp, kvm_accessible=False)
            result = check_host(env)
        self.assertFalse(result.ok)
        joined = "\n".join(result.errors)
        self.assertIn("kvm", joined.lower())
        self.assertIn("re-login", joined.lower())
        self.assertIn("TCG", joined)

    def test_missing_kvm_node(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = _success_env(tmp, kvm_exists=False)
            result = check_host(env)
        self.assertFalse(result.ok)
        joined = "\n".join(result.errors)
        self.assertIn("does not exist", joined)
        self.assertIn("TCG", joined)

    def test_missing_ovmf(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = _success_env(tmp, include_ovmf=False)
            result = check_host(env)
        self.assertFalse(result.ok)
        joined = "\n".join(result.errors)
        self.assertIn("OVMF", joined)
        self.assertIn("secboot", joined.lower())

    def test_success_generates_key_under_cache(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = _success_env(tmp)
            result = check_host(env)
            key = config.ssh_private_key(env.cache_dir)
            self.assertTrue(result.ok, result.errors)
            self.assertEqual(result.errors, ())
            self.assertTrue(key.is_file(), key)
            self.assertTrue(key.with_name(key.name + ".pub").is_file())
            self.assertEqual(result.ssh_key, key)
            self.assertIsNotNone(result.ovmf_code)
            assert result.ovmf_code is not None
            self.assertTrue(result.ovmf_code.is_file())
            self.assertNotIn("secboot", result.ovmf_code.name.lower())

    def test_keygen_uses_strata_qemu_cache_env(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache = tmp / "from-env"
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            try:
                env = _success_env(tmp)
                env = CheckHostEnv(
                    path=env.path,
                    kvm_path=env.kvm_path,
                    kvm_accessible=env.kvm_accessible,
                    cache_dir=config.cache_dir(),
                    firmware_share_roots=env.firmware_share_roots,
                    virgl_ok=env.virgl_ok,
                    mem_available_mib=env.mem_available_mib,
                    euid=env.euid,
                )
                result = check_host(env)
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
            self.assertTrue(result.ok, result.errors)
            key = cache / "keys" / "id_ed25519"
            self.assertTrue(key.is_file(), key)
            self.assertEqual(result.ssh_key, key)

    def test_success_does_not_overwrite_existing_key(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = _success_env(tmp)
            key = config.ssh_private_key(env.cache_dir)
            key.parent.mkdir(parents=True, exist_ok=True)
            key.write_text("keep-me", encoding="utf-8")
            result = check_host(env)
            self.assertTrue(result.ok, result.errors)
            self.assertEqual(key.read_text(encoding="utf-8"), "keep-me")

    def test_virgl_failure_is_fail_closed(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = _success_env(tmp, virgl_ok=False)
            result = check_host(env)
        self.assertFalse(result.ok)
        joined = "\n".join(result.errors)
        self.assertIn("virgl", joined.lower())

    def test_low_memory_fails(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = _success_env(tmp, mem=1024)
            result = check_host(env)
        self.assertFalse(result.ok)
        joined = "\n".join(result.errors)
        self.assertIn("MemAvailable", joined)

    def test_root_euid_fails(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = _success_env(tmp, euid=0)
            result = check_host(env)
        self.assertFalse(result.ok)
        joined = "\n".join(result.errors)
        self.assertIn("root", joined.lower())


class OvmfSearchTests(unittest.TestCase):
    def test_prefers_4m_non_secboot_over_secboot(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            share = Path(td)
            secboot = _touch(share / "edk2" / "x64" / "OVMF_CODE.4m.secboot.fd")
            good = _touch(share / "edk2" / "x64" / "OVMF_CODE.4m.fd")
            later = _touch(share / "edk2" / "ovmf" / "OVMF_CODE.fd")
            found = find_ovmf_code(
                ovmf_code_candidates((share,)) + [secboot, later]
            )
            self.assertEqual(found, good)
            self.assertNotIn("secboot", found.name.lower())
            self.assertTrue(_is_4m_name(found))

    def test_search_order_arch_then_ubuntu_then_fedora(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            share = Path(td)
            ubuntu = _touch(share / "OVMF" / "OVMF_CODE_4M.fd")
            fedora = _touch(share / "edk2" / "ovmf" / "OVMF_CODE.fd")
            found = find_ovmf_code(ovmf_code_candidates((share,)))
            self.assertEqual(found, ubuntu)
            self.assertNotEqual(found, fedora)

    def test_fedora_generic_name_accepted_if_only_hit(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            share = Path(td)
            fedora = _touch(share / "edk2" / "ovmf" / "OVMF_CODE.fd")
            found = find_ovmf_code(ovmf_code_candidates((share,)))
            self.assertEqual(found, fedora)

    def test_secboot_only_is_rejected(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            share = Path(td)
            _touch(share / "edk2" / "x64" / "OVMF_CODE.4m.secboot.fd")
            _touch(share / "OVMF" / "OVMF_CODE_4M.secboot.fd")
            found = find_ovmf_code(ovmf_code_candidates((share,)))
            self.assertIsNone(found)

    def test_4m_preferred_over_later_generic(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            share = Path(td)
            four = _touch(share / "OVMF" / "OVMF_CODE_4M.fd")
            generic = _touch(share / "edk2" / "ovmf" / "OVMF_CODE.fd")
            found = find_ovmf_code(ovmf_code_candidates((share,)))
            self.assertEqual(found, four)
            self.assertNotEqual(found, generic)


def _is_4m_name(path: Path) -> bool:
    return "4m" in path.name.lower()


class ImagePruneTests(unittest.TestCase):
    def test_prune_removes_runs_keeps_golden_and_key(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache = tmp / "cache"
            golden = cache / "images" / "fake.qcow2"
            golden.parent.mkdir(parents=True)
            golden.write_bytes(b"golden")
            overlay = cache / "runs" / "r1" / "overlay.qcow2"
            overlay.parent.mkdir(parents=True)
            overlay.write_bytes(b"overlay")
            key = cache / "keys" / "id_ed25519"
            key.parent.mkdir(parents=True)
            key.write_bytes(b"secret")
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            try:
                code = main(["image-prune"])
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
            self.assertEqual(code, 0)
            self.assertFalse(overlay.exists())
            self.assertFalse((cache / "runs" / "r1").exists())
            self.assertTrue(golden.is_file())
            self.assertEqual(golden.read_bytes(), b"golden")
            self.assertTrue(key.is_file())
            self.assertEqual(key.read_bytes(), b"secret")

    def test_prune_images_removes_golden_keeps_key(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache = tmp / "cache"
            golden = cache / "images" / "fake.qcow2"
            golden.parent.mkdir(parents=True)
            golden.write_bytes(b"golden")
            overlay = cache / "runs" / "r1" / "overlay.qcow2"
            overlay.parent.mkdir(parents=True)
            overlay.write_bytes(b"overlay")
            key = cache / "keys" / "id_ed25519"
            key.parent.mkdir(parents=True)
            key.write_bytes(b"secret")
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            try:
                code = main(["image-prune", "--images"])
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
            self.assertEqual(code, 0)
            self.assertFalse(overlay.exists())
            self.assertFalse(golden.exists())
            self.assertTrue(key.is_file())
            self.assertEqual(key.read_bytes(), b"secret")

    def test_prune_twice_on_empty_exits_zero(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "cache"
            cache.mkdir()
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            try:
                self.assertEqual(main(["image-prune"]), 0)
                self.assertEqual(main(["image-prune"]), 0)
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old


class ShippedCliDoesNotSpawnQemuTests(unittest.TestCase):
    def test_check_host_does_not_exec_qemu(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = _success_env(tmp)
            with patch("subprocess.run") as run:
                # Real ssh-keygen is allowed; fail if qemu is invoked.
                def _run(cmd, *args, **kwargs):
                    if cmd and Path(str(cmd[0])).name.startswith("qemu"):
                        raise AssertionError(f"spawned qemu: {cmd}")
                    # Still generate a key without calling qemu.
                    key = Path(cmd[cmd.index("-f") + 1])
                    key.parent.mkdir(parents=True, exist_ok=True)
                    key.write_text("k", encoding="utf-8")
                    key.with_name(key.name + ".pub").write_text(
                        "p", encoding="utf-8"
                    )

                    class _C:
                        returncode = 0

                    return _C()

                run.side_effect = _run
                result = check_host(env)
            self.assertTrue(result.ok, result.errors)
            for call in run.call_args_list:
                cmd = call.args[0]
                self.assertFalse(
                    Path(str(cmd[0])).name.startswith("qemu"),
                    cmd,
                )

    def test_image_prune_does_not_exec_qemu(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "cache"
            cache.mkdir()
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            try:
                with patch("subprocess.run") as run:
                    code = main(["image-prune"])
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(code, 0)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
