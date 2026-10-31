---
name: strataqemu-add-smoke-step
description: >-
  Add a new run-test step or guest-side smoke script to strata-qemu-testing
  (strataqemu/tests_spec.py and guest-tests/*.sh), wire it into the
  session-only, install-from, or omarchy-bindings flows, record it in
  result.json, and test it with fake SSH responders. Use when adding an
  assertion about the guest desktop, the installed Strata app, or a new
  oracle such as an AT-SPI probe, portal check, or file-manager test.
---

# Add a run-test smoke step

`run-test` is a sequence of steps in `strataqemu/tests_spec.py`. Each
step runs commands in the guest over SSH, raises `SessionSmokeError` or
`TimeoutError` on failure, and appends a dict to the `steps` list that
ends up in `result.json`. Read `docs/architecture.md` section "run-test"
first.

## Where steps live

| Flow | Function | Steps today |
| --- | --- | --- |
| `--session-only` | `run_session_only_steps` | `session`, `screenshot` |
| `--install-from` | `run_install_from_release_steps` | `session`, `install`, `version`, `desktop-entry`, `window`, `about-version` |
| `--update-from` | `run_update_from_steps` | `session`, `install-previous`, `version-previous`, `about-version-before`, `update`, `version`, `desktop-entry`, `window`, `about-version-after` |
| `--omarchy-bindings` | `run_omarchy_bindings_steps` | `session`, `omarchy-detect`, `omarchy-bindings`, `screenshot` |

`--apparmor-bwrap-restrict` is not a flow. It is a ubuntu-2404 modifier
that `run_run_test` runs as the first step (`apparmor-bwrap-restrict`)
before any flow; `vm-live` runs it before installing Strata.
`--omarchy-bindings` is Omarchy-only and exclusive with the other flows.
`--update-from` is exclusive with the other three. Do not add
detect/bindings oracles to `--install-from` or `--update-from`.
Previous versions for `--update-from` live in `DEFAULT_UPDATE_FROM_VERSIONS`
and `STRATA_QEMU_UPDATE_FROM`; do not hardcode a single from-version in
tests that are meant to cover the matrix.

Each flow receives a `Machine`, an injectable `run` callable (the fake
`subprocess.run` in tests), and a `commands` list that records every
guest command for the audit at the end of a session-only run.

## Guest command helpers

Always go through these so `ssh.log` and the `commands` audit stay
complete:

```python
proc = ssh_run(machine, remote_cmd, timeout=15, check=False, run=run, commands=recorded)
scp_to_guest(machine, local_path, "/tmp/name", run=run, commands=recorded)
scp_from_guest(machine, "/tmp/name", local_path, run=run, commands=recorded)
```

Commands that need the graphical session must be prefixed with the env
from the `session` step:

```python
env = session_env_from_exports(exports)   # XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS, WAYLAND_DISPLAY, HYPRLAND_INSTANCE_SIGNATURE
remote = f"{env_prefix(env)} some-tool --flag"
```

Quote anything interpolated with `shlex.quote`.

## Step shape

```python
started = time.monotonic()
proc = ssh_run(machine, remote, timeout=MY_TIMEOUT_S, check=True, run=run, commands=recorded)
if "expected" not in proc.stdout:
    raise SessionSmokeError(f"my-step: unexpected output: {proc.stdout.strip()}")
steps.append({
    "name": "my-step",
    "status": "pass",
    "seconds": round(time.monotonic() - started, 1),
    # step-specific evidence, e.g. "path": str(dest), "oracle": "..."
})
```

Polling oracles follow `wait_gnome_bus_name` / `wait_hyprland_class`:
deadline loop, injectable `sleep`, raise `TimeoutError` with the last
observed output. Keep budgets as module constants next to
`SESSION_TIMEOUT_S`.

A step that can legitimately not apply records `"status": "skip"` with a
`"reason"` (see the `version` step) rather than failing.

## Session-only contract

`--session-only` must not install or launch Strata. Any guest command
containing a token in `SESSION_ONLY_FORBIDDEN` fails the run after the
fact. If your step belongs to session-only, do not use `install.sh`,
`strata --version`, `gtk-launch`, `gio launch`, `NameHasOwner`, or the bus
name. If it is an install-flow step, add it to
`run_install_from_release_steps` only.

If the step belongs to `--omarchy-bindings`, add it to
`run_omarchy_bindings_steps` only (and `OMARCHY_BINDINGS_STEPS`). That
flow must not install or launch Strata.

Update `SESSION_ONLY_STEPS` / `INSTALL_FROM_RELEASE_STEPS` /
`UPDATE_FROM_STEPS` / `OMARCHY_BINDINGS_STEPS` when adding a step name;
tests assert on them.

## Guest-side scripts

Prefer a shell script in `guest-tests/` when the logic needs several
guest commands or must inspect guest state closely. Pattern:

- `#!/usr/bin/env bash`, `set -euo pipefail`.
- Inputs via environment variables with defaults (`SMOKE_COMPOSITOR`,
  `SMOKE_ORACLE`, ...), never positional secrets.
- Output `KEY=value` lines on stdout for the host to parse; diagnostics on
  stderr; non-zero exit on failure.
- Add an accessor in `tests_spec.py` (`smoke_<name>_script()`), upload
  with `scp_to_guest` to `/tmp/<name>.sh`, run with `bash /tmp/<name>.sh`.

Test the script in `tests/test_guest_smokes.py` by putting fake
`loginctl`, `hyprctl`, `gdbus`, `grim`, etc. on `PATH` (see
`LOGINCTL_FAKE` and the fixture helpers there).

## Compositor differences

Branch on `compositor_process_name(guest)` returning `"gnome-shell"` or
`"Hyprland"`:

- Screenshots: `screenshot_tool_for_compositor` (`gnome-screenshot` vs
  `grim`). Use `capture_guest_screenshot`, which handles the GNOME
  portal timeout with a VNC fallback.
- Window presence: GNOME `gdbus ... NameHasOwner <bus name>`; Hyprland
  `hyprctl clients -j | jq -e 'select(.class == ...)'`.
- The Hyprland guests also export `HYPRLAND_INSTANCE_SIGNATURE`; GNOME
  guests do not.

## Wiring into run_test.py

`run_run_test` calls the matching step function
(`run_session_only_steps`, `run_install_from_release_steps`,
`run_update_from_steps`, or `run_omarchy_bindings_steps`) and merges the
returned `steps` list and `extras` dict into `result.json`. Add new
top-level result keys through `extras`, not by editing `run_run_test`.

## Tests

In `tests/test_run_test.py` and `tests/test_tests_spec.py` the `_FakeRun`
class answers guest commands by substring match on the remote command
string. Extend it (or a local copy) with a branch for your new command,
then assert:

- the step appears in `result.json` with `status` and `seconds`;
- failure paths raise the right error and leave `result.json` with
  `error` and `commands`;
- for session-only steps, `assert_session_only_commands(recorded)` passes;
- no `qemu-system-*` is ever invoked (`_refuse_qemu_system`).

Run `mise run test`. Then verify live on at least one GNOME and one
Hyprland guest:

```bash
mise run run-test -- ubuntu-2404 --session-only
mise run run-test -- arch --install-from release
mise run run-test -- omarchy-4 --omarchy-bindings
```
