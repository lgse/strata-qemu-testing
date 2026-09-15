#!/usr/bin/env bash
# Wrap the installed Strata binary so udiskie hook argv is logged, then wait
# for a hotplugged LUKS disk and for that hook (or --unlock-volume) to run.
# Never formats or unlocks the volume. Inputs via environment only.
#
# Inputs: SMOKE_LUKS_CASE (wrap|wait), SMOKE_STRATA_BIN, SMOKE_HOOK_LOG,
#         SMOKE_LUKS_SERIAL, SMOKE_DEVICE_TIMEOUT_S, SMOKE_HOOK_TIMEOUT_S,
#         SMOKE_SLEEP.
set -euo pipefail

case_name="${SMOKE_LUKS_CASE:-wait}"
bin="${SMOKE_STRATA_BIN:-$HOME/.local/bin/strata}"
hook_log="${SMOKE_HOOK_LOG:-/tmp/strata-luks-hook.log}"
serial="${SMOKE_LUKS_SERIAL:-strata-luks}"
device_timeout="${SMOKE_DEVICE_TIMEOUT_S:-20}"
hook_timeout="${SMOKE_HOOK_TIMEOUT_S:-20}"
nap="${SMOKE_SLEEP:-1}"

if [[ $case_name != wrap && $case_name != wait ]]; then
  echo "unknown SMOKE_LUKS_CASE: ${case_name}" >&2
  exit 1
fi

if [[ $case_name == wrap ]]; then
  if [[ ! -x $bin ]]; then
    echo "strata binary missing: ${bin}" >&2
    exit 1
  fi
  real="${bin}.real"
  if [[ ! -e $real ]]; then
    mv "$bin" "$real"
  fi
  cat >"$bin" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> $(printf '%q' "$hook_log")
exec $(printf '%q' "$real") "\$@"
EOF
  chmod +x "$bin"
  : >"$hook_log"
  printf 'SMOKE_LUKS_CASE=wrap\n'
  printf 'WRAPPED=1\n'
  printf 'HOOK_LOG=%s\n' "$hook_log"
  printf 'STRATA_REAL=%s\n' "$real"
  exit 0
fi

lsblk_has_luks() {
  lsblk -ln -o NAME,FSTYPE,SERIAL 2>/dev/null \
    | awk -v serial="$serial" '$2 == "crypto_LUKS" && $3 == serial { print $1; found=1; exit }
      END { exit found ? 0 : 1 }'
}

hook_line() {
  if [[ -s $hook_log ]]; then
    grep -E -- '--udiskie-hook|--unlock-volume' "$hook_log" | tail -n1 || true
  fi
}

started="$(date +%s)"
dev=""
while :; do
  if dev="$(lsblk_has_luks)"; then
    break
  fi
  now="$(date +%s)"
  if (( now - started >= device_timeout )); then
    echo "LUKS device with serial ${serial} not visible in ${device_timeout}s" >&2
    lsblk -ln -o NAME,FSTYPE,SERIAL >&2 || true
    exit 1
  fi
  sleep "$nap"
done

started="$(date +%s)"
line=""
while :; do
  line="$(hook_line)"
  if [[ -n $line ]]; then
    break
  fi
  now="$(date +%s)"
  if (( now - started >= hook_timeout )); then
    echo "Strata was not called for LUKS serial ${serial} in ${hook_timeout}s" >&2
    if [[ -f $hook_log ]]; then
      echo "hook log:" >&2
      cat "$hook_log" >&2 || true
    fi
    pgrep -af strata >&2 || true
    exit 1
  fi
  sleep "$nap"
done

printf 'SMOKE_LUKS_CASE=wait\n'
printf 'LUKS_DEV=%s\n' "$dev"
printf 'LUKS_SERIAL=%s\n' "$serial"
printf 'STRATA_CALLED=1\n'
printf 'HOOK_LINE=%s\n' "$line"
