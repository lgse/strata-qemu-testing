"""Argparse CLI: check-host, image-build, run-test, vm-run, vm-live, image-prune, spike."""

from __future__ import annotations

import argparse
import glob
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from strataqemu import config
from strataqemu.overlay import prune

log = logging.getLogger("strataqemu")

REQUIRED_BINARIES = (
    "qemu-system-x86_64",
    "qemu-img",
    "xorriso",
    "mkfs.vfat",
    "mcopy",
    "ssh",
    "curl",
)

# Relative to each firmware share root (default /usr/share). 4M non-secboot first.
OVMF_CODE_RELATIVE = (
    Path("edk2/x64/OVMF_CODE.4m.fd"),
    Path("OVMF/OVMF_CODE_4M.fd"),
    Path("edk2/ovmf/OVMF_CODE.fd"),
)

DEFAULT_FIRMWARE_SHARE_ROOTS: tuple[Path, ...] = (Path("/usr/share"),)

# Generic floor when no guest is selected: 8 GiB guest + 1 GiB host slack.
GENERIC_MEM_FLOOR_MIB = 9 * 1024

# Named when qemu-system-x86_64 is missing. Not a generic "install qemu".
QEMU_BOOTSTRAP_HINT = """\
Install host packages with `mise bootstrap` (apt / dnf / pacman).
QEMU system emulator:
  pacman: qemu-system-x86
  apt:    qemu-system-x86
  dnf:    qemu-system-x86
GL modules matching egl-headless + virtio-gpu-gl-pci:
  pacman: qemu-ui-egl-headless, qemu-hw-display-virtio-gpu-pci-gl
  apt:    qemu-system-gui
  dnf:    qemu-device-display-virtio-gpu-pci-gl
"""


@dataclass(frozen=True)
class CheckHostEnv:
    """Injectable host state so unit tests never touch this machine's /dev/kvm."""

    path: str
    kvm_path: Path
    kvm_accessible: Callable[[Path], bool]
    cache_dir: Path
    firmware_share_roots: Sequence[Path]
    virgl_ok: bool
    mem_available_mib: int | None
    euid: int
    keygen: Callable[[Path], None] | None = None


@dataclass(frozen=True)
class CheckHostResult:
    ok: bool
    errors: tuple[str, ...]
    ovmf_code: Path | None = None
    ssh_key: Path | None = None


def ovmf_code_candidates(share_roots: Sequence[Path] | None = None) -> list[Path]:
    roots = tuple(share_roots) if share_roots else DEFAULT_FIRMWARE_SHARE_ROOTS
    return [root / rel for root in roots for rel in OVMF_CODE_RELATIVE]


def _is_secboot(path: Path) -> bool:
    return "secboot" in path.name.lower()


def _is_4m(path: Path) -> bool:
    return "4m" in path.name.lower()


def find_ovmf_code(candidates: Sequence[Path]) -> Path | None:
    """First existing 4M non-secboot firmware in search order.

    Skips any path whose name contains ``secboot``. Among hits, prefers a
    4M-named file over a later generic ``OVMF_CODE.fd``.
    """
    found: list[Path] = []
    for cand in candidates:
        if cand.is_file() and not _is_secboot(cand):
            found.append(cand)
    if not found:
        return None
    four_m = [p for p in found if _is_4m(p)]
    if four_m:
        return four_m[0]
    return found[0]


def probe_virgl_egl() -> bool:
    """libvirglrenderer + an EGL DRM device. Does not spawn QEMU."""
    lib_patterns = (
        "/usr/lib/libvirglrenderer.so*",
        "/usr/lib64/libvirglrenderer.so*",
        "/usr/lib/*/libvirglrenderer.so*",
    )
    has_lib = any(glob.glob(pattern) for pattern in lib_patterns)
    dri = Path("/dev/dri")
    has_device = dri.is_dir() and any(dri.glob("renderD*"))
    return has_lib and has_device


def read_mem_available_mib() -> int | None:
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            kib = int(line.split()[1])
            return kib // 1024
    return None


def default_env() -> CheckHostEnv:
    return CheckHostEnv(
        path=os.environ.get("PATH", ""),
        kvm_path=Path("/dev/kvm"),
        kvm_accessible=lambda p: os.access(p, os.R_OK | os.W_OK),
        cache_dir=config.cache_dir(),
        firmware_share_roots=DEFAULT_FIRMWARE_SHARE_ROOTS,
        virgl_ok=probe_virgl_egl(),
        mem_available_mib=read_mem_available_mib(),
        euid=os.geteuid(),
    )


def ensure_ssh_key(key_path: Path, *, path: str | None = None) -> Path:
    """Generate ed25519 key with empty passphrase if missing."""
    if key_path.is_file():
        return key_path
    key_path.parent.mkdir(parents=True, exist_ok=True)
    keygen = shutil.which("ssh-keygen", path=path) or shutil.which("ssh-keygen")
    if keygen is None:
        raise FileNotFoundError(
            "ssh-keygen not found; OpenSSH is required to generate $CACHE/keys/"
        )
    subprocess.run(
        [keygen, "-t", "ed25519", "-f", str(key_path), "-N", ""],
        check=True,
        capture_output=True,
        text=True,
    )
    return key_path


def check_host(env: CheckHostEnv | None = None) -> CheckHostResult:
    """Fail-closed capability check. Does not install packages or spawn QEMU."""
    env = env if env is not None else default_env()
    errors: list[str] = []

    if env.euid == 0:
        errors.append("check-host: refusing to run as root.")

    kvm = env.kvm_path
    if not kvm.exists():
        errors.append(
            f"{kvm} does not exist. Load the kvm module and add your user to "
            "the kvm group, then re-login. KVM is required (no TCG fallback)."
        )
    elif not env.kvm_accessible(kvm):
        errors.append(
            f"{kvm} is not readable and writable. Add your user to the kvm "
            "group and re-login. KVM is required (no TCG fallback)."
        )

    missing_bins: list[str] = []
    for name in REQUIRED_BINARIES:
        if shutil.which(name, path=env.path) is None:
            missing_bins.append(name)
    if "qemu-system-x86_64" in missing_bins:
        errors.append(
            "qemu-system-x86_64 is not on PATH. "
            + QEMU_BOOTSTRAP_HINT.strip()
        )
    for name in missing_bins:
        if name == "qemu-system-x86_64":
            continue
        errors.append(f"{name} is not on PATH. Install it with `mise bootstrap`.")

    if not env.virgl_ok:
        errors.append(
            "virgl/EGL is not usable: need libvirglrenderer and a "
            "/dev/dri/renderD* device. Install the virgl/EGL packages with "
            "`mise bootstrap` and make sure a GPU driver is loaded."
        )

    candidates = ovmf_code_candidates(env.firmware_share_roots)
    ovmf = find_ovmf_code(candidates)
    if ovmf is None:
        searched = ", ".join(str(p) for p in candidates)
        errors.append(
            "OVMF 4M firmware not found (searched: "
            f"{searched}). Install edk2-ovmf / ovmf with `mise bootstrap`. "
            "Secure Boot (.secboot) firmware is not accepted."
        )

    mem = env.mem_available_mib
    if mem is None:
        errors.append("Could not read MemAvailable from /proc/meminfo.")
    elif mem < GENERIC_MEM_FLOOR_MIB:
        errors.append(
            f"MemAvailable is {mem} MiB; need at least {GENERIC_MEM_FLOOR_MIB} MiB "
            "(8 GiB guest plus 1 GiB host headroom)."
        )

    if errors:
        return CheckHostResult(ok=False, errors=tuple(errors), ovmf_code=ovmf)

    key_path = config.ssh_private_key(env.cache_dir)
    try:
        if env.keygen is not None:
            if not key_path.is_file():
                env.keygen(key_path)
        else:
            ensure_ssh_key(key_path, path=env.path)
    except (OSError, subprocess.CalledProcessError, FileNotFoundError) as exc:
        return CheckHostResult(
            ok=False,
            errors=(f"failed to generate SSH key at {key_path}: {exc}",),
            ovmf_code=ovmf,
        )

    return CheckHostResult(
        ok=True,
        errors=(),
        ovmf_code=ovmf,
        ssh_key=key_path,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m strataqemu",
        description=(
            "QEMU/KVM guests for Strata installer and desktop testing. "
            "Normally invoked through `mise run <task>`."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging to stderr",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "check-host",
        help="Check KVM, QEMU, virgl/EGL, OVMF, tools, and RAM; generate the SSH key",
    )

    image_build = sub.add_parser(
        "image-build",
        help="Build a golden image from its recipe, or print the existing one",
    )
    image_build.add_argument("guest", nargs="?", help="Guest id (e.g. ubuntu-2404)")
    image_build.add_argument(
        "--force",
        action="store_true",
        help="Rebuild even if a matching golden exists",
    )

    run_test = sub.add_parser(
        "run-test",
        help="Boot a throwaway overlay of a golden and run smoke checks",
    )
    run_test.add_argument("guest", nargs="?", help="Guest id")
    run_test.add_argument(
        "--session-only",
        action="store_true",
        help="Check the desktop session and take a screenshot; do not install Strata",
    )
    run_test.add_argument(
        "--install-from",
        nargs="+",
        metavar=("SOURCE", "PATH"),
        help=(
            "Install Strata from the latest GitHub release (release) "
            "or a host tarball (local-archive PATH), then launch it"
        ),
    )
    run_test.add_argument(
        "--keep",
        action="store_true",
        help="Keep the overlay disk after a successful run",
    )
    run_test.add_argument(
        "--omarchy-bindings",
        action="store_true",
        help=(
            "Omarchy-only: probe detect_omarchy_major and write/check "
            "Hyprland file-manager bindings (lgse/strata#743). "
            "Does not install or launch Strata. Optional STRATA_QEMU_INSTALL_SH "
            "uploads a host install.sh instead of curling main."
        ),
    )
    run_test.add_argument(
        "--update-from",
        metavar="VERSION",
        help=(
            "Install a previous Strata release (e.g. 0.15.0), then run current "
            "install.sh to update to latest. Override the default previous-version "
            "list with STRATA_QEMU_UPDATE_FROM (comma-separated)."
        ),
    )
    run_test.add_argument(
        "--udiskie-unlock",
        action="store_true",
        help=(
            "Omarchy/Arch: source install.sh udiskie-unlock helpers with PATH "
            "and BIN_PATH isolated (lgse/strata#537). Does not spawn real "
            "udiskie or rewrite host config. Optional STRATA_QEMU_INSTALL_SH "
            "uploads a host install.sh; otherwise the committed fixture is used."
        ),
    )
    run_test.add_argument(
        "--luks-hotplug",
        nargs="?",
        const="",
        metavar="PATH",
        help=(
            "Omarchy/Arch: install a host Strata binary/archive/checkout, "
            "register the udiskie unlock handler, hotplug a LUKS disk, and "
            "assert Strata is called (lgse/strata#537)."
        ),
    )

    vm_run = sub.add_parser(
        "vm-run",
        help="Boot an interactive throwaway overlay of a golden",
    )
    vm_run.add_argument("guest", nargs="?", help="Guest id")
    vm_run.add_argument(
        "--graphical",
        action="store_true",
        help="Open a GTK/SDL window instead of running headless",
    )
    vm_run.add_argument(
        "--keep",
        action="store_true",
        help="Keep the run directory after QEMU exits",
    )

    vm_live = sub.add_parser(
        "vm-live",
        help=(
            "Boot a live overlay, install a tagged or local Strata, "
            "and seed ~/fixtures with sample files"
        ),
        description=(
            "Boot a throwaway overlay, install Strata from a GitHub release "
            "tag or a host binary/archive, seed ~/fixtures with sample files, "
            "and leave a live QEMU session running."
        ),
    )
    vm_live.add_argument("guest", nargs="?", help="Guest id")
    vm_live_source = vm_live.add_mutually_exclusive_group()
    vm_live_source.add_argument(
        "--from-tag",
        metavar="VERSION",
        help="Install a GitHub release tag (e.g. 0.15.0)",
    )
    vm_live_source.add_argument(
        "--from-local",
        metavar="PATH",
        help=(
            "Install a host binary, a release tarball, or a checkout "
            "containing target/release/strata"
        ),
    )
    vm_live.add_argument(
        "--graphical",
        action="store_true",
        help="Open a GTK/SDL window (default)",
    )
    vm_live.add_argument(
        "--headless",
        action="store_true",
        help="No GTK/SDL window; SSH only",
    )
    vm_live.add_argument(
        "--keep",
        action="store_true",
        help="Keep the run directory after QEMU exits",
    )

    prune = sub.add_parser(
        "image-prune",
        help="Delete run directories; with --images also delete golden images",
    )
    prune.add_argument(
        "--images",
        action="store_true",
        help="Also delete golden images (never the SSH key or downloads)",
    )

    spike = sub.add_parser(
        "spike-wayland-ubuntu",
        help="Ad-hoc Ubuntu Noble Wayland session spike (predates the recipe system)",
    )
    spike.add_argument(
        "--keep",
        action="store_true",
        help="Keep the run directory after success",
    )
    return parser


def _run_image_prune(*, images: bool) -> int:
    try:
        prune(images=images)
    except OSError as exc:
        print(f"image-prune: {exc}", file=sys.stderr)
        return 1
    return 0


def _run_check_host() -> int:
    result = check_host()
    for err in result.errors:
        print(err, file=sys.stderr)
    if not result.ok:
        return 1
    print("check-host: ok")
    if result.ovmf_code is not None:
        print(f"OVMF: {result.ovmf_code}")
    if result.ssh_key is not None:
        print(f"SSH key: {result.ssh_key}")
    return 0


def normalize_cli_argv(argv: Sequence[str]) -> list[str]:
    """Drop lone ``--`` so ``run-test -- ubuntu-2404 --session-only`` works."""
    return [a for a in argv if a != "--"]


def parse_install_from(
    raw: Sequence[str] | None,
) -> tuple[str | None, str | None, str | None]:
    """Split ``--install-from release|local-archive [PATH]``.

    Returns ``(source, archive_path, error)``. A missing local-archive path
    is left for ``run_run_test`` so the fail-closed message stays in one place.
    """
    if not raw:
        return None, None, None
    source = raw[0]
    rest = list(raw[1:])
    if source not in ("release", "local-archive"):
        return (
            None,
            None,
            "run-test: --install-from must be 'release' or 'local-archive'",
        )
    if source == "local-archive":
        if len(rest) > 1:
            extra = " ".join(rest[1:])
            return None, None, f"run-test: unexpected extra arguments: {extra}"
        path = rest[0] if rest else None
        return source, path, None
    if rest:
        return (
            None,
            None,
            "run-test: --install-from release does not take a path",
        )
    return source, None, None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw = sys.argv[1:] if argv is None else argv
    try:
        args = parser.parse_args(normalize_cli_argv(raw))
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 1

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(name)s: %(message)s",
    )

    if args.command == "check-host":
        return _run_check_host()

    if args.command == "image-prune":
        return _run_image_prune(images=args.images)

    if args.command == "spike-wayland-ubuntu":
        from strataqemu.spike import run_spike_wayland_ubuntu

        return run_spike_wayland_ubuntu(keep=args.keep)

    if args.command == "image-build":
        from strataqemu.image_build import run_image_build

        return run_image_build(args.guest, force=args.force)

    if args.command == "run-test":
        from strataqemu.run_test import run_run_test

        install_from, archive_path, parse_err = parse_install_from(
            args.install_from
        )
        if parse_err is not None:
            print(parse_err, file=sys.stderr)
            return 2

        return run_run_test(
            args.guest,
            session_only=args.session_only,
            install_from=install_from,
            archive_path=archive_path,
            keep=args.keep,
            omarchy_bindings=args.omarchy_bindings,
            update_from=args.update_from,
            udiskie_unlock=args.udiskie_unlock,
            luks_hotplug=args.luks_hotplug,
        )

    if args.command == "vm-run":
        from strataqemu.run_test import run_vm_run

        return run_vm_run(
            args.guest,
            graphical=args.graphical,
            keep=args.keep,
        )

    if args.command == "vm-live":
        from strataqemu.vm_live import run_vm_live

        if args.graphical and args.headless:
            print(
                "vm-live: --graphical and --headless cannot be combined",
                file=sys.stderr,
            )
            print(
                "usage: python -m strataqemu vm-live "
                "(--from-tag VERSION | --from-local PATH) "
                "[--graphical] [--headless] [--keep] <guest>",
                file=sys.stderr,
            )
            return 2

        return run_vm_live(
            args.guest,
            from_tag=args.from_tag,
            from_local=args.from_local,
            graphical=not args.headless,
            keep=args.keep,
        )

    parser.error(f"unknown command {args.command!r}")
    return 2
