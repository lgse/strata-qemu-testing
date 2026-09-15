# strata-qemu-testing

QEMU/KVM guests for testing Strata's installer and desktop integration on
real Linux desktop sessions.

Each guest is built once from a committed recipe into a read-only "golden"
image. Every test run boots a throwaway overlay of that golden, waits for
an autologin Wayland session, and runs smoke checks over SSH: session and
compositor health, an in-guest screenshot, and optionally `install.sh`
followed by launching the installed application.

Guests in tree:

| Guest | Distribution | Desktop |
| --- | --- | --- |
| `ubuntu-2404` | Ubuntu 24.04 (dated cloud image, snapshot APT) | GDM, GNOME Shell |
| `fedora-workstation` | Fedora 44 Cloud Base + Workstation | GDM, GNOME Shell |
| `arch` | Arch Linux (dated cloud image, dated archive) | greetd, Hyprland |
| `omarchy-4` | Omarchy 4.x ISO autoinstall | SDDM, Hyprland |
| `omarchy-3` | Omarchy 3.x ISO autoinstall | SDDM or seamless-login, Hyprland |

How the pieces fit together is described in
[docs/architecture.md](docs/architecture.md).

## Requirements

- Linux x86_64 host with KVM (`/dev/kvm` readable and writable by your
  user). Software emulation is not supported.
- A working GPU/EGL stack: `libvirglrenderer` and a `/dev/dri/renderD*`
  device. QEMU runs with `egl-headless,gl=on`.
- About 9 GiB of free RAM per running guest (guests use 8 GiB) and
  roughly 100 GiB of free disk in the cache directory for a build
  (working copy + golden + source).
- [mise](https://mise.jdx.dev) 2026.9.4 or newer. Install it from your
  distribution's package manager; do not run it as root.

macOS and Windows are not supported.

## Setup

```bash
git clone <this repo> && cd strata-qemu-testing
mise trust
mise install          # Python 3.11 into the mise toolchain
mise bootstrap        # host packages via apt, dnf, or pacman, then check-host
mise run check-host
```

`mise bootstrap` installs QEMU (with the GL display modules), `qemu-img`,
OVMF, `xorriso`, `dosfstools`, `mtools`, OpenSSH, `curl`, and `openssl`.
It does not load the KVM module, add you to the `kvm` group, or install
GPU drivers. If `check-host` reports a problem it prints what to fix.

On a distribution without apt, dnf, or pacman, install those packages
yourself and run `mise run check-host`.

`check-host` generates an SSH keypair at `$CACHE/keys/id_ed25519` on its
first successful run. This key is only ever installed into throwaway
guests.

## Build a golden image

```bash
mise run image-build -- ubuntu-2404
```

This downloads the pinned source image or ISO, boots it, installs the
desktop and test dependencies, and saves the result under
`$CACHE/images/`. The terminal is quiet while the guest works; SSH
transcripts, the serial console, and the QEMU log are written to the run
directory instead.

Expect roughly 5-20 minutes for a cloud-image guest (`ubuntu-2404`,
`fedora-workstation`, `arch`) and 10-30 minutes for the Omarchy ISO
installs, depending on network speed and the host. The hard timeout is 60
minutes.

`image-build` is incremental. If a golden already exists for the current
recipe and source pin, it prints the path and exits without building.
Editing any recipe file (`image.toml`, `bootstrap.sh`, `setup.sh`,
templates, `cidata/`) changes the digest and triggers a rebuild. Pass
`--force` to rebuild regardless.

On failure the run directory (`$CACHE/runs/<stamp>-<guest>-build/`) is
kept with `qemu.log`, `serial.log`, `ssh.log`, and, for timeouts, a
screenshot of the console.

## Run tests

### Session smoke

```bash
mise run run-test -- ubuntu-2404 --session-only
```

Boots an overlay, waits for the autologin Wayland session, verifies the
compositor is running, and captures a screenshot from inside the guest.
Nothing Strata-related is installed or launched. Typical runtime is one to
three minutes.

### Installer smoke

```bash
mise run run-test -- arch --install-from release
mise run run-test -- ubuntu-2404 --install-from local-archive ~/Downloads/strata-1.2.3-x86_64-unknown-linux-gnu.tar.gz
```

Runs the session smoke, then downloads Strata's `install.sh` inside the
guest, records its SHA-256, and runs it with
`--non-interactive --with-desktop-entry --without-file-chooser` (adding
`--archive` for `local-archive`). It then checks `~/.local/bin/strata`
exists, compares `strata --version` with the intended version (the
GitHub latest release, or the version in the archive filename), verifies
the desktop entry, launches the application, and waits for its window
(Hyprland client class or GNOME session bus name) before taking a
screenshot.

The version step is recorded as skipped if `strata --version` does not
behave like a command-line flag. In that case the run opens
**Settings → About** (Ctrl+, then Tab × 5 to the About sidebar item,
then Space) and saves `about-version.png`. `--update-from` captures
both `about-version-before.png` (seeded previous install) and
`about-version-after.png` (after `install.sh` to latest). In-guest
`wtype` is preferred; Hyprland can send Ctrl+, via `hyprctl`; otherwise
the host sends keys through QEMU `send-key`.

`--install-from` does not probe Omarchy version detection or rewrite
Hyprland bindings. Use `--omarchy-bindings` for that.

### Update from a previous version

```bash
mise run run-test -- ubuntu-2404 --update-from 0.15.0
STRATA_QEMU_UPDATE_FROM=0.15.0,0.14.0 mise run test
```

Installs the given previous Strata release inside the guest, then runs
current `install.sh` to update to latest. The host checks `strata --version`
against the seeded previous tag, then against the latest GitHub release,
and continues with the desktop-entry and window oracles from
`--install-from`.

`--update-from` is exclusive with `--session-only`, `--install-from`, and
`--omarchy-bindings`. Same guests as `--install-from`.

The default previous versions the suite is written against are `0.15.0` and
`0.14.0`. Override that list with `STRATA_QEMU_UPDATE_FROM` (comma-separated
tags). Live `run-test` still takes one `--update-from VERSION` per overlay;
the env var is the configurable matrix host unit tests iterate.

### Omarchy bindings

```bash
mise run run-test -- omarchy-4 --omarchy-bindings
STRATA_QEMU_INSTALL_SH=~/dev/strata/install.sh \
  mise run run-test -- omarchy-3 --omarchy-bindings
```

Omarchy guests only (`omarchy-3`, `omarchy-4`). Exclusive with
`--session-only` and `--install-from`. Does not install or launch Strata.

The guest sources `install.sh` (curled from `lgse/strata` `main`, or a
host copy uploaded from `STRATA_QEMU_INSTALL_SH`) and:

1. checks that `detect_omarchy_major` matches the guest (4 or 3);
2. probes `omarchy_major_from` on whole `N.M` tokens and on
   `dev (b280f130)` (lgse/strata#743 / #652);
3. writes and asserts Hyprland file-manager bindings (`bindings.lua` on 4,
   `bindings.conf` on 3; the unused sibling must not carry the installer
   marker);
4. takes a session screenshot.

Point `STRATA_QEMU_INSTALL_SH` at a PR checkout when `main` does not yet
include `omarchy_major_from`; the token probes fail closed against the
pre-#743 installer.

### Udiskie unlock helpers

```bash
mise run run-test -- omarchy-4 --udiskie-unlock
mise run run-test -- arch --udiskie-unlock
STRATA_QEMU_INSTALL_SH=~/dev/strata/install.sh \
  mise run run-test -- omarchy-3 --udiskie-unlock
```

Omarchy and Arch guests only (`omarchy-3`, `omarchy-4`, `arch`). Exclusive
with `--session-only`, `--install-from`, `--omarchy-bindings`, and
`--update-from`. Does not install or launch Strata, spawn real udiskie, or
write the host user's udiskie config.

The guest sources `install.sh` (the committed helper fixture, or a host
copy from `STRATA_QEMU_INSTALL_SH`) under `STRATA_INSTALLER_TESTING=1`
with `PATH` limited to a stub directory, `BIN_PATH` stubbed to record
argv, and a temp `extracted/udiskie/unlock` marker when the simulated
archive supports the feature. Host unit tests in `tests/test_udiskie_unlock.py`
cover the full eligibility → marker → prompt → exec matrix, including
`--with-udiskie-unlock` flag parsing.

`--install-from` still uses `--non-interactive` without
`--with-udiskie-unlock`, so unattended guest installs do not rewrite
udiskie config.

### LUKS hotplug

```bash
mise run run-test -- omarchy-4 --luks-hotplug ~/dev/strata/target/release/strata
mise run run-test -- arch --luks-hotplug ~/dev/strata
mise run run-test -- omarchy-3 --luks-hotplug ~/dev/strata/target/release/strata
```

Omarchy and Arch guests only (`omarchy-3`, `omarchy-4`, `arch`). Exclusive
with `--session-only`, `--install-from`, `--omarchy-bindings`,
`--update-from`, and `--udiskie-unlock`. PATH is a host binary, a release
tarball, or a checkout containing `target/release/strata`.

The guest installs that Strata, runs `strata --install-udiskie-unlock`
(installing `udiskie` on Arch if needed), wraps the binary to record hook
argv, then the host hotplugs a LUKS disk over QMP. The smoke waits until
udiskie invokes Strata (`--udiskie-hook` / `--unlock-volume`). Host
`cryptsetup` and `qemu-img` are required to format the throwaway image.

### Output

On success `run-test` prints the guest, the screenshot path, and the run
directory. `result.json` in the run directory lists each step with its
duration and details such as the `install.sh` digest and observed version.

```
$CACHE/runs/<stamp>-<guest>/
  result.json        step results
  screenshot.png     in-guest screenshot
  qmp-session.png    QEMU-side screendump (may be absent)
  ssh.log            every guest command and its output
  serial.log         guest console
  qemu.log           QEMU stdout/stderr
```

The overlay disk is deleted after a successful run; pass `--keep` to
retain it. Failed runs keep everything and exit 1.

`run-test` never builds a golden. If one is missing it tells you to run
`image-build` first.

## Interactive use

```bash
mise run vm-run -- ubuntu-2404 --graphical
```

Boots a throwaway overlay in a QEMU window (GTK, or SDL if GTK is
unavailable). Without `--graphical` the guest runs headless and is
reachable over SSH only. `vm-run` prints the forwarded SSH port:

```bash
ssh -i "$CACHE/keys/id_ed25519" -p <port> tester@127.0.0.1
```

The `tester` account's password is `foobar` and it has passwordless sudo.
Changes are discarded when QEMU exits unless `--keep` is given; the golden
is never modified.

### Live session with Strata installed

```bash
mise run vm-live -- ubuntu-2404 --from-tag 0.15.0
mise run vm-live -- ubuntu-2404 --from-local ~/dev/strata/target/release/strata
mise run vm-live -- arch --from-local ~/Downloads/strata-0.16.0-x86_64-unknown-linux-gnu.tar.gz
```

Like `vm-run`, but waits for the autologin Wayland session, installs
Strata, seeds `~/fixtures` with sample files (documents, a PNG, a zip,
nested dirs, a symlink, a hidden file), and launches Strata on that
directory. A GTK/SDL window is the default; pass `--headless` for SSH
only.

`--from-tag VERSION` downloads that GitHub release inside the guest
(`0.15.0` or `v0.15.0`). `--from-local PATH` copies a host binary, a
release tarball, or a checkout that contains `target/release/strata`.
Exactly one of `--from-tag` or `--from-local` is required.

`vm-live` never builds a golden. If one is missing it tells you to run
`image-build` first.

## Cleaning up

```bash
mise run image-prune              # delete all run directories
mise run image-prune -- --images  # also delete golden images
```

Downloads and the SSH key are never deleted by `image-prune`.

## Cache location

Default: `$XDG_CACHE_HOME/strata-qemu-testing`, else
`~/.cache/strata-qemu-testing`. Override with `STRATA_QEMU_CACHE`. Put it
on a filesystem with plenty of space; goldens are 40 GiB sparse qcow2
files and builds need room for a working copy as well.

```
$CACHE/
  keys/         generated SSH key
  downloads/    pinned cloud images and ISOs
  images/       goldens, symlinks, provenance JSON
  runs/         per-run directories
```

## Host unit tests

```bash
mise run test
```

Runs the `unittest` suite. It exercises the CLI, recipe loading, argv
builders, protocol helpers, and the guest shell scripts with fixtures; it
never starts QEMU and does not need KVM.

## Command reference

mise options go before the task name; everything after `--` is passed to
the command.

| Command | Description |
| --- | --- |
| `mise run check-host` | Verify KVM, QEMU, GL, OVMF, tools, and RAM. Generates the SSH key. |
| `mise run image-build -- <guest> [--force]` | Build or reuse a golden image. |
| `mise run run-test -- <guest> --session-only [--keep]` | Session and screenshot smoke. |
| `mise run run-test -- <guest> --install-from release [--keep]` | Install from the latest GitHub release. |
| `mise run run-test -- <guest> --install-from local-archive PATH [--keep]` | Install from a host tarball. |
| `mise run run-test -- <guest> --update-from VERSION [--keep]` | Seed a previous release, then run current `install.sh` to latest. |
| `mise run run-test -- omarchy-3\|omarchy-4 --omarchy-bindings [--keep]` | Probe Omarchy detection and Hyprland bindings (optional `STRATA_QEMU_INSTALL_SH`). |
| `mise run run-test -- omarchy-3\|omarchy-4\|arch --udiskie-unlock [--keep]` | PATH-isolated `configure_udiskie_unlock` helper smoke (optional `STRATA_QEMU_INSTALL_SH`). |
| `mise run run-test -- omarchy-3\|omarchy-4\|arch --luks-hotplug PATH [--keep]` | Install host Strata, register the udiskie handler, hotplug LUKS, assert Strata is called. |
| `mise run vm-run -- <guest> [--graphical] [--keep]` | Interactive throwaway overlay. |
| `mise run vm-live -- <guest> (--from-tag VERSION \| --from-local PATH) [--headless] [--keep]` | Live overlay with Strata installed and `~/fixtures` sample files. |
| `mise run image-prune [-- --images]` | Delete run directories (and goldens). |
| `mise run test` | Host unit tests. |

For debug logging, invoke the module directly with `-v` before the
command:

```bash
mise exec -- python -m strataqemu -v run-test ubuntu-2404 --session-only
```

## Contributing

Recipes live in `images/<guest>/`; smoke scripts in `guest-tests/`; the
host code in `strataqemu/`. Guides for adding a guest, a smoke step, or a
CLI command are in `.agents/skills/`. Run `mise run test` before opening a
merge request.

## License

This project is licensed under the [MIT License](LICENSE).
