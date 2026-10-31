"""``vm-live``: interactive overlay with Strata installed and sample files.

Boots a throwaway overlay like ``vm-run``, installs a tagged GitHub release
or a host binary/archive, seeds ``$HOME/fixtures``, and leaves QEMU running.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from strataqemu import artifacts, config
from strataqemu.cli import CheckHostResult, check_host
from strataqemu.guest import Guest
from strataqemu.image_build import ImageBuildError, wait_ssh
from strataqemu.qemu import Machine
from strataqemu.run_test import (
    SETTLE_S,
    CreateOverlayFn,
    PopenFn,
    RunFn,
    RunTestError,
    _load_or_usage,
    _prepare_overlay,
    _shutdown_machine,
    _spawn_overlay_vm,
    choose_graphical_ui,
    require_golden,
)
from strataqemu.sample_tree import (
    GUEST_FIXTURES_REL,
    GUEST_FIXTURES_TAR_REMOTE,
    build_fixtures_archive,
)
from strataqemu.tests_spec import (
    APPARMOR_BWRAP_RESTRICT_UNSUPPORTED,
    GUEST_ARCHIVE_REMOTE,
    INSTALL_TIMEOUT_S,
    SESSION_TIMEOUT_S,
    STRATA_BIN_REL,
    STRATA_DESKTOP_FILE,
    SessionSmokeError,
    compositor_process_name,
    env_prefix,
    normalize_version,
    parse_archive_version,
    parse_smoke_kv,
    run_apparmor_bwrap_restrict_step,
    run_session_step,
    scp_to_guest,
    session_env_from_exports,
    smoke_update_script,
    ssh_run,
    supports_apparmor_bwrap_restrict,
    supports_install_from_release,
    update_smoke_command,
)

log = logging.getLogger("strataqemu")

GUEST_BINARY_REMOTE = "/tmp/strata-bin"
GUEST_DESKTOP_REMOTE = f"/tmp/{STRATA_DESKTOP_FILE}"
RUNTIME_DEPS_TIMEOUT_S = 300
# Same set as lgse/strata install.sh REQUIRED_PACKAGES. The arch golden does
# not ship GTK; Omarchy and GNOME guests already have it.
ARCH_RUNTIME_PACKAGES = (
    "bubblewrap",
    "desktop-file-utils",
    "ffmpeg",
    "ffmpegthumbnailer",
    "fontconfig",
    "gst-libav",
    "gstreamer",
    "gst-plugins-base",
    "gst-plugins-good",
    "gtk4",
    "gtksourceview5",
    "gvfs",
    "poppler-glib",
    "xdg-utils",
)

# GNOME blanks and locks an idle session within minutes. A hidden Strata
# window requests no thumbnails, so a live session must stay awake.
GNOME_IDLE_SETTINGS = (
    ("org.gnome.desktop.session", "idle-delay", "0"),
    ("org.gnome.desktop.screensaver", "lock-enabled", "false"),
    ("org.gnome.settings-daemon.plugins.power", "idle-dim", "false"),
)

VM_LIVE_SOURCE_REQUIRED = (
    "vm-live: pass --from-tag VERSION or --from-local PATH"
)
VM_LIVE_SOURCE_EXCLUSIVE = (
    "vm-live: --from-tag and --from-local cannot be combined"
)
VM_LIVE_FAIL_CLOSED = (
    "vm-live: installing Strata is not supported for this guest"
)
VM_LIVE_LOCAL_MISSING = "vm-live: --from-local path not found"
VM_LIVE_LOCAL_EMPTY = (
    "vm-live: --from-local path has no strata binary or archive"
)
VM_LIVE_TAG_MISSING = (
    "vm-live: --from-tag requires a release tag (e.g. 0.15.0)"
)
VM_LIVE_TAG_BAD = "vm-live: --from-tag is not a Strata release tag"
VM_LIVE_USAGE = (
    "usage: python -m strataqemu vm-live "
    "(--from-tag VERSION | --from-local PATH) "
    "[--graphical] [--headless] [--apparmor-bwrap-restrict] [--keep] <guest>"
)


@dataclass(frozen=True)
class LocalStrata:
    """A host binary or release tarball to copy into the guest."""

    kind: str
    path: Path


def disable_gnome_idle_lock_command(env: Mapping[str, str]) -> str:
    """Keep the GNOME session awake and unlocked for a human operator."""
    settings = " && ".join(
        f"gsettings set {schema} {key} {value}"
        for schema, key, value in GNOME_IDLE_SETTINGS
    )
    return (
        f"{env_prefix(env)} bash -c {shlex.quote(settings)} "
        "&& sudo -n loginctl unlock-sessions"
    )


def parse_from_tag(value: str | None) -> str:
    """Normalize ``--from-tag VERSION``. Fail closed on empty or junk."""
    text = (value or "").strip()
    if not text:
        raise SessionSmokeError(VM_LIVE_TAG_MISSING)
    normalized = normalize_version(text)
    if not re.match(r"\d+\.\d+", normalized):
        raise SessionSmokeError(f"{VM_LIVE_TAG_BAD}: {value!r}")
    return normalized


def resolve_local_strata(path: Path | str) -> LocalStrata:
    """Locate a Strata binary or ``.tar.gz`` under ``path``."""
    raw = Path(path).expanduser()
    resolved = raw.resolve() if raw.exists() else raw
    if not resolved.exists():
        raise SessionSmokeError(f"{VM_LIVE_LOCAL_MISSING}: {path}")
    if resolved.is_file():
        name = resolved.name.lower()
        if name.endswith(".tar.gz") or name.endswith(".tgz"):
            return LocalStrata(kind="archive", path=resolved)
        return LocalStrata(kind="binary", path=resolved)
    if not resolved.is_dir():
        raise SessionSmokeError(f"{VM_LIVE_LOCAL_MISSING}: {path}")

    candidates = (
        resolved / "strata",
        resolved / "target" / "release" / "strata",
        resolved / "target" / "debug" / "strata",
    )
    for cand in candidates:
        if cand.is_file():
            return LocalStrata(kind="binary", path=cand)

    archives = sorted(resolved.glob("strata-*.tar.gz")) + sorted(
        resolved.glob("strata-*.tgz")
    )
    if archives:
        return LocalStrata(kind="archive", path=archives[-1])
    raise SessionSmokeError(f"{VM_LIVE_LOCAL_EMPTY}: {path}")


def guest_fixtures_dir(user: str) -> str:
    return f"/home/{user}/{GUEST_FIXTURES_REL}"


def _desktop_entry_text(exec_path: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Strata\n"
        "GenericName=File Manager\n"
        f"Exec={exec_path} %U\n"
        "Terminal=false\n"
        "StartupNotify=true\n"
        f"StartupWMClass=io.github.lgse.Strata\n"
    )


def install_binary_commands(user: str) -> str:
    dest = f"/home/{user}/{STRATA_BIN_REL}"
    desktop_dir = f"/home/{user}/.local/share/applications"
    dest_q = shlex.quote(dest)
    return (
        f"mkdir -p {shlex.quote(str(Path(dest).parent))} "
        f"{shlex.quote(desktop_dir)} && "
        f"install -Dm755 {shlex.quote(GUEST_BINARY_REMOTE)} {dest_q} && "
        f"install -Dm644 {shlex.quote(GUEST_DESKTOP_REMOTE)} "
        f"{shlex.quote(desktop_dir + '/' + STRATA_DESKTOP_FILE)} && "
        f"test -x {dest_q}"
    )


def extract_fixtures_command(user: str) -> str:
    dest = guest_fixtures_dir(user)
    dest_q = shlex.quote(dest)
    return (
        f"mkdir -p {dest_q} && "
        f"tar -xzf {shlex.quote(GUEST_FIXTURES_TAR_REMOTE)} -C {dest_q} && "
        f"test -f {shlex.quote(dest + '/readme.md')}"
    )


def ensure_runtime_deps_command() -> str:
    """Install GTK runtime packages on Arch-based guests that lack gtk4.

    ``--from-tag`` / ``--from-local`` copy a binary and skip ``install.sh``,
    so a greetd/Hyprland arch golden cannot start Strata until gtk4 is
    present. Omarchy and GNOME guests already have it; the ``pacman -Q``
    check is a no-op there.
    """
    packages = " ".join(shlex.quote(p) for p in ARCH_RUNTIME_PACKAGES)
    return (
        "if command -v pacman >/dev/null 2>&1 "
        "&& ! pacman -Q gtk4 >/dev/null 2>&1; then "
        f"sudo -n pacman -S --needed --noconfirm {packages}; "
        "printf 'RUNTIME_DEPS=installed\\n'; "
        "else printf 'RUNTIME_DEPS=ok\\n'; fi"
    )


def hyprland_exec_lua(command: str) -> str:
    """Lua dispatcher for Hyprland 0.55+ ``hyprctl dispatch``.

    ``hyprctl dispatch exec /path`` is spliced into
    ``return hl.dispatch(exec /path)``, which is invalid Lua (the ``.`` in
    ``.local`` is a syntax error). Pass a real dispatcher table instead.
    """
    return f"hl.dsp.exec_cmd({json.dumps(command)})"


def launch_strata_at_command(
    env: dict[str, str],
    directory: str,
    *,
    compositor: str,
    user: str,
) -> str:
    """Open Strata on ``directory`` in the graphical session.

    Hyprland 0.55+ (arch golden) must spawn via ``hl.dsp.exec_cmd``. Legacy
    ``hyprctl dispatch exec PATH`` is parsed as Lua and dies on ``.local``.
    Fall back to ``hl.exec_cmd`` then ``nohup`` for older compositors.
    """
    prefix = env_prefix(env)
    binary = f"/home/{user}/{STRATA_BIN_REL}"
    quoted_bin = shlex.quote(binary)
    quoted_dir = shlex.quote(directory)
    payload = f"{binary} {directory}"
    if compositor == "Hyprland":
        lua = hyprland_exec_lua(payload)
        eval_lua = f"hl.exec_cmd({json.dumps(payload)})"
        inner = (
            f"hyprctl dispatch {shlex.quote(lua)} "
            f"|| hyprctl eval {shlex.quote(eval_lua)} "
            f"|| {{ nohup {quoted_bin} {quoted_dir} "
            ">/tmp/strata-launch.log 2>&1 & disown || true; }"
        )
        return f"{prefix} bash -c {shlex.quote(inner)}"
    inner = (
        f"nohup {quoted_bin} {quoted_dir} "
        ">/tmp/strata-launch.log 2>&1 & "
        "disown || true; sleep 1; exit 0"
    )
    # ``bash -c``, not ``-lc``: a login shell can clobber WAYLAND_DISPLAY.
    return f"{prefix} bash -c {shlex.quote(inner)}"


def _usage() -> None:
    print(VM_LIVE_USAGE, file=sys.stderr)


def _install_from_tag(
    machine: Machine,
    *,
    guest: Guest,
    version: str,
    run: RunFn | None,
    commands: list[str],
) -> None:
    helper = smoke_update_script()
    if not helper.is_file():
        raise SessionSmokeError(f"missing update smoke script {helper}")
    scp_to_guest(
        machine, helper, "/tmp/smoke-update.sh", run=run, commands=commands
    )
    prev = ssh_run(
        machine,
        update_smoke_command(
            phase="previous",
            from_version=version,
            forbid_omarchy=guest.id == "arch",
        ),
        timeout=INSTALL_TIMEOUT_S,
        check=True,
        run=run,
        commands=commands,
    )
    seeded = parse_smoke_kv(f"{prev.stdout}{prev.stderr}", "FROM_VERSION")
    if seeded != version:
        raise SessionSmokeError(
            f"vm-live: installed FROM_VERSION {seeded!r}, expected {version}"
        )


def _install_from_local(
    machine: Machine,
    *,
    guest: Guest,
    local: LocalStrata,
    run: RunFn | None,
    commands: list[str],
    run_dir: Path,
) -> None:
    if local.kind == "archive":
        helper = smoke_update_script()
        if not helper.is_file():
            raise SessionSmokeError(f"missing update smoke script {helper}")
        scp_to_guest(
            machine, helper, "/tmp/smoke-update.sh", run=run, commands=commands
        )
        scp_to_guest(
            machine, local.path, GUEST_ARCHIVE_REMOTE, run=run, commands=commands
        )
        try:
            from_version = parse_archive_version(str(local.path))
        except SessionSmokeError:
            from_version = "local"
        ssh_run(
            machine,
            update_smoke_command(
                phase="previous",
                from_version=from_version,
                archive=GUEST_ARCHIVE_REMOTE,
                forbid_omarchy=guest.id == "arch",
            ),
            timeout=INSTALL_TIMEOUT_S,
            check=True,
            run=run,
            commands=commands,
        )
        return

    dest_bin = f"/home/{guest.user.name}/{STRATA_BIN_REL}"
    desktop = run_dir / STRATA_DESKTOP_FILE
    desktop.write_text(_desktop_entry_text(dest_bin), encoding="utf-8")
    scp_to_guest(
        machine, local.path, GUEST_BINARY_REMOTE, run=run, commands=commands
    )
    scp_to_guest(
        machine, desktop, GUEST_DESKTOP_REMOTE, run=run, commands=commands
    )
    ssh_run(
        machine,
        install_binary_commands(guest.user.name),
        timeout=30,
        check=True,
        run=run,
        commands=commands,
    )


def _seed_fixtures(
    machine: Machine,
    *,
    user: str,
    run_dir: Path,
    run: RunFn | None,
    commands: list[str],
) -> str:
    archive = run_dir / "fixtures.tar.gz"
    build_fixtures_archive(archive)
    scp_to_guest(
        machine, archive, GUEST_FIXTURES_TAR_REMOTE, run=run, commands=commands
    )
    ssh_run(
        machine,
        extract_fixtures_command(user),
        timeout=30,
        check=True,
        run=run,
        commands=commands,
    )
    return guest_fixtures_dir(user)


def run_vm_live(
    guest_id: str | None,
    *,
    from_tag: str | None = None,
    from_local: Path | str | None = None,
    graphical: bool = True,
    keep: bool = False,
    cache_dir: Path | None = None,
    check_host_fn: Callable[[], CheckHostResult] | None = None,
    run: RunFn | None = None,
    popen: PopenFn | None = None,
    create_overlay_fn: CreateOverlayFn | None = None,
    settle_s: float = SETTLE_S,
    graphical_ui: str | None = None,
    wait: bool = True,
    apparmor_bwrap_restrict: bool = False,
) -> int:
    """CLI body for ``vm-live``. Throwaway overlay; the golden is never written."""
    loaded = _load_or_usage(guest_id, "vm-live")
    if isinstance(loaded, int):
        return loaded
    guest = loaded

    if from_tag and from_local:
        print(VM_LIVE_SOURCE_EXCLUSIVE, file=sys.stderr)
        _usage()
        return 2
    if not from_tag and not from_local:
        print(VM_LIVE_SOURCE_REQUIRED, file=sys.stderr)
        _usage()
        return 2
    if not supports_install_from_release(guest):
        print(VM_LIVE_FAIL_CLOSED, file=sys.stderr)
        return 2
    if apparmor_bwrap_restrict and not supports_apparmor_bwrap_restrict(guest):
        print(f"vm-live: {APPARMOR_BWRAP_RESTRICT_UNSUPPORTED}", file=sys.stderr)
        return 2

    tag: str | None = None
    local: LocalStrata | None = None
    try:
        if from_tag is not None:
            tag = parse_from_tag(from_tag)
        else:
            assert from_local is not None
            local = resolve_local_strata(from_local)
    except SessionSmokeError as exc:
        print(str(exc), file=sys.stderr)
        _usage()
        return 2

    cache = cache_dir if cache_dir is not None else config.cache_dir()
    try:
        golden = require_golden(guest, cache)
    except RunTestError as exc:
        print(f"vm-live: {exc}", file=sys.stderr)
        return 1
    del golden

    host_fn = check_host_fn if check_host_fn is not None else check_host
    host = host_fn()
    if not host.ok:
        for err in host.errors:
            print(err, file=sys.stderr)
        return 1
    if host.ssh_key is None:
        print("vm-live: check-host ok but SSH key missing", file=sys.stderr)
        return 1
    if guest.firmware == "uefi" and host.ovmf_code is None:
        print("vm-live: check-host ok but OVMF code missing", file=sys.stderr)
        return 1

    ui = graphical_ui
    if graphical and ui is None:
        ui = choose_graphical_ui()
    if ui is None:
        ui = "gtk"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = artifacts.runs_dir(cache) / f"{stamp}-{guest.id}-live"
    run_dir.mkdir(parents=True, exist_ok=True)
    machine: Machine | None = None
    failed = False
    recorded: list[str] = []

    try:
        overlay, ovmf_vars = _prepare_overlay(
            guest, cache, run_dir, create_overlay_fn=create_overlay_fn
        )
        ovmf_code = host.ovmf_code if guest.firmware == "uefi" else None
        machine = _spawn_overlay_vm(
            guest=guest,
            overlay=overlay,
            run_dir=run_dir,
            ovmf_code=ovmf_code,
            ovmf_vars=ovmf_vars,
            identity=host.ssh_key,
            graphical=graphical,
            graphical_ui=ui,
            popen=popen,
            settle_s=settle_s,
            inherit_stdio=True,
        )
        wait_ssh(machine, timeout=guest.boot_timeout_s, run=run)
        if apparmor_bwrap_restrict:
            apparmor = run_apparmor_bwrap_restrict_step(
                machine, run=run, commands=recorded
            )
            print(
                "vm-live: AppArmor bwrap-userns-restrict enforced; "
                f"nested unshare in bwrap: {apparmor['nested_unshare_error']}",
                flush=True,
            )
        exports = run_session_step(
            machine,
            compositor=compositor_process_name(guest),
            timeout=SESSION_TIMEOUT_S,
            run=run,
            commands=recorded,
        )
        env = session_env_from_exports(exports)
        compositor = compositor_process_name(guest)
        if compositor == "gnome-shell":
            awake = ssh_run(
                machine,
                disable_gnome_idle_lock_command(env),
                timeout=30,
                run=run,
                commands=recorded,
            )
            if awake.returncode != 0:
                print(
                    "vm-live: could not disable GNOME idle lock: "
                    f"{(awake.stderr or awake.stdout).strip()}",
                    file=sys.stderr,
                )
        if tag is not None:
            _install_from_tag(
                machine,
                guest=guest,
                version=tag,
                run=run,
                commands=recorded,
            )
            source_note = f"tag {tag}"
        else:
            assert local is not None
            _install_from_local(
                machine,
                guest=guest,
                local=local,
                run=run,
                commands=recorded,
                run_dir=run_dir,
            )
            source_note = f"local {local.path}"
        fixtures = _seed_fixtures(
            machine,
            user=guest.user.name,
            run_dir=run_dir,
            run=run,
            commands=recorded,
        )
        ssh_run(
            machine,
            ensure_runtime_deps_command(),
            timeout=RUNTIME_DEPS_TIMEOUT_S,
            check=True,
            run=run,
            commands=recorded,
        )
        ssh_run(
            machine,
            launch_strata_at_command(
                env,
                fixtures,
                compositor=compositor,
                user=guest.user.name,
            ),
            timeout=30,
            check=True,
            run=run,
            commands=recorded,
        )
        print(f"vm-live: {guest.id} ssh_port={machine.ssh_port}", flush=True)
        print(f"vm-live: strata from {source_note}", flush=True)
        print(f"vm-live: fixtures {fixtures}", flush=True)
        if wait and machine._proc is not None:
            machine._proc.wait()
        return 0
    except KeyboardInterrupt:
        failed = True
        print("vm-live: interrupted", file=sys.stderr)
        return 130
    except (
        RunTestError,
        SessionSmokeError,
        ImageBuildError,
        TimeoutError,
        OSError,
        subprocess.CalledProcessError,
    ) as exc:
        failed = True
        print(f"vm-live: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        failed = True
        log.exception("vm-live failed")
        print(f"vm-live: {exc}", file=sys.stderr)
        return 1
    finally:
        if machine is not None:
            _shutdown_machine(machine)
        if not keep and not failed:
            shutil.rmtree(run_dir, ignore_errors=True)
        elif failed or keep:
            print(f"kept run dir: {run_dir}", file=sys.stderr)
