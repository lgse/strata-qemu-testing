"""Session, screenshot, install, version, and desktop-oracle steps for ``run-test``.

Injectable SSH/run-dir so host unittests never spawn QEMU. ``--session-only``
runs session + in-guest screenshot; it does not gtk-launch Strata, run
``install.sh``, or wait on the application bus name. ``--install-from``
release/local-archive on ``arch``, ``ubuntu-2404``, ``fedora-workstation``,
``omarchy-3``, and ``omarchy-4`` runs session → install → version →
desktop-entry → window. ``--update-from VERSION`` on the same guests seeds
that previous release, then runs current ``install.sh`` to latest.
``--omarchy-bindings`` is a separate Omarchy-only flow (lgse/strata#743):
session → detect → write/check Hyprland bindings.
``--udiskie-unlock`` is a separate Omarchy/Arch flow that sources
``configure_udiskie_unlock`` with PATH and ``BIN_PATH`` isolated.
``--luks-hotplug PATH`` installs a host Strata binary, registers the
udiskie handler, hotplugs a LUKS disk, and asserts Strata is called.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from strataqemu import config
from strataqemu.guest import Guest
from strataqemu.qemu import (
    LUKS_HOTPLUG_QMP_FAILED,
    LUKS_HOTPLUG_SERIAL,
    LUKS_IMAGE_TOOLS_MISSING,
    Machine,
    create_luks_image,
    qmp_hotplug_raw_disk,
    qmp_screendump,
    qmp_send_key,
    vnc_framebuffer_png,
)
from strataqemu.ssh import scp_command, scp_download_command

log = logging.getLogger("strataqemu")

RunFn = Callable[..., subprocess.CompletedProcess]

SESSION_TIMEOUT_S = 60
INSTALL_TIMEOUT_S = 180
WINDOW_TIMEOUT_S = 45
DESKTOP_ENTRY_TIMEOUT_S = 10
VERSION_TIMEOUT_S = 10
GUEST_SCREENSHOT_REMOTE = "/tmp/strata-window.png"
GUEST_ARCHIVE_REMOTE = "/tmp/strata-archive.tar.gz"
ABOUT_VERSION_PNG_NAME = "about-version.png"
ABOUT_OPEN_SETTLE_S = 0.6
ABOUT_NAV_SETTLE_S = 0.08
ABOUT_PAGE_SETTLE_S = 0.5
ABOUT_INPUT_MISSING_EXIT = 2
# Sidebar after Ctrl+,: first Tab focuses General (see live Omarchy/GNOME
# shots); About is the fifth item (General, Keybindings, Theme, Updates,
# About). Space activates the focused nav button.
ABOUT_SIDEBAR_TABS = 5
ABOUT_CLI_ALREADY_RECORDED = "CLI version already recorded"
ABOUT_INPUT_FAILED = "could not open Settings About (no wtype, hyprctl, or QMP)"
ABOUT_BEFORE_PNG_NAME = "about-version-before.png"
ABOUT_AFTER_PNG_NAME = "about-version-after.png"
SCREENSHOT_TOOL_MISSING = "screenshot tool missing; rebuild the golden"
GNOME_SCREENSHOT_TIMEOUT_S = 15
STRATA_BUS_NAME = "io.github.lgse.Strata"
STRATA_DESKTOP_FILE = "io.github.lgse.Strata.desktop"
STRATA_BIN_REL = ".local/bin/strata"
STRATA_VERSION_COMMAND = f"$HOME/{STRATA_BIN_REL} --version"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MIN_PNG_BYTES = 256
SESSION_ONLY_STEPS = ("session", "screenshot")
INSTALL_FROM_RELEASE_STEPS = (
    "session",
    "install",
    "version",
    "desktop-entry",
    "window",
    "about-version",
)
UPDATE_FROM_STEPS = (
    "session",
    "install-previous",
    "version-previous",
    "about-version-before",
    "update",
    "version",
    "desktop-entry",
    "window",
    "about-version-after",
)
OMARCHY_BINDINGS_STEPS = (
    "session",
    "omarchy-detect",
    "omarchy-bindings",
    "screenshot",
)
UDISKIE_UNLOCK_STEPS = (
    "session",
    "udiskie-unlock",
    "screenshot",
)
LUKS_HOTPLUG_STEPS = (
    "session",
    "install",
    "udiskie-handler",
    "luks-hotplug",
    "screenshot",
)
OMARCHY_BINDINGS_GUESTS = frozenset({"omarchy-4", "omarchy-3"})
UDISKIE_UNLOCK_GUESTS = frozenset({"omarchy-4", "omarchy-3", "arch"})
LUKS_HOTPLUG_GUESTS = UDISKIE_UNLOCK_GUESTS
LUKS_HOTPLUG_TIMEOUT_S = 30
GUEST_HOOK_LOG = "/tmp/strata-luks-hook.log"
# Whole N.M token cases for omarchy_major_from (lgse/strata#743 / #652).
OMARCHY_TOKEN_CASES: tuple[tuple[str, str], ...] = (
    ("4.0.0-1", "4"),
    ("4.0.0.alpha", "4"),
    ("3.8.5", "3"),
    ("1:4.0.0-1", "4"),
    ("Omarchy 2.3.1", ""),
    ("5.4.0", ""),
    ("dev (b280f130)", ""),
)
OMARCHY_DEV_HASH_OUTPUT = "dev (b280f130)"
GUEST_INSTALL_SH_REMOTE = "/tmp/strata-install.sh"
OMARCHY_BINDINGS_MARKER = "strata-installer: file-manager start"
INSTALL_FROM_RELEASE_GUESTS = frozenset(
    {
        "arch",
        "ubuntu-2404",
        "fedora-workstation",
        "omarchy-4",
        "omarchy-3",
    }
)
VERSION_CLI_RE = re.compile(r"^(strata\s+)?v?\d+\.\d+", re.IGNORECASE)
INSTALL_FROM_FAIL_CLOSED = (
    "run-test: --install-from is not supported for this guest; "
    "use --session-only"
)
OMARCHY_BINDINGS_FAIL_CLOSED = (
    "run-test: --omarchy-bindings is only supported for omarchy-3 and omarchy-4"
)
OMARCHY_BINDINGS_EXCLUSIVE = (
    "run-test: --omarchy-bindings cannot be combined with "
    "--session-only, --install-from, --update-from, --udiskie-unlock, "
    "or --luks-hotplug"
)
UDISKIE_UNLOCK_FAIL_CLOSED = (
    "run-test: --udiskie-unlock is only supported for omarchy-3, omarchy-4, and arch"
)
UDISKIE_UNLOCK_EXCLUSIVE = (
    "run-test: --udiskie-unlock cannot be combined with "
    "--session-only, --install-from, --omarchy-bindings, --update-from, "
    "or --luks-hotplug"
)
UDISKIE_UNLOCK_FIXTURE_MISSING = (
    "run-test: udiskie-unlock installer fixture is missing"
)
LUKS_HOTPLUG_FAIL_CLOSED = (
    "run-test: --luks-hotplug is only supported for omarchy-3, omarchy-4, and arch"
)
LUKS_HOTPLUG_EXCLUSIVE = (
    "run-test: --luks-hotplug cannot be combined with "
    "--session-only, --install-from, --omarchy-bindings, --update-from, "
    "or --udiskie-unlock"
)
LUKS_HOTPLUG_MISSING_PATH = (
    "run-test: --luks-hotplug requires a host Strata binary, archive, or checkout"
)
LUKS_HOTPLUG_HOOK_MISSING = (
    "run-test: strata --install-udiskie-unlock did not write the udiskie hook"
)
INSTALL_SH_SHA256_PREFIX = "INSTALL_SH_SHA256="
INSTALL_SH_URL = "https://raw.githubusercontent.com/lgse/strata/main/install.sh"
INSTALL_SH_FLAGS = (
    "--non-interactive",
    "--with-desktop-entry",
    "--without-file-chooser",
)
GITHUB_LATEST_RELEASE = "https://github.com/lgse/strata/releases/latest"
ARCHIVE_VERSION_RE = re.compile(r"^strata-v?(.+)$", re.IGNORECASE)
_ARCHIVE_TARGET_MARKERS = (
    "-x86_64-",
    "-aarch64-",
    "-arm64-",
    "-x86_64.",
    "-aarch64.",
    "-arm64.",
)
LOCAL_ARCHIVE_MISSING_PATH = (
    "run-test: --install-from local-archive requires a host archive path"
)
INSTALL_SH_MISSING_PATH = (
    "run-test: STRATA_QEMU_INSTALL_SH is set but the file is missing"
)
UPDATE_FROM_ENV = config.UPDATE_FROM_ENV
# Previous releases the update-from flow is written against. Override with
# STRATA_QEMU_UPDATE_FROM (comma-separated tags such as 0.15.0,0.14.0).
DEFAULT_UPDATE_FROM_VERSIONS = ("0.15.0", "0.14.0")
STRATA_RELEASE_TARGET = "x86_64-unknown-linux-gnu"
GITHUB_RELEASE_DOWNLOAD = "https://github.com/lgse/strata/releases/download"
UPDATE_FROM_MISSING_VERSION = (
    "run-test: --update-from requires a previous version (e.g. 0.15.0)"
)
UPDATE_FROM_BAD_VERSION = (
    "run-test: --update-from version is not a Strata release tag"
)
UPDATE_FROM_EMPTY_LIST = (
    f"run-test: {UPDATE_FROM_ENV} did not list any versions"
)
UPDATE_FROM_EXCLUSIVE = (
    "run-test: --update-from cannot be combined with "
    "--session-only, --install-from, --omarchy-bindings, --udiskie-unlock, "
    "or --luks-hotplug"
)
UPDATE_FROM_FAIL_CLOSED = (
    "run-test: --update-from is not supported for this guest; "
    "use --session-only"
)
UPDATE_FROM_SAME_AS_LATEST = (
    "run-test: --update-from version is already the latest release"
)
# Substrings that must not appear in --session-only guest commands.
SESSION_ONLY_FORBIDDEN = (
    "install.sh",
    "strata --version",
    "gtk-launch",
    "gio launch",
    "NameHasOwner",
    STRATA_BUS_NAME,
)
GDBUS_NAME_HAS_OWNER_ARGV = (
    "gdbus",
    "call",
    "--session",
    "--dest",
    "org.freedesktop.DBus",
    "--object-path",
    "/org/freedesktop/DBus",
    "--method",
    "org.freedesktop.DBus.NameHasOwner",
    STRATA_BUS_NAME,
)


class SessionSmokeError(RuntimeError):
    """Fail-closed session / screenshot / GNOME oracle error."""


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def guest_tests_dir() -> Path:
    return repo_root() / "guest-tests"


def smoke_session_script() -> Path:
    return guest_tests_dir() / "smoke-session.sh"


def smoke_desktop_script() -> Path:
    return guest_tests_dir() / "smoke-desktop.sh"


def smoke_install_script() -> Path:
    return guest_tests_dir() / "smoke-install.sh"


def smoke_update_script() -> Path:
    return guest_tests_dir() / "smoke-update.sh"


def smoke_about_script() -> Path:
    return guest_tests_dir() / "smoke-about.sh"


def smoke_omarchy_detect_script() -> Path:
    return guest_tests_dir() / "smoke-omarchy-detect.sh"


def smoke_omarchy_bindings_script() -> Path:
    return guest_tests_dir() / "smoke-omarchy-bindings.sh"


def smoke_udiskie_unlock_script() -> Path:
    return guest_tests_dir() / "smoke-udiskie-unlock.sh"


def smoke_luks_hotplug_script() -> Path:
    return guest_tests_dir() / "smoke-luks-hotplug.sh"


def udiskie_unlock_install_sh_fixture() -> Path:
    return (
        repo_root()
        / "tests"
        / "fixtures"
        / "udiskie-unlock"
        / "install-with-helpers.sh"
    )


def omarchy_install_major(guest: Guest | str) -> int | None:
    """Installer major the guest recipe is supposed to look like, or None."""
    guest_id = guest.id if isinstance(guest, Guest) else guest
    if guest_id == "omarchy-4":
        return 4
    if guest_id == "omarchy-3":
        return 3
    return None


def supports_omarchy_bindings(guest: Guest | str) -> bool:
    return omarchy_install_major(guest) is not None


def supports_udiskie_unlock(guest: Guest | str) -> bool:
    guest_id = guest.id if isinstance(guest, Guest) else guest
    return guest_id in UDISKIE_UNLOCK_GUESTS


def supports_luks_hotplug(guest: Guest | str) -> bool:
    guest_id = guest.id if isinstance(guest, Guest) else guest
    return guest_id in LUKS_HOTPLUG_GUESTS


def missing_golden_message(guest_id: str) -> str:
    return f"run `mise run image-build -- {guest_id}` first"


def compositor_process_name(guest: Guest | str) -> str:
    """Process name for the session compositor check.

    ``ubuntu-2404`` (and GNOME/Mutter recipes) use ``gnome-shell``.
    ``arch`` (Hyprland) uses ``Hyprland``.
    """
    if isinstance(guest, Guest):
        guest_id = guest.id
        kind = guest.session.kind.lower()
        compositor = guest.session.compositor.lower()
    else:
        guest_id = guest
        kind = ""
        compositor = ""
    if guest_id == "ubuntu-2404" or kind == "gnome" or compositor == "mutter":
        return "gnome-shell"
    if (
        guest_id in {"arch", "omarchy-4", "omarchy-3"}
        or compositor == "hyprland"
        or kind == "hyprland"
    ):
        return "Hyprland"
    return compositor or "gnome-shell"


def screenshot_tool_for_compositor(compositor: str) -> str:
    """In-guest capture binary. Hyprland uses ``grim``; GNOME uses gnome-screenshot."""
    if compositor == "Hyprland":
        return "grim"
    return "gnome-screenshot"


def supports_install_from_release(guest: Guest | str) -> bool:
    guest_id = guest.id if isinstance(guest, Guest) else guest
    return guest_id in INSTALL_FROM_RELEASE_GUESTS


def supports_update_from(guest: Guest | str) -> bool:
    return supports_install_from_release(guest)


def install_arch_script() -> Path:
    return repo_root() / "images" / "common" / "install-arch.sh"


def parse_session_exports(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines from ``smoke-session.sh`` stdout."""
    wanted = {
        "SESSION_ID",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "WAYLAND_DISPLAY",
        "HYPRLAND_INSTANCE_SIGNATURE",
    }
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key in wanted:
            env[key] = value
    return env


def session_env_from_exports(exports: Mapping[str, str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in (
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "WAYLAND_DISPLAY",
        "HYPRLAND_INSTANCE_SIGNATURE",
    ):
        if key in exports and exports[key]:
            env[key] = exports[key]
    return env


def env_prefix(env: Mapping[str, str]) -> str:
    return " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())


def parse_name_has_owner(output: str) -> bool:
    """Parse ``gdbus … NameHasOwner`` output (``(true,)`` / ``(false,)``)."""
    blob = output.lower()
    if "true" in blob:
        return True
    if "false" in blob:
        return False
    raise SessionSmokeError(f"unparseable NameHasOwner reply: {output!r}")


def screenshot_tool_missing(which_returncode: int) -> bool:
    return which_returncode != 0


def gdbus_name_has_owner_command(env: Mapping[str, str] | None = None) -> str:
    body = " ".join(shlex.quote(part) for part in GDBUS_NAME_HAS_OWNER_ARGV)
    if env:
        return f"{env_prefix(env)} {body}"
    return body


def install_sh_argv(*, archive: str | None = None) -> list[str]:
    argv = list(INSTALL_SH_FLAGS)
    if archive:
        argv.extend(["--archive", archive])
    return argv


def install_smoke_command(
    *,
    archive: str | None = None,
    forbid_omarchy: bool = False,
) -> str:
    """Host-side SSH command that runs the uploaded guest install smoke."""
    prefix = "SMOKE_FORBID_OMARCHY=1 " if forbid_omarchy else ""
    flags = " ".join(shlex.quote(part) for part in install_sh_argv(archive=archive))
    return f"{prefix}bash /tmp/smoke-install.sh {flags}".rstrip()


def update_smoke_command(
    *,
    phase: str,
    from_version: str | None = None,
    archive: str | None = None,
    forbid_omarchy: bool = False,
) -> str:
    """Host-side SSH command that runs the uploaded guest update smoke."""
    parts: list[str] = [f"SMOKE_UPDATE_PHASE={shlex.quote(phase)}"]
    if from_version:
        parts.append(f"UPDATE_FROM_VERSION={shlex.quote(from_version)}")
    if archive:
        parts.append(f"UPDATE_FROM_ARCHIVE={shlex.quote(archive)}")
    if forbid_omarchy:
        parts.append("SMOKE_FORBID_OMARCHY=1")
    parts.append("bash /tmp/smoke-update.sh")
    return " ".join(parts)


def about_smoke_command(*, compositor: str) -> str:
    return (
        f"SMOKE_COMPOSITOR={shlex.quote(compositor)} "
        f"SMOKE_ABOUT_TABS={ABOUT_SIDEBAR_TABS} "
        "bash /tmp/smoke-about.sh"
    )


def qmp_open_about_chords() -> list[list[str]]:
    """Ctrl+, then Tab × ``ABOUT_SIDEBAR_TABS`` (About), then Space."""
    return (
        [["ctrl", "comma"]]
        + [["tab"] for _ in range(ABOUT_SIDEBAR_TABS)]
        + [["spc"]]
    )


def qmp_about_nav_chords() -> list[list[str]]:
    """Tabs and Space after Settings is already open."""
    return [["tab"] for _ in range(ABOUT_SIDEBAR_TABS)] + [["spc"]]


def omarchy_detect_command(
    *,
    case: str = "live",
    install_sh: str = GUEST_INSTALL_SH_REMOTE,
    output: str | None = None,
    user_version: str | None = None,
) -> str:
    """Host-side SSH command that runs the uploaded Omarchy detect smoke."""
    parts = [
        f"INSTALL_SH={shlex.quote(install_sh)}",
        f"SMOKE_OMARCHY_CASE={shlex.quote(case)}",
    ]
    if output is not None:
        parts.append(f"SMOKE_OMARCHY_OUTPUT={shlex.quote(output)}")
    if user_version is not None:
        parts.append(f"SMOKE_USER_VERSION={shlex.quote(user_version)}")
    parts.append("bash /tmp/smoke-omarchy-detect.sh")
    return " ".join(parts)


def omarchy_bindings_command(
    major: int,
    *,
    write: bool = False,
    install_sh: str = GUEST_INSTALL_SH_REMOTE,
) -> str:
    parts = [f"SMOKE_OMARCHY_MAJOR={int(major)}"]
    if write:
        parts.append("SMOKE_WRITE_BINDINGS=1")
        parts.append(f"INSTALL_SH={shlex.quote(install_sh)}")
    parts.append("bash /tmp/smoke-omarchy-bindings.sh")
    return " ".join(parts)


def udiskie_unlock_command(
    *,
    install_sh: str = GUEST_INSTALL_SH_REMOTE,
    case: str = "configure",
    args: str | None = None,
    omarchy_major: str | None = None,
    arch_based: str = "no",
    udiskie_stub: bool = False,
    host_udiskie: bool = False,
    marker: bool = False,
    prompt: str | None = None,
    bin_exit: str | None = None,
    call_twice: bool = False,
    bin_has_flag_string: bool = False,
) -> str:
    """Host-side SSH command that runs the uploaded udiskie-unlock smoke."""
    parts = [
        f"INSTALL_SH={shlex.quote(install_sh)}",
        f"SMOKE_UDISKIE_CASE={shlex.quote(case)}",
        f"SMOKE_ARCH_BASED={shlex.quote(arch_based)}",
    ]
    if args is not None:
        parts.append(f"SMOKE_UDISKIE_ARGS={shlex.quote(args)}")
    if omarchy_major is not None:
        parts.append(f"SMOKE_OMARCHY_MAJOR={shlex.quote(omarchy_major)}")
    if udiskie_stub:
        parts.append("SMOKE_UDISKIE_STUB=1")
    if host_udiskie:
        parts.append("SMOKE_HOST_UDISKIE=1")
    if marker:
        parts.append("SMOKE_MARKER=1")
    if prompt is not None:
        parts.append(f"SMOKE_PROMPT={shlex.quote(prompt)}")
    if bin_exit is not None:
        parts.append(f"SMOKE_BIN_EXIT={shlex.quote(bin_exit)}")
    if call_twice:
        parts.append("SMOKE_CALL_TWICE=1")
    if bin_has_flag_string:
        parts.append("SMOKE_BIN_HAS_FLAG_STRING=1")
    parts.append("bash /tmp/smoke-udiskie-unlock.sh")
    return " ".join(parts)


def parse_smoke_kv(text: str, key: str) -> str:
    """Read ``KEY=value`` from guest-smoke stdout. Empty values are allowed."""
    prefix = f"{key}="
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(prefix):
            return line.split("=", 1)[1]
    raise SessionSmokeError(f"smoke did not print {key}=")


def run_omarchy_install_oracles(
    machine: Machine,
    *,
    major: int,
    probe_pr743: bool = True,
    write_bindings: bool = True,
    run: RunFn | None = None,
    commands: list[str] | None = None,
) -> tuple[list[dict], dict]:
    """``omarchy-detect`` then write/check ``omarchy-bindings``.

    Used only by ``--omarchy-bindings``, not by ``--install-from``.
    """
    recorded = commands if commands is not None else []
    steps: list[dict] = []
    extras: dict = {}

    detect_helper = smoke_omarchy_detect_script()
    if not detect_helper.is_file():
        raise SessionSmokeError(f"missing Omarchy detect smoke {detect_helper}")
    scp_to_guest(
        machine,
        detect_helper,
        "/tmp/smoke-omarchy-detect.sh",
        run=run,
        commands=recorded,
    )
    started = time.monotonic()
    live = ssh_run(
        machine,
        omarchy_detect_command(case="live"),
        timeout=15,
        check=True,
        run=run,
        commands=recorded,
    )
    detected = parse_smoke_kv(f"{live.stdout}{live.stderr}", "DETECTED_MAJOR")
    expected = str(major)
    if detected != expected:
        raise SessionSmokeError(
            f"omarchy-detect: live major {detected!r}, expected {expected}"
        )
    if probe_pr743:
        for output, want in OMARCHY_TOKEN_CASES:
            token = ssh_run(
                machine,
                omarchy_detect_command(case="token", output=output),
                timeout=15,
                check=True,
                run=run,
                commands=recorded,
            )
            got = parse_smoke_kv(f"{token.stdout}{token.stderr}", "DETECTED_MAJOR")
            if got != want:
                raise SessionSmokeError(
                    f"omarchy-detect: token {output!r} detected {got!r}, "
                    f"expected {want!r} (lgse/strata#743)"
                )
        hashed = ssh_run(
            machine,
            omarchy_detect_command(
                case="command", output=OMARCHY_DEV_HASH_OUTPUT
            ),
            timeout=15,
            check=True,
            run=run,
            commands=recorded,
        )
        got_hash = parse_smoke_kv(
            f"{hashed.stdout}{hashed.stderr}", "DETECTED_MAJOR"
        )
        if got_hash != expected:
            raise SessionSmokeError(
                f"omarchy-detect: {OMARCHY_DEV_HASH_OUTPUT!r} detected "
                f"{got_hash!r}, expected {expected} from the version file "
                "(lgse/strata#743)"
            )
        extras["omarchy_pr743_probes"] = "pass"
    steps.append(
        {
            "name": "omarchy-detect",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "detected_major": detected,
            "oracle": "detect_omarchy_major",
        }
    )
    extras["omarchy_major"] = detected

    bind_helper = smoke_omarchy_bindings_script()
    if not bind_helper.is_file():
        raise SessionSmokeError(
            f"missing Omarchy bindings smoke {bind_helper}"
        )
    scp_to_guest(
        machine,
        bind_helper,
        "/tmp/smoke-omarchy-bindings.sh",
        run=run,
        commands=recorded,
    )
    started = time.monotonic()
    bindings = ssh_run(
        machine,
        omarchy_bindings_command(major, write=write_bindings),
        timeout=15,
        check=True,
        run=run,
        commands=recorded,
    )
    kind = parse_smoke_kv(f"{bindings.stdout}{bindings.stderr}", "BINDINGS_KIND")
    path = parse_smoke_kv(f"{bindings.stdout}{bindings.stderr}", "BINDINGS_PATH")
    want_kind = "lua" if major == 4 else "conf"
    if kind != want_kind:
        raise SessionSmokeError(
            f"omarchy-bindings: kind {kind!r}, expected {want_kind} "
            f"for Omarchy {major}"
        )
    steps.append(
        {
            "name": "omarchy-bindings",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "kind": kind,
            "path": path,
        }
    )
    extras["omarchy_bindings"] = kind
    return steps, extras


def stage_guest_install_sh(
    machine: Machine,
    *,
    install_sh_path: Path | str | None,
    run: RunFn | None = None,
    commands: list[str] | None = None,
) -> None:
    """Put ``install.sh`` at ``GUEST_INSTALL_SH_REMOTE``. Never pipe curl to bash."""
    recorded = commands if commands is not None else []
    if install_sh_path is not None:
        src = Path(install_sh_path)
        if not src.is_file():
            raise SessionSmokeError(f"run-test: install.sh not found: {src}")
        scp_to_guest(
            machine,
            src,
            GUEST_INSTALL_SH_REMOTE,
            run=run,
            commands=recorded,
        )
        return
    ssh_run(
        machine,
        (
            f"curl -fsSL {shlex.quote(INSTALL_SH_URL)} "
            f"-o {shlex.quote(GUEST_INSTALL_SH_REMOTE)}"
        ),
        timeout=60,
        check=True,
        run=run,
        commands=recorded,
    )


def run_omarchy_bindings_steps(
    machine: Machine,
    *,
    guest: Guest,
    screenshot_dest: Path,
    qmp_dest: Path | None = None,
    session_timeout: float = SESSION_TIMEOUT_S,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
    install_sh_path: Path | str | None = None,
) -> tuple[list[dict], dict]:
    """session → detect (#743 probes) → write/check bindings → screenshot."""
    major = omarchy_install_major(guest)
    if major is None:
        raise SessionSmokeError(OMARCHY_BINDINGS_FAIL_CLOSED)
    compositor = compositor_process_name(guest)
    recorded = commands if commands is not None else []
    steps: list[dict] = []

    started = time.monotonic()
    exports = run_session_step(
        machine,
        compositor=compositor,
        timeout=session_timeout,
        run=run,
        commands=recorded,
        sleep=sleep,
    )
    steps.append(
        {
            "name": "session",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "sid": exports.get("SESSION_ID"),
        }
    )
    env = session_env_from_exports(exports)
    stage_guest_install_sh(
        machine,
        install_sh_path=install_sh_path,
        run=run,
        commands=recorded,
    )
    extra_steps, extras = run_omarchy_install_oracles(
        machine,
        major=major,
        probe_pr743=True,
        write_bindings=True,
        run=run,
        commands=recorded,
    )
    steps.extend(extra_steps)

    shot_started = time.monotonic()
    capture_guest_screenshot(
        machine,
        env,
        screenshot_dest,
        tool=screenshot_tool_for_compositor(compositor),
        run=run,
        commands=recorded,
    )
    if qmp_dest is not None:
        extra_qmp_screendump(machine, qmp_dest)
    steps.append(
        {
            "name": "screenshot",
            "status": "pass",
            "seconds": round(time.monotonic() - shot_started, 1),
            "path": str(screenshot_dest),
        }
    )
    extras["screenshot"] = str(screenshot_dest)
    return steps, extras


def udiskie_unlock_guest_cases(guest: Guest | str) -> tuple[dict, ...]:
    """In-guest smoke cases for ``--udiskie-unlock``. Host unittests own the full matrix."""
    major = omarchy_install_major(guest)
    if major is not None:
        return (
            {
                "name": "parse-args-with-flag",
                "case": "parse-args",
                "args": "--with-udiskie-unlock",
                "expect": {"NON_INTERACTIVE": "yes", "WITH_UDISKIE_UNLOCK": "yes"},
            },
            {
                "name": "eligible-prompt-yes",
                "case": "configure",
                "omarchy_major": str(major),
                "arch_based": "yes",
                "udiskie_stub": True,
                "marker": True,
                "prompt": "yes",
                "expect": {"BIN_CALLS": "1"},
            },
            {
                "name": "eligible-prompt-no",
                "case": "configure",
                "omarchy_major": str(major),
                "arch_based": "yes",
                "udiskie_stub": True,
                "marker": True,
                "prompt": "no",
                "expect": {"BIN_CALLS": "0"},
            },
            {
                "name": "omarchy-no-stub-ask",
                "case": "configure",
                "omarchy_major": str(major),
                "arch_based": "yes",
                "marker": True,
                "expect": {"BIN_CALLS": "0"},
            },
        )
    return (
        {
            "name": "parse-args-with-flag",
            "case": "parse-args",
            "args": "--with-udiskie-unlock",
            "expect": {"NON_INTERACTIVE": "yes", "WITH_UDISKIE_UNLOCK": "yes"},
        },
        {
            "name": "arch-prompt-yes",
            "case": "configure",
            "arch_based": "yes",
            "udiskie_stub": True,
            "marker": True,
            "prompt": "yes",
            "expect": {"BIN_CALLS": "1"},
        },
        {
            "name": "arch-no-stub-ask",
            "case": "configure",
            "arch_based": "yes",
            "host_udiskie": True,
            "expect": {"BIN_CALLS": "0", "UDISKIE_ON_PATH": "0"},
        },
    )


def run_udiskie_unlock_oracles(
    machine: Machine,
    *,
    guest: Guest,
    install_sh: str = GUEST_INSTALL_SH_REMOTE,
    run: RunFn | None = None,
    commands: list[str] | None = None,
) -> tuple[list[dict], dict]:
    """Upload the smoke and run PATH-isolated helper cases. Does not launch Strata."""
    recorded = commands if commands is not None else []
    helper = smoke_udiskie_unlock_script()
    if not helper.is_file():
        raise SessionSmokeError(f"missing udiskie-unlock smoke {helper}")
    scp_to_guest(
        machine,
        helper,
        "/tmp/smoke-udiskie-unlock.sh",
        run=run,
        commands=recorded,
    )
    started = time.monotonic()
    case_results: list[str] = []
    for spec in udiskie_unlock_guest_cases(guest):
        remote = udiskie_unlock_command(
            install_sh=install_sh,
            case=str(spec["case"]),
            args=spec.get("args"),  # type: ignore[arg-type]
            omarchy_major=spec.get("omarchy_major"),  # type: ignore[arg-type]
            arch_based=str(spec.get("arch_based", "no")),
            udiskie_stub=bool(spec.get("udiskie_stub")),
            host_udiskie=bool(spec.get("host_udiskie")),
            marker=bool(spec.get("marker")),
            prompt=spec.get("prompt"),  # type: ignore[arg-type]
        )
        proc = ssh_run(
            machine,
            remote,
            timeout=15,
            check=True,
            run=run,
            commands=recorded,
        )
        blob = f"{proc.stdout}{proc.stderr}"
        expect = spec.get("expect") or {}
        for key, want in expect.items():  # type: ignore[union-attr]
            got = parse_smoke_kv(blob, str(key))
            if got != want:
                raise SessionSmokeError(
                    f"udiskie-unlock: {spec['name']} {key}={got!r}, expected {want!r}"
                )
        if expect.get("BIN_CALLS") == "1":  # type: ignore[union-attr]
            argv = parse_smoke_kv(blob, "BIN_ARGV")
            if argv != "--install-udiskie-unlock":
                raise SessionSmokeError(
                    f"udiskie-unlock: {spec['name']} BIN_ARGV={argv!r}"
                )
        case_results.append(str(spec["name"]))
    steps = [
        {
            "name": "udiskie-unlock",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "cases": case_results,
        }
    ]
    extras = {"udiskie_unlock_cases": case_results}
    return steps, extras


def run_udiskie_unlock_steps(
    machine: Machine,
    *,
    guest: Guest,
    screenshot_dest: Path,
    qmp_dest: Path | None = None,
    session_timeout: float = SESSION_TIMEOUT_S,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
    install_sh_path: Path | str | None = None,
) -> tuple[list[dict], dict]:
    """session → PATH-isolated udiskie helper cases → screenshot."""
    if not supports_udiskie_unlock(guest):
        raise SessionSmokeError(UDISKIE_UNLOCK_FAIL_CLOSED)
    compositor = compositor_process_name(guest)
    recorded = commands if commands is not None else []
    steps: list[dict] = []

    started = time.monotonic()
    exports = run_session_step(
        machine,
        compositor=compositor,
        timeout=session_timeout,
        run=run,
        commands=recorded,
        sleep=sleep,
    )
    steps.append(
        {
            "name": "session",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "sid": exports.get("SESSION_ID"),
        }
    )
    env = session_env_from_exports(exports)
    if install_sh_path is None:
        raise SessionSmokeError(UDISKIE_UNLOCK_FIXTURE_MISSING)
    stage_guest_install_sh(
        machine,
        install_sh_path=install_sh_path,
        run=run,
        commands=recorded,
    )
    extra_steps, extras = run_udiskie_unlock_oracles(
        machine,
        guest=guest,
        run=run,
        commands=recorded,
    )
    steps.extend(extra_steps)

    shot_started = time.monotonic()
    capture_guest_screenshot(
        machine,
        env,
        screenshot_dest,
        tool=screenshot_tool_for_compositor(compositor),
        run=run,
        commands=recorded,
    )
    if qmp_dest is not None:
        extra_qmp_screendump(machine, qmp_dest)
    steps.append(
        {
            "name": "screenshot",
            "status": "pass",
            "seconds": round(time.monotonic() - shot_started, 1),
            "path": str(screenshot_dest),
        }
    )
    extras["screenshot"] = str(screenshot_dest)
    return steps, extras


def ensure_udiskie_config_dir_command(user: str) -> str:
    """Make ``~/.config/udiskie`` writable. Arch goldens may own ``~/.config`` as root."""
    cfg = f"/home/{user}/.config"
    udi = f"{cfg}/udiskie"
    return (
        f"sudo -n mkdir -p {shlex.quote(udi)} && "
        f"sudo -n chown -R {shlex.quote(user)}:{shlex.quote(user)} "
        f"{shlex.quote(cfg)}"
    )


def ensure_udiskie_command() -> str:
    """Install udiskie on Arch goldens that lack it; no-op when already present."""
    return (
        "if command -v udiskie >/dev/null 2>&1; then "
        "printf 'UDISKIE=ok\\n'; "
        "elif command -v pacman >/dev/null 2>&1; then "
        "sudo -n pacman -S --needed --noconfirm udiskie && "
        "printf 'UDISKIE=installed\\n'; "
        "else echo 'udiskie is not on PATH' >&2; exit 1; fi"
    )


def install_udiskie_handler_command(env: Mapping[str, str], user: str) -> str:
    dest = f"/home/{user}/{STRATA_BIN_REL}"
    return f"{env_prefix(env)} {shlex.quote(dest)} --install-udiskie-unlock"


def udiskie_hook_config_command() -> str:
    return (
        "cfg=\"${XDG_CONFIG_HOME:-$HOME/.config}/udiskie/config.yml\"; "
        "test -f \"$cfg\" && grep -q -- '--udiskie-hook' \"$cfg\" && "
        "printf 'HOOK_CONFIG=1\\n'"
    )


def luks_hotplug_command(
    *,
    case: str,
    bin_path: str | None = None,
    serial: str = LUKS_HOTPLUG_SERIAL,
) -> str:
    parts = [
        f"SMOKE_LUKS_CASE={shlex.quote(case)}",
        f"SMOKE_LUKS_SERIAL={shlex.quote(serial)}",
        f"SMOKE_HOOK_LOG={shlex.quote(GUEST_HOOK_LOG)}",
    ]
    if bin_path:
        parts.append(f"SMOKE_STRATA_BIN={shlex.quote(bin_path)}")
    parts.append("bash /tmp/smoke-luks-hotplug.sh")
    return " ".join(parts)


def run_luks_hotplug_steps(
    machine: Machine,
    *,
    guest: Guest,
    local_path: Path | str,
    screenshot_dest: Path,
    qmp_dest: Path | None = None,
    session_timeout: float = SESSION_TIMEOUT_S,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
    create_luks_fn: Callable[[Path], Path] | None = None,
    hotplug_fn: Callable[[Path], bool] | None = None,
) -> tuple[list[dict], dict]:
    """session → local Strata → udiskie hook → LUKS hotplug → screenshot."""
    from strataqemu.vm_live import (
        RUNTIME_DEPS_TIMEOUT_S,
        _install_from_local,
        ensure_runtime_deps_command,
        resolve_local_strata,
    )

    if not supports_luks_hotplug(guest):
        raise SessionSmokeError(LUKS_HOTPLUG_FAIL_CLOSED)
    compositor = compositor_process_name(guest)
    recorded = commands if commands is not None else []
    steps: list[dict] = []
    local = resolve_local_strata(local_path)

    started = time.monotonic()
    exports = run_session_step(
        machine,
        compositor=compositor,
        timeout=session_timeout,
        run=run,
        commands=recorded,
        sleep=sleep,
    )
    steps.append(
        {
            "name": "session",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "sid": exports.get("SESSION_ID"),
        }
    )
    env = session_env_from_exports(exports)

    install_started = time.monotonic()
    ssh_run(
        machine,
        ensure_runtime_deps_command(),
        timeout=RUNTIME_DEPS_TIMEOUT_S,
        check=True,
        run=run,
        commands=recorded,
    )
    _install_from_local(
        machine,
        guest=guest,
        local=local,
        run=run,
        commands=recorded,
        run_dir=machine.artifacts.root,
    )
    steps.append(
        {
            "name": "install",
            "status": "pass",
            "seconds": round(time.monotonic() - install_started, 1),
            "source": str(local.path),
        }
    )

    handler_started = time.monotonic()
    ssh_run(
        machine,
        ensure_udiskie_command(),
        timeout=INSTALL_TIMEOUT_S,
        check=True,
        run=run,
        commands=recorded,
    )
    ssh_run(
        machine,
        ensure_udiskie_config_dir_command(guest.user.name),
        timeout=15,
        check=True,
        run=run,
        commands=recorded,
    )
    ssh_run(
        machine,
        install_udiskie_handler_command(env, guest.user.name),
        timeout=30,
        check=True,
        run=run,
        commands=recorded,
    )
    cfg = ssh_run(
        machine,
        udiskie_hook_config_command(),
        timeout=15,
        check=False,
        run=run,
        commands=recorded,
    )
    try:
        hook_ok = parse_smoke_kv(f"{cfg.stdout}{cfg.stderr}", "HOOK_CONFIG")
    except SessionSmokeError:
        hook_ok = ""
    if hook_ok != "1":
        raise SessionSmokeError(LUKS_HOTPLUG_HOOK_MISSING)
    helper = smoke_luks_hotplug_script()
    if not helper.is_file():
        raise SessionSmokeError(f"missing luks-hotplug smoke {helper}")
    scp_to_guest(
        machine,
        helper,
        "/tmp/smoke-luks-hotplug.sh",
        run=run,
        commands=recorded,
    )
    wrap = ssh_run(
        machine,
        luks_hotplug_command(
            case="wrap",
            bin_path=f"/home/{guest.user.name}/{STRATA_BIN_REL}",
        ),
        timeout=15,
        check=True,
        run=run,
        commands=recorded,
    )
    try:
        wrapped = parse_smoke_kv(f"{wrap.stdout}{wrap.stderr}", "WRAPPED")
    except SessionSmokeError:
        wrapped = ""
    if wrapped != "1":
        raise SessionSmokeError("luks-hotplug: wrap did not replace the Strata binary")
    steps.append(
        {
            "name": "udiskie-handler",
            "status": "pass",
            "seconds": round(time.monotonic() - handler_started, 1),
        }
    )

    plug_started = time.monotonic()
    image = machine.artifacts.root / "luks.img"
    maker = create_luks_fn or create_luks_image
    try:
        image = Path(maker(image))
    except FileNotFoundError as exc:
        raise SessionSmokeError(LUKS_IMAGE_TOOLS_MISSING) from exc
    except RuntimeError as exc:
        raise SessionSmokeError(f"run-test: LUKS image failed: {exc}") from exc
    plugger = hotplug_fn or (
        lambda path: qmp_hotplug_raw_disk(machine.artifacts.qmp_sock, path)
    )
    if not plugger(image):
        raise SessionSmokeError(LUKS_HOTPLUG_QMP_FAILED)
    wait = ssh_run(
        machine,
        luks_hotplug_command(
            case="wait",
            bin_path=f"/home/{guest.user.name}/{STRATA_BIN_REL}",
        ),
        timeout=LUKS_HOTPLUG_TIMEOUT_S + 15,
        check=True,
        run=run,
        commands=recorded,
    )
    blob = f"{wait.stdout}{wait.stderr}"
    if parse_smoke_kv(blob, "STRATA_CALLED") != "1":
        raise SessionSmokeError(
            f"luks-hotplug: Strata was not called: {blob.strip()}"
        )
    hook_line = parse_smoke_kv(blob, "HOOK_LINE")
    luks_dev = parse_smoke_kv(blob, "LUKS_DEV")
    steps.append(
        {
            "name": "luks-hotplug",
            "status": "pass",
            "seconds": round(time.monotonic() - plug_started, 1),
            "device": luks_dev,
            "hook": hook_line,
        }
    )

    shot_started = time.monotonic()
    capture_guest_screenshot(
        machine,
        env,
        screenshot_dest,
        tool=screenshot_tool_for_compositor(compositor),
        run=run,
        commands=recorded,
    )
    if qmp_dest is not None:
        extra_qmp_screendump(machine, qmp_dest)
    steps.append(
        {
            "name": "screenshot",
            "status": "pass",
            "seconds": round(time.monotonic() - shot_started, 1),
            "path": str(screenshot_dest),
        }
    )
    extras = {
        "screenshot": str(screenshot_dest),
        "luks_device": luks_dev,
        "luks_hook": hook_line,
        "luks_source": str(local.path),
    }
    return steps, extras


def strata_version_is_cli(returncode: int, output: str) -> bool:
    """True when ``strata --version`` printed a version and did not open GTK."""
    del returncode
    line = ""
    for raw in output.splitlines():
        stripped = raw.strip()
        if stripped:
            line = stripped
            break
    if not line or not VERSION_CLI_RE.match(line):
        return False
    return True


def parse_observed_version(text: str) -> str:
    """First line of ``strata --version``. Not an installer-log parse."""
    line = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped:
            line = stripped
            break
    if not line:
        raise SessionSmokeError("strata --version produced empty stdout")
    line = re.sub(r"^strata\s+", "", line, flags=re.IGNORECASE)
    return _strip_v_prefix(line)


def _strip_v_prefix(value: str) -> str:
    text = value.strip()
    if len(text) > 1 and text[0] in "vV" and text[1].isdigit():
        return text[1:]
    return text


def normalize_version(value: str) -> str:
    return _strip_v_prefix(value.strip())


def versions_match(observed: str, intended: str) -> bool:
    return normalize_version(observed) == normalize_version(intended)


def parse_update_from_version(value: str) -> str:
    """Normalize ``--update-from VERSION``. Fail closed on empty or junk."""
    text = (value or "").strip()
    if not text:
        raise SessionSmokeError(UPDATE_FROM_MISSING_VERSION)
    normalized = normalize_version(text)
    if not re.match(r"\d+\.\d+", normalized):
        raise SessionSmokeError(f"{UPDATE_FROM_BAD_VERSION}: {value!r}")
    return normalized


def parse_update_from_versions(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated previous-version list.

    ``None`` or blank uses ``DEFAULT_UPDATE_FROM_VERSIONS``.
    """
    if raw is None or not str(raw).strip():
        return DEFAULT_UPDATE_FROM_VERSIONS
    versions: list[str] = []
    seen: set[str] = set()
    for part in str(raw).split(","):
        token = part.strip()
        if not token:
            continue
        ver = parse_update_from_version(token)
        if ver in seen:
            continue
        seen.add(ver)
        versions.append(ver)
    if not versions:
        raise SessionSmokeError(UPDATE_FROM_EMPTY_LIST)
    return tuple(versions)


def update_from_versions(
    *, environ: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Configured previous versions for ``--update-from``.

    Override with ``STRATA_QEMU_UPDATE_FROM`` (comma-separated).
    """
    env = os.environ if environ is None else environ
    return parse_update_from_versions(env.get(UPDATE_FROM_ENV))


def previous_release_archive_name(
    version: str, *, target: str = STRATA_RELEASE_TARGET
) -> str:
    return f"strata-{normalize_version(version)}-{target}.tar.gz"


def previous_release_url(
    version: str, *, target: str = STRATA_RELEASE_TARGET
) -> str:
    ver = normalize_version(version)
    name = previous_release_archive_name(ver, target=target)
    return f"{GITHUB_RELEASE_DOWNLOAD}/v{ver}/{name}"


def parse_archive_version(filename: str) -> str:
    """Version from ``strata-VERSION-x86_64-unknown-linux-gnu.tar.gz``."""
    name = Path(filename).name
    for ext in (".tar.gz", ".tar.xz", ".tgz", ".zip", ".tar"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    match = ARCHIVE_VERSION_RE.match(name)
    if not match:
        raise SessionSmokeError(
            f"cannot parse intended version from archive name {Path(filename).name!r}"
        )
    rest = match.group(1)
    for marker in _ARCHIVE_TARGET_MARKERS:
        idx = rest.lower().find(marker)
        if idx != -1:
            rest = rest[:idx]
            break
    else:
        rest = re.sub(r"-(x86_64|aarch64|arm64)$", "", rest, flags=re.IGNORECASE)
    rest = rest.strip()
    if not re.match(r"\d+\.\d+", rest):
        raise SessionSmokeError(
            f"cannot parse intended version from archive name {Path(filename).name!r}"
        )
    return rest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def github_latest_version(
    url: str = GITHUB_LATEST_RELEASE,
    *,
    opener: Callable[..., object] | None = None,
) -> str:
    """Resolve GitHub ``/releases/latest`` to a tag (no leading ``v``)."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "strata-qemu-testing",
        },
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=15) as resp:  # type: ignore[misc]
            body = resp.read()
            final = resp.geturl() if hasattr(resp, "geturl") else url
    except (OSError, urllib.error.URLError) as exc:
        raise SessionSmokeError(
            f"could not resolve latest Strata release from {url}: {exc}"
        ) from exc
    text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
    tag = ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            tag = str(data.get("tag_name") or data.get("tag") or "")
    except json.JSONDecodeError:
        tag = ""
    if not tag and final:
        tag = str(final).rstrip("/").rsplit("/", 1)[-1]
    if not tag or tag in {"latest", "releases"}:
        raise SessionSmokeError(
            f"could not resolve latest Strata release from {url}"
        )
    return normalize_version(tag)


def resolve_intended_version(
    *,
    install_from: str,
    archive_path: Path | None = None,
    intended_version: str | None = None,
    intended_version_fn: Callable[[], str] | None = None,
) -> str:
    if intended_version:
        return normalize_version(intended_version)
    if intended_version_fn is not None:
        return normalize_version(intended_version_fn())
    if install_from == "local-archive":
        if archive_path is None:
            raise SessionSmokeError("local-archive requires a host archive path")
        return parse_archive_version(str(archive_path))
    return github_latest_version()


def parse_install_sh_sha256(text: str) -> str:
    """Read ``INSTALL_SH_SHA256=`` from the Arch helper stdout."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith(INSTALL_SH_SHA256_PREFIX):
            continue
        digest = line.split("=", 1)[1].strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            return digest
    raise SessionSmokeError("install helper did not print INSTALL_SH_SHA256")


def hyprctl_class_oracle_command(env: Mapping[str, str] | None = None) -> str:
    jq_filter = f'.[] | select(.class == "{STRATA_BUS_NAME}")'
    body = f"hyprctl clients -j | jq -e {shlex.quote(jq_filter)}"
    if env:
        return f"{env_prefix(env)} {body}"
    return body


def launch_desktop_entry_command(env: Mapping[str, str]) -> str:
    """gtk-launch, else gio launch, in the graphical env. Not used by --session-only.

    The launcher is backgrounded: gtk-launch/gio wait for the GTK app to exit.
    """
    inner = (
        "if command -v gtk-launch >/dev/null 2>&1; then "
        f"nohup gtk-launch {STRATA_BUS_NAME} >/tmp/strata-launch.log 2>&1 & "
        "elif command -v gio >/dev/null 2>&1; then "
        f"nohup gio launch \"$HOME/.local/share/applications/{STRATA_DESKTOP_FILE}\" "
        ">/tmp/strata-launch.log 2>&1 & "
        "else echo 'gtk-launch and gio launch missing' >&2; exit 1; fi; "
        "disown || true; sleep 1; exit 0"
    )
    prefix = env_prefix(env)
    return f"{prefix} bash -lc {shlex.quote(inner)}"


def session_only_step_names() -> tuple[str, ...]:
    return SESSION_ONLY_STEPS


def command_is_forbidden_for_session_only(command: str) -> bool:
    return any(token in command for token in SESSION_ONLY_FORBIDDEN)


def _append_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")


def ssh_run(
    machine: Machine,
    command: str,
    *,
    timeout: float = 120,
    check: bool = False,
    run: RunFn | None = None,
    commands: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if commands is not None:
        commands.append(command)
    runner = run or subprocess.run
    argv = machine.ssh(command)
    log.debug("ssh: %s", command)
    try:
        proc = runner(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        _append_log(
            machine.artifacts.root / "ssh.log",
            f"$ {command}\n[timeout {timeout}s]\n{out}{err}",
        )
        raise
    blob = f"$ {command}\n[exit {proc.returncode}]\n{proc.stdout}{proc.stderr}"
    _append_log(machine.artifacts.root / "ssh.log", blob)
    if check and proc.returncode != 0:
        raise SessionSmokeError(
            f"ssh command failed ({proc.returncode}): {command}\n"
            f"{proc.stdout}{proc.stderr}"
        )
    return proc


def scp_to_guest(
    machine: Machine,
    source: Path,
    dest: str,
    *,
    run: RunFn | None = None,
    commands: list[str] | None = None,
) -> None:
    if machine.identity is None:
        raise SessionSmokeError("identity is required for scp")
    note = f"scp {source} {dest}"
    if commands is not None:
        commands.append(note)
    argv = scp_command(
        port=machine.ssh_port,
        identity=machine.identity,
        source=str(source),
        destination=dest,
        user=machine.user,
    )
    runner = run or subprocess.run
    proc = runner(argv, check=False, capture_output=True, text=True)
    _append_log(
        machine.artifacts.root / "ssh.log",
        f"$ {note}\n[exit {proc.returncode}]\n{proc.stdout}{proc.stderr}",
    )
    if proc.returncode != 0:
        raise SessionSmokeError(f"scp failed: {proc.stderr or proc.stdout}")


def scp_from_guest(
    machine: Machine,
    remote: str,
    dest: Path,
    *,
    run: RunFn | None = None,
    commands: list[str] | None = None,
) -> None:
    if machine.identity is None:
        raise SessionSmokeError("identity is required for scp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    note = f"scp {remote} {dest}"
    if commands is not None:
        commands.append(note)
    argv = scp_download_command(
        port=machine.ssh_port,
        identity=machine.identity,
        remote_path=remote,
        local_path=dest,
        user=machine.user,
    )
    runner = run or subprocess.run
    proc = runner(argv, check=False, capture_output=True, text=True)
    _append_log(
        machine.artifacts.root / "ssh.log",
        f"$ {note}\n[exit {proc.returncode}]\n{proc.stdout}{proc.stderr}",
    )
    if proc.returncode != 0:
        raise SessionSmokeError(
            f"scp download failed: {proc.stderr or proc.stdout}"
        )


def run_session_step(
    machine: Machine,
    *,
    compositor: str,
    timeout: float = SESSION_TIMEOUT_S,
    run: RunFn | None = None,
    script: Path | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, str]:
    """Upload and run ``smoke-session.sh`` until it passes or ``timeout``."""
    script_path = script if script is not None else smoke_session_script()
    if not script_path.is_file():
        raise SessionSmokeError(f"missing session smoke script {script_path}")
    scp_to_guest(
        machine, script_path, "/tmp/smoke-session.sh", run=run, commands=commands
    )
    remote = (
        f"SMOKE_COMPOSITOR={shlex.quote(compositor)} bash /tmp/smoke-session.sh"
    )
    deadline = time.monotonic() + timeout
    last = "no probe yet"
    nap = sleep or time.sleep
    while time.monotonic() < deadline:
        proc = ssh_run(
            machine, remote, timeout=30, run=run, commands=commands
        )
        blob = f"{proc.stdout}{proc.stderr}"
        if proc.returncode == 0:
            exports = parse_session_exports(proc.stdout)
            if "WAYLAND_DISPLAY" not in exports:
                raise SessionSmokeError(
                    f"session smoke produced no WAYLAND_DISPLAY: {blob}"
                )
            return exports
        if "x11" in blob.lower():
            raise SessionSmokeError(blob.strip() or "Type=x11 is a hard fail")
        last = blob.strip() or f"exit {proc.returncode}"
        nap(2)
    raise TimeoutError(f"session smoke failed in {timeout}s: {last}")


def capture_guest_screenshot(
    machine: Machine,
    env: Mapping[str, str],
    dest: Path,
    *,
    tool: str | None = None,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    vnc_capture_fn: Callable[..., bool] | None = None,
    screenshot_timeout: float = GNOME_SCREENSHOT_TIMEOUT_S,
) -> Path:
    """In-guest ``grim`` / ``gnome-screenshot``. Missing binary is a golden bug.

    GNOME 50 in QEMU denies ``org.gnome.Shell.Screenshot`` and the screenshot
    portal never completes; after the in-guest tool is confirmed on PATH,
    a timed-out ``gnome-screenshot`` falls back to the QEMU VNC framebuffer.
    """
    binary = tool or "gnome-screenshot"
    which = ssh_run(
        machine,
        f"command -v {shlex.quote(binary)}",
        timeout=15,
        run=run,
        commands=commands,
    )
    if screenshot_tool_missing(which.returncode):
        raise SessionSmokeError(SCREENSHOT_TOOL_MISSING)
    prefix = env_prefix(env)
    if binary == "grim":
        cmd = f"{prefix} grim {shlex.quote(GUEST_SCREENSHOT_REMOTE)}"
        ssh_run(
            machine, cmd, timeout=screenshot_timeout, check=True, run=run, commands=commands
        )
        scp_from_guest(
            machine, GUEST_SCREENSHOT_REMOTE, dest, run=run, commands=commands
        )
    else:
        cmd = f"{prefix} gnome-screenshot -f {shlex.quote(GUEST_SCREENSHOT_REMOTE)}"
        grabbed = False
        try:
            ssh_run(
                machine,
                cmd,
                timeout=screenshot_timeout,
                check=True,
                run=run,
                commands=commands,
            )
            scp_from_guest(
                machine, GUEST_SCREENSHOT_REMOTE, dest, run=run, commands=commands
            )
            grabbed = True
        except (subprocess.TimeoutExpired, SessionSmokeError):
            grabbed = False
        if not grabbed or not _png_ok(dest):
            grabber = vnc_capture_fn or vnc_framebuffer_png
            if machine.vnc_port is None or not grabber(
                dest, display=machine.vnc_port
            ):
                raise SessionSmokeError(
                    "gnome-screenshot did not produce a PNG; VNC fallback failed"
                )
    data = dest.read_bytes() if dest.is_file() else b""
    if len(data) < MIN_PNG_BYTES or not data.startswith(PNG_MAGIC):
        raise SessionSmokeError(
            f"in-guest screenshot is empty or not a PNG ({dest}, {len(data)} bytes)"
        )
    return dest


def _png_ok(dest: Path) -> bool:
    try:
        data = dest.read_bytes()
    except OSError:
        return False
    return len(data) >= MIN_PNG_BYTES and data.startswith(PNG_MAGIC)


def extra_qmp_screendump(machine: Machine, dest: Path) -> bool:
    """Best-effort QMP dump. ``no surface`` must not fail the step."""
    try:
        return qmp_screendump(machine.artifacts.qmp_sock, dest)
    except OSError as exc:
        log.debug("qmp screendump extra failed: %s", exc)
        return False


def wait_gnome_bus_name(
    machine: Machine,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 45,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Wait until the session bus owns ``io.github.lgse.Strata``.

    Not used by ``--session-only``.
    """
    remote = gdbus_name_has_owner_command(env)
    deadline = time.monotonic() + timeout
    last = "no probe yet"
    nap = sleep or time.sleep
    while time.monotonic() < deadline:
        proc = ssh_run(
            machine, remote, timeout=15, run=run, commands=commands
        )
        blob = f"{proc.stdout}{proc.stderr}"
        try:
            if parse_name_has_owner(blob):
                return
        except SessionSmokeError:
            last = blob.strip() or f"exit {proc.returncode}"
        else:
            last = blob.strip() or "NameHasOwner false"
        nap(1)
    raise TimeoutError(
        f"bus name {STRATA_BUS_NAME} not owned in {timeout}s: {last}"
    )


def run_gnome_desktop_oracle(
    machine: Machine,
    *,
    run: RunFn | None = None,
    script: Path | None = None,
    commands: list[str] | None = None,
) -> None:
    """Upload and run GNOME ``smoke-desktop.sh`` (bus name + screenshot)."""
    script_path = script if script is not None else smoke_desktop_script()
    if not script_path.is_file():
        raise SessionSmokeError(f"missing desktop smoke script {script_path}")
    scp_to_guest(
        machine, script_path, "/tmp/smoke-desktop.sh", run=run, commands=commands
    )
    proc = ssh_run(
        machine,
        "bash /tmp/smoke-desktop.sh",
        timeout=60,
        run=run,
        commands=commands,
    )
    blob = f"{proc.stdout}{proc.stderr}"
    if proc.returncode != 0:
        if SCREENSHOT_TOOL_MISSING in blob:
            raise SessionSmokeError(SCREENSHOT_TOOL_MISSING)
        raise SessionSmokeError(blob.strip() or "desktop oracle failed")


def run_session_only_steps(
    machine: Machine,
    *,
    compositor: str,
    screenshot_dest: Path,
    qmp_dest: Path | None = None,
    session_timeout: float = SESSION_TIMEOUT_S,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[dict]:
    """Session + screenshot only. Does not launch Strata or wait on its bus."""
    recorded = commands if commands is not None else []
    steps: list[dict] = []
    started = time.monotonic()
    exports = run_session_step(
        machine,
        compositor=compositor,
        timeout=session_timeout,
        run=run,
        commands=recorded,
        sleep=sleep,
    )
    steps.append(
        {
            "name": "session",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "sid": exports.get("SESSION_ID"),
        }
    )
    env = session_env_from_exports(exports)
    shot_started = time.monotonic()
    capture_guest_screenshot(
        machine,
        env,
        screenshot_dest,
        tool=screenshot_tool_for_compositor(compositor),
        run=run,
        commands=recorded,
    )
    if qmp_dest is not None:
        extra_qmp_screendump(machine, qmp_dest)
    steps.append(
        {
            "name": "screenshot",
            "status": "pass",
            "seconds": round(time.monotonic() - shot_started, 1),
            "path": str(screenshot_dest),
        }
    )
    for command in recorded:
        if command_is_forbidden_for_session_only(command):
            raise SessionSmokeError(
                f"--session-only invoked a forbidden command: {command}"
            )
    return steps


def assert_session_only_commands(commands: Sequence[str]) -> None:
    """Fail if any recorded guest command is outside the session-only contract."""
    for command in commands:
        if command_is_forbidden_for_session_only(command):
            raise SessionSmokeError(
                f"--session-only invoked a forbidden command: {command}"
            )


def wait_hyprland_class(
    machine: Machine,
    env: Mapping[str, str],
    *,
    timeout: float = WINDOW_TIMEOUT_S,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Wait until ``hyprctl clients`` lists class ``io.github.lgse.Strata``."""
    remote = hyprctl_class_oracle_command(env)
    deadline = time.monotonic() + timeout
    last = "no probe yet"
    nap = sleep or time.sleep
    while time.monotonic() < deadline:
        proc = ssh_run(
            machine, remote, timeout=15, run=run, commands=commands
        )
        blob = f"{proc.stdout}{proc.stderr}"
        if proc.returncode == 0:
            return
        last = blob.strip() or f"exit {proc.returncode}"
        nap(1)
    raise TimeoutError(
        f"hyprctl class {STRATA_BUS_NAME} not present in {timeout}s: {last}"
    )


def _desktop_entry_probe_command() -> str:
    path = f"$HOME/.local/share/applications/{STRATA_DESKTOP_FILE}"
    return f"test -f {path} && grep -E '^Exec=' {path}"


def _ensure_screenshot_tool(
    machine: Machine,
    tool: str,
    *,
    run: RunFn | None = None,
    commands: list[str] | None = None,
) -> None:
    which = ssh_run(
        machine,
        f"command -v {shlex.quote(tool)}",
        timeout=15,
        run=run,
        commands=commands,
    )
    if screenshot_tool_missing(which.returncode):
        raise SessionSmokeError(SCREENSHOT_TOOL_MISSING)


def launch_strata(
    machine: Machine,
    env: Mapping[str, str],
    *,
    run: RunFn | None = None,
    commands: list[str] | None = None,
) -> None:
    ssh_run(
        machine,
        launch_desktop_entry_command(env),
        timeout=30,
        check=True,
        run=run,
        commands=commands,
    )


def wait_strata_window(
    machine: Machine,
    env: Mapping[str, str],
    compositor: str,
    *,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> str:
    """Wait until Strata's window is up. Returns the oracle name."""
    if compositor == "Hyprland":
        wait_hyprland_class(
            machine,
            env,
            timeout=WINDOW_TIMEOUT_S,
            run=run,
            commands=commands,
            sleep=sleep,
        )
        return "hyprctl-class"
    wait_gnome_bus_name(
        machine,
        env=env,
        timeout=WINDOW_TIMEOUT_S,
        run=run,
        commands=commands,
        sleep=sleep,
    )
    return "bus-name"


def quit_strata(
    machine: Machine,
    env: Mapping[str, str],
    *,
    run: RunFn | None = None,
    commands: list[str] | None = None,
) -> None:
    """Stop the running Strata process so a later update can relaunch it."""
    remote = f"{env_prefix(env)} sh -c 'pkill -x strata || true'"
    ssh_run(machine, remote, timeout=15, run=run, commands=commands)


def _send_qmp_chords(
    machine: Machine,
    chords: Sequence[Sequence[str]],
    *,
    send_key: Callable[..., bool] | None = None,
    sleep: Callable[[float], None] | None = None,
    pause_s: float = 0.05,
) -> bool:
    sender = send_key or qmp_send_key
    nap = sleep or time.sleep
    sock = machine.artifacts.qmp_sock
    for chord in chords:
        if not sender(sock, list(chord)):
            return False
        nap(pause_s)
    return True


def run_about_version_step(
    machine: Machine,
    *,
    env: Mapping[str, str],
    compositor: str,
    screenshot_dest: Path,
    cli_version_passed: bool,
    intended: str,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
    send_key: Callable[..., bool] | None = None,
    name: str = "about-version",
) -> dict:
    """Open Settings → About and screenshot. Skip when CLI version already passed."""
    started = time.monotonic()
    if cli_version_passed:
        return {
            "name": name,
            "status": "skip",
            "seconds": round(time.monotonic() - started, 1),
            "reason": ABOUT_CLI_ALREADY_RECORDED,
        }
    recorded = commands if commands is not None else []
    nap = sleep or time.sleep
    tool = screenshot_tool_for_compositor(compositor)
    helper = smoke_about_script()
    if not helper.is_file():
        raise SessionSmokeError(f"missing about smoke script {helper}")
    scp_to_guest(
        machine, helper, "/tmp/smoke-about.sh", run=run, commands=recorded
    )
    about_cmd = about_smoke_command(compositor=compositor)
    if env:
        about_cmd = f"{env_prefix(env)} {about_cmd}"
    proc = ssh_run(
        machine,
        about_cmd,
        timeout=30,
        check=False,
        run=run,
        commands=recorded,
    )
    blob = f"{proc.stdout}{proc.stderr}"
    try:
        injected = parse_smoke_kv(blob, "INPUT")
    except SessionSmokeError:
        injected = ""
    used = ""
    if proc.returncode == 0 and injected == "wtype":
        used = "wtype"
        nap(ABOUT_PAGE_SETTLE_S)
    elif proc.returncode == 0 and injected == "hyprctl-open":
        nap(ABOUT_OPEN_SETTLE_S)
        if _send_qmp_chords(
            machine,
            qmp_about_nav_chords(),
            send_key=send_key,
            sleep=sleep,
            pause_s=ABOUT_NAV_SETTLE_S,
        ):
            used = "hyprctl+qmp"
            nap(ABOUT_PAGE_SETTLE_S)
    if not used:
        if _send_qmp_chords(
            machine, [["ctrl", "comma"]], send_key=send_key, sleep=sleep
        ):
            nap(ABOUT_OPEN_SETTLE_S)
            if _send_qmp_chords(
                machine,
                qmp_about_nav_chords(),
                send_key=send_key,
                sleep=sleep,
                pause_s=ABOUT_NAV_SETTLE_S,
            ):
                used = "qmp"
                nap(ABOUT_PAGE_SETTLE_S)

    if not used:
        return {
            "name": name,
            "status": "skip",
            "seconds": round(time.monotonic() - started, 1),
            "reason": ABOUT_INPUT_FAILED,
            "intended": intended,
        }

    capture_guest_screenshot(
        machine,
        env,
        screenshot_dest,
        tool=tool,
        run=run,
        commands=recorded,
    )
    return {
        "name": name,
        "status": "pass",
        "seconds": round(time.monotonic() - started, 1),
        "oracle": "settings-about",
        "input": used,
        "path": str(screenshot_dest),
        "intended": intended,
        "nav": f"tab×{ABOUT_SIDEBAR_TABS}+space",
    }


def run_version_oracle(
    machine: Machine,
    *,
    intended: str,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    name: str = "version",
) -> tuple[dict, str | None]:
    """``strata --version`` vs ``intended``. Skip when it is not a CLI."""
    started = time.monotonic()
    observed: str | None = None
    version_status = "skip"
    version_reason = "strata --version is not a CLI"
    try:
        ver = ssh_run(
            machine,
            STRATA_VERSION_COMMAND,
            timeout=VERSION_TIMEOUT_S,
            check=False,
            run=run,
            commands=commands,
        )
        if strata_version_is_cli(ver.returncode, ver.stdout):
            if ver.returncode != 0:
                raise SessionSmokeError(
                    f"ssh command failed ({ver.returncode}): "
                    f"{STRATA_VERSION_COMMAND}\n{ver.stdout}{ver.stderr}"
                )
            observed = parse_observed_version(ver.stdout)
            if not versions_match(observed, intended):
                raise SessionSmokeError(
                    f"version mismatch: intended {intended}, observed {observed} "
                    f"(from {STRATA_VERSION_COMMAND})"
                )
            version_status = "pass"
            version_reason = ""
    except subprocess.TimeoutExpired:
        version_status = "skip"
        version_reason = "strata --version is not a CLI"
    version_step: dict = {
        "name": name,
        "status": version_status,
        "seconds": round(time.monotonic() - started, 1),
    }
    if version_status == "pass":
        version_step.update(
            {
                "oracle": "strata --version",
                "intended": intended,
                "observed": observed,
            }
        )
    else:
        version_step["reason"] = version_reason
    return version_step, observed


def run_install_from_release_steps(
    machine: Machine,
    *,
    guest: Guest,
    screenshot_dest: Path,
    qmp_dest: Path | None = None,
    session_timeout: float = SESSION_TIMEOUT_S,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
    install_from: str = "release",
    archive_path: Path | str | None = None,
    intended_version: str | None = None,
    intended_version_fn: Callable[[], str] | None = None,
    send_key: Callable[..., bool] | None = None,
) -> tuple[list[dict], dict]:
    """session → install → version → desktop-entry → window → about-version."""
    if not supports_install_from_release(guest):
        raise SessionSmokeError(
            f"--install-from {install_from} is not implemented for {guest.id}"
        )
    if install_from not in {"release", "local-archive"}:
        raise SessionSmokeError(
            f"--install-from {install_from} is not implemented for {guest.id}"
        )
    archive: Path | None = Path(archive_path) if archive_path is not None else None
    if install_from == "local-archive":
        if archive is None:
            raise SessionSmokeError(LOCAL_ARCHIVE_MISSING_PATH)
        if not archive.is_file():
            raise SessionSmokeError(f"run-test: archive not found: {archive}")

    intended = resolve_intended_version(
        install_from=install_from,
        archive_path=archive,
        intended_version=intended_version,
        intended_version_fn=intended_version_fn,
    )

    compositor = compositor_process_name(guest)
    tool = screenshot_tool_for_compositor(compositor)
    recorded = commands if commands is not None else []
    steps: list[dict] = []

    started = time.monotonic()
    exports = run_session_step(
        machine,
        compositor=compositor,
        timeout=session_timeout,
        run=run,
        commands=recorded,
        sleep=sleep,
    )
    steps.append(
        {
            "name": "session",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "sid": exports.get("SESSION_ID"),
        }
    )
    env = session_env_from_exports(exports)

    started = time.monotonic()
    helper = smoke_install_script()
    if not helper.is_file():
        raise SessionSmokeError(f"missing install smoke script {helper}")
    scp_to_guest(
        machine, helper, "/tmp/smoke-install.sh", run=run, commands=recorded
    )
    archive_digest: str | None = None
    remote_archive: str | None = None
    if archive is not None:
        archive_digest = sha256_file(archive)
        remote_archive = GUEST_ARCHIVE_REMOTE
        scp_to_guest(
            machine, archive, remote_archive, run=run, commands=recorded
        )
    proc = ssh_run(
        machine,
        install_smoke_command(
            archive=remote_archive,
            forbid_omarchy=guest.id == "arch",
        ),
        timeout=INSTALL_TIMEOUT_S,
        check=True,
        run=run,
        commands=recorded,
    )
    digest = parse_install_sh_sha256(f"{proc.stdout}{proc.stderr}")
    ssh_run(
        machine,
        f"test -x \"$HOME/{STRATA_BIN_REL}\"",
        timeout=15,
        check=True,
        run=run,
        commands=recorded,
    )
    steps.append(
        {
            "name": "install",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "install_sh_sha256": digest,
        }
    )

    version_step, observed = run_version_oracle(
        machine,
        intended=intended,
        run=run,
        commands=recorded,
    )
    steps.append(version_step)

    started = time.monotonic()
    entry = ssh_run(
        machine,
        _desktop_entry_probe_command(),
        timeout=DESKTOP_ENTRY_TIMEOUT_S,
        check=True,
        run=run,
        commands=recorded,
    )
    exec_blob = f"{entry.stdout}{entry.stderr}"
    if STRATA_BIN_REL not in exec_blob:
        raise SessionSmokeError(
            f"desktop Exec= does not point at ~/{STRATA_BIN_REL}: {exec_blob}"
        )
    steps.append(
        {
            "name": "desktop-entry",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
        }
    )

    started = time.monotonic()
    which = ssh_run(
        machine,
        f"command -v {shlex.quote(tool)}",
        timeout=15,
        run=run,
        commands=recorded,
    )
    if screenshot_tool_missing(which.returncode):
        raise SessionSmokeError(SCREENSHOT_TOOL_MISSING)
    ssh_run(
        machine,
        launch_desktop_entry_command(env),
        timeout=30,
        check=True,
        run=run,
        commands=recorded,
    )
    window_error: BaseException | None = None
    window_oracle = "hyprctl-class" if compositor == "Hyprland" else "bus-name"
    try:
        if compositor == "Hyprland":
            wait_hyprland_class(
                machine,
                env,
                timeout=WINDOW_TIMEOUT_S,
                run=run,
                commands=recorded,
                sleep=sleep,
            )
        else:
            wait_gnome_bus_name(
                machine,
                env=env,
                timeout=WINDOW_TIMEOUT_S,
                run=run,
                commands=recorded,
                sleep=sleep,
            )
    except (TimeoutError, SessionSmokeError) as exc:
        window_error = exc
    capture_guest_screenshot(
        machine,
        env,
        screenshot_dest,
        tool=tool,
        run=run,
        commands=recorded,
    )
    if qmp_dest is not None:
        extra_qmp_screendump(machine, qmp_dest)
    if window_error is not None:
        raise window_error
    steps.append(
        {
            "name": "window",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "path": str(screenshot_dest),
            "oracle": window_oracle,
        }
    )
    about_dest = screenshot_dest.with_name(ABOUT_VERSION_PNG_NAME)
    about_step = run_about_version_step(
        machine,
        env=env,
        compositor=compositor,
        screenshot_dest=about_dest,
        cli_version_passed=version_step.get("status") == "pass",
        intended=intended,
        run=run,
        commands=recorded,
        sleep=sleep,
        send_key=send_key,
    )
    steps.append(about_step)
    extras: dict = {
        "install_method": "install.sh",
        "install_sh_sha256": digest,
        "intended_version": intended,
        "screenshot": str(screenshot_dest),
    }
    if observed is not None:
        extras["observed_version"] = observed
    if archive_digest is not None:
        extras["archive_sha256"] = archive_digest
    if about_step.get("status") == "pass":
        extras["about_version"] = about_step.get("path")
    return steps, extras


def run_update_from_steps(
    machine: Machine,
    *,
    guest: Guest,
    from_version: str,
    screenshot_dest: Path,
    qmp_dest: Path | None = None,
    session_timeout: float = SESSION_TIMEOUT_S,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
    intended_version: str | None = None,
    intended_version_fn: Callable[[], str] | None = None,
    previous_archive: Path | str | None = None,
    send_key: Callable[..., bool] | None = None,
) -> tuple[list[dict], dict]:
    """session → previous → version-previous → update → version → desktop → window → about-version."""
    if not supports_update_from(guest):
        raise SessionSmokeError(UPDATE_FROM_FAIL_CLOSED)
    from_ver = parse_update_from_version(from_version)
    archive: Path | None = (
        Path(previous_archive) if previous_archive is not None else None
    )
    if archive is not None and not archive.is_file():
        raise SessionSmokeError(f"run-test: previous archive not found: {archive}")
    intended = resolve_intended_version(
        install_from="release",
        intended_version=intended_version,
        intended_version_fn=intended_version_fn,
    )
    if versions_match(from_ver, intended):
        raise SessionSmokeError(f"{UPDATE_FROM_SAME_AS_LATEST} ({intended})")

    compositor = compositor_process_name(guest)
    tool = screenshot_tool_for_compositor(compositor)
    recorded = commands if commands is not None else []
    steps: list[dict] = []

    started = time.monotonic()
    exports = run_session_step(
        machine,
        compositor=compositor,
        timeout=session_timeout,
        run=run,
        commands=recorded,
        sleep=sleep,
    )
    steps.append(
        {
            "name": "session",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "sid": exports.get("SESSION_ID"),
        }
    )
    env = session_env_from_exports(exports)

    helper = smoke_update_script()
    if not helper.is_file():
        raise SessionSmokeError(f"missing update smoke script {helper}")
    scp_to_guest(
        machine, helper, "/tmp/smoke-update.sh", run=run, commands=recorded
    )
    remote_archive: str | None = None
    archive_digest: str | None = None
    if archive is not None:
        archive_digest = sha256_file(archive)
        remote_archive = GUEST_ARCHIVE_REMOTE
        scp_to_guest(
            machine, archive, remote_archive, run=run, commands=recorded
        )

    started = time.monotonic()
    prev = ssh_run(
        machine,
        update_smoke_command(
            phase="previous",
            from_version=from_ver,
            archive=remote_archive,
            forbid_omarchy=guest.id == "arch",
        ),
        timeout=INSTALL_TIMEOUT_S,
        check=True,
        run=run,
        commands=recorded,
    )
    seeded = parse_smoke_kv(f"{prev.stdout}{prev.stderr}", "FROM_VERSION")
    if not versions_match(seeded, from_ver):
        raise SessionSmokeError(
            f"install-previous: FROM_VERSION {seeded!r}, expected {from_ver}"
        )
    ssh_run(
        machine,
        f"test -x \"$HOME/{STRATA_BIN_REL}\"",
        timeout=15,
        check=True,
        run=run,
        commands=recorded,
    )
    steps.append(
        {
            "name": "install-previous",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "from_version": from_ver,
            "archive": previous_release_archive_name(from_ver),
        }
    )

    prev_version_step, prev_observed = run_version_oracle(
        machine,
        intended=from_ver,
        run=run,
        commands=recorded,
        name="version-previous",
    )
    steps.append(prev_version_step)

    started = time.monotonic()
    _ensure_screenshot_tool(machine, tool, run=run, commands=recorded)
    launch_strata(machine, env, run=run, commands=recorded)
    wait_strata_window(
        machine,
        env,
        compositor,
        run=run,
        commands=recorded,
        sleep=sleep,
    )
    before_dest = screenshot_dest.with_name(ABOUT_BEFORE_PNG_NAME)
    before_step = run_about_version_step(
        machine,
        env=env,
        compositor=compositor,
        screenshot_dest=before_dest,
        cli_version_passed=False,
        intended=from_ver,
        run=run,
        commands=recorded,
        sleep=sleep,
        send_key=send_key,
        name="about-version-before",
    )
    steps.append(before_step)
    quit_strata(machine, env, run=run, commands=recorded)
    before_step["seconds"] = round(time.monotonic() - started, 1)

    started = time.monotonic()
    latest = ssh_run(
        machine,
        update_smoke_command(
            phase="latest",
            forbid_omarchy=guest.id == "arch",
        ),
        timeout=INSTALL_TIMEOUT_S,
        check=True,
        run=run,
        commands=recorded,
    )
    digest = parse_install_sh_sha256(f"{latest.stdout}{latest.stderr}")
    ssh_run(
        machine,
        f"test -x \"$HOME/{STRATA_BIN_REL}\"",
        timeout=15,
        check=True,
        run=run,
        commands=recorded,
    )
    steps.append(
        {
            "name": "update",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "install_sh_sha256": digest,
            "install_method": "install.sh",
        }
    )

    version_step, observed = run_version_oracle(
        machine,
        intended=intended,
        run=run,
        commands=recorded,
    )
    steps.append(version_step)

    started = time.monotonic()
    entry = ssh_run(
        machine,
        _desktop_entry_probe_command(),
        timeout=DESKTOP_ENTRY_TIMEOUT_S,
        check=True,
        run=run,
        commands=recorded,
    )
    exec_blob = f"{entry.stdout}{entry.stderr}"
    if STRATA_BIN_REL not in exec_blob:
        raise SessionSmokeError(
            f"desktop Exec= does not point at ~/{STRATA_BIN_REL}: {exec_blob}"
        )
    steps.append(
        {
            "name": "desktop-entry",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
        }
    )

    started = time.monotonic()
    which = ssh_run(
        machine,
        f"command -v {shlex.quote(tool)}",
        timeout=15,
        run=run,
        commands=recorded,
    )
    if screenshot_tool_missing(which.returncode):
        raise SessionSmokeError(SCREENSHOT_TOOL_MISSING)
    ssh_run(
        machine,
        launch_desktop_entry_command(env),
        timeout=30,
        check=True,
        run=run,
        commands=recorded,
    )
    window_error: BaseException | None = None
    window_oracle = "hyprctl-class" if compositor == "Hyprland" else "bus-name"
    try:
        if compositor == "Hyprland":
            wait_hyprland_class(
                machine,
                env,
                timeout=WINDOW_TIMEOUT_S,
                run=run,
                commands=recorded,
                sleep=sleep,
            )
        else:
            wait_gnome_bus_name(
                machine,
                env=env,
                timeout=WINDOW_TIMEOUT_S,
                run=run,
                commands=recorded,
                sleep=sleep,
            )
    except (TimeoutError, SessionSmokeError) as exc:
        window_error = exc
    capture_guest_screenshot(
        machine,
        env,
        screenshot_dest,
        tool=tool,
        run=run,
        commands=recorded,
    )
    if qmp_dest is not None:
        extra_qmp_screendump(machine, qmp_dest)
    if window_error is not None:
        raise window_error
    steps.append(
        {
            "name": "window",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "path": str(screenshot_dest),
            "oracle": window_oracle,
        }
    )
    after_dest = screenshot_dest.with_name(ABOUT_AFTER_PNG_NAME)
    after_step = run_about_version_step(
        machine,
        env=env,
        compositor=compositor,
        screenshot_dest=after_dest,
        cli_version_passed=False,
        intended=intended,
        run=run,
        commands=recorded,
        sleep=sleep,
        send_key=send_key,
        name="about-version-after",
    )
    steps.append(after_step)
    extras: dict = {
        "install_method": "install.sh",
        "install_sh_sha256": digest,
        "from_version": from_ver,
        "intended_version": intended,
        "screenshot": str(screenshot_dest),
        "update_from": from_ver,
    }
    if prev_observed is not None:
        extras["observed_previous_version"] = prev_observed
    if observed is not None:
        extras["observed_version"] = observed
    if archive_digest is not None:
        extras["archive_sha256"] = archive_digest
    if before_step.get("status") == "pass":
        extras["about_version_before"] = before_step.get("path")
    if after_step.get("status") == "pass":
        extras["about_version_after"] = after_step.get("path")
    return steps, extras
