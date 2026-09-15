#!/usr/bin/env bash
# Source install.sh under STRATA_INSTALLER_TESTING=1 and exercise
# parse_args / configure_udiskie_unlock with PATH, BIN_PATH, extracted,
# HOME, and XDG isolated to throwaway dirs. Never spawn real udiskie or
# write the tester's ~/.config/udiskie.
#
# Inputs: INSTALL_SH, SMOKE_UDISKIE_CASE (parse-args|configure),
#         SMOKE_UDISKIE_ARGS, SMOKE_OMARCHY_MAJOR, SMOKE_ARCH_BASED,
#         SMOKE_UDISKIE_STUB, SMOKE_HOST_UDISKIE, SMOKE_MARKER,
#         SMOKE_PROMPT (yes|no), SMOKE_BIN_EXIT, SMOKE_CALL_TWICE,
#         SMOKE_BIN_HAS_FLAG_STRING.
set -euo pipefail

install_sh="${INSTALL_SH:-/tmp/strata-install.sh}"
case_name="${SMOKE_UDISKIE_CASE:-configure}"

if [[ ! -f $install_sh ]]; then
  echo "missing install.sh: ${install_sh}" >&2
  exit 1
fi

if [[ $case_name != parse-args && $case_name != configure ]]; then
  echo "unknown SMOKE_UDISKIE_CASE: ${case_name}" >&2
  exit 1
fi

bash_bin="${BASH:-}"
if [[ -z $bash_bin ]] || [[ ! -x $bash_bin ]]; then
  bash_bin="$(command -v bash)"
fi
if [[ -z $bash_bin ]]; then
  echo "bash not found" >&2
  exit 1
fi

tmp="$(mktemp -d)"
calls="$tmp/calls"
decoy_calls="$tmp/decoy-calls"
udiskie_ran="$tmp/udiskie-ran"
: >"$calls"
: >"$decoy_calls"

cleanup() {
  local rc=$?
  local bin_calls=0 decoy=0
  if [[ -s $calls ]]; then
    bin_calls="$(wc -l <"$calls" | tr -d ' ')"
  fi
  if [[ -s $decoy_calls ]]; then
    decoy="$(wc -l <"$decoy_calls" | tr -d ' ')"
  fi
  printf 'SMOKE_UDISKIE_CASE=%s\n' "$case_name"
  printf 'SMOKE_RC=%s\n' "$rc"
  printf 'BIN_PATH=%s\n' "${BIN_PATH_PRINTED:-}"
  printf 'EXTRACTED=%s\n' "${EXTRACTED_PRINTED:-}"
  printf 'PATH_USED=%s\n' "${PATH_USED:-}"
  printf 'BIN_SOURCE_HAS_FLAG=%s\n' "${BIN_SOURCE_HAS_FLAG:-0}"
  printf 'BIN_CALLS=%s\n' "$bin_calls"
  printf 'DECOY_CALLS=%s\n' "$decoy"
  printf 'UDISKIE_RAN=%s\n' "$( [[ -s $udiskie_ran ]] && echo 1 || echo 0 )"
  if [[ -s $calls ]]; then
    while IFS= read -r line; do
      printf 'BIN_ARGV=%s\n' "$line"
    done <"$calls"
  fi
  if [[ -n ${CHILD_STDOUT:-} && -f $CHILD_STDOUT ]]; then
    cat "$CHILD_STDOUT"
  fi
  rm -rf "$tmp"
  exit "$rc"
}
trap cleanup EXIT

home="$tmp/home"
xdg_config="$tmp/xdg-config"
xdg_data="$tmp/xdg-data"
stub_bin="$tmp/stub-bin"
host_bin="$tmp/host-bin"
extracted="$tmp/extracted"
permanent="$tmp/permanent"
child_out="$tmp/child-stdout"
mkdir -p "$home" "$xdg_config" "$xdg_data" "$stub_bin" "$extracted" "$permanent"

if [[ ${SMOKE_UDISKIE_STUB:-} == 1 ]]; then
  cat >"$stub_bin/udiskie" <<EOF
#!/bin/sh
printf 'ran\n' >> '$udiskie_ran'
exit 0
EOF
  chmod +x "$stub_bin/udiskie"
fi

if [[ ${SMOKE_HOST_UDISKIE:-} == 1 ]]; then
  mkdir -p "$host_bin"
  cat >"$host_bin/udiskie" <<EOF
#!/bin/sh
printf 'host-ran\n' >> '$udiskie_ran'
exit 0
EOF
  chmod +x "$host_bin/udiskie"
fi

if [[ ${SMOKE_MARKER:-} == 1 ]]; then
  mkdir -p "$extracted/udiskie"
  printf '%s\n' "This release's strata binary supports --install-udiskie-unlock." \
    >"$extracted/udiskie/unlock"
fi

bin="$permanent/strata"
{
  printf '%s\n' '#!/bin/bash'
  if [[ ${SMOKE_BIN_HAS_FLAG_STRING:-} == 1 ]]; then
    printf '%s\n' '# --install-udiskie-unlock'
  fi
  printf '%s\n' "printf '%s\\n' \"\$@\" >> '$calls'"
  printf '%s\n' "exit ${SMOKE_BIN_EXIT:-0}"
} >"$bin"
chmod +x "$bin"
BIN_SOURCE_HAS_FLAG=0
if grep -q -- '--install-udiskie-unlock' "$bin"; then
  BIN_SOURCE_HAS_FLAG=1
fi

decoy="$extracted/strata"
{
  printf '%s\n' '#!/bin/bash'
  printf '%s\n' "printf '%s\\n' \"\$@\" >> '$decoy_calls'"
  printf '%s\n' 'exit 0'
} >"$decoy"
chmod +x "$decoy"

BIN_PATH_PRINTED="$bin"
EXTRACTED_PRINTED="$extracted"
PATH_USED="$stub_bin"
CHILD_STDOUT="$child_out"

cat >"$tmp/run.sh" <<'RUN'
set -euo pipefail
source "$INSTALL_SH"
hash -r

print_flags() {
  printf 'NON_INTERACTIVE=%s\n' "${NON_INTERACTIVE-}"
  printf 'WITH_UDISKIE_UNLOCK=%s\n' "${WITH_UDISKIE_UNLOCK-}"
  printf 'WITH_SMB=%s\n' "${WITH_SMB-}"
  printf 'WITH_RAW=%s\n' "${WITH_RAW-}"
  printf 'WITH_DESKTOP_ENTRY=%s\n' "${WITH_DESKTOP_ENTRY-}"
  printf 'WITH_FOLDER_ASSOCIATION=%s\n' "${WITH_FOLDER_ASSOCIATION-}"
  printf 'WITH_FILE_MANAGER=%s\n' "${WITH_FILE_MANAGER-}"
  printf 'WITH_FILE_CHOOSER=%s\n' "${WITH_FILE_CHOOSER-}"
  printf 'WITH_OMARCHY_KEYBINDS=%s\n' "${WITH_OMARCHY_KEYBINDS-}"
}

if [[ ${SMOKE_PROMPT:-} == yes ]]; then
  prompt() { return 0; }
elif [[ ${SMOKE_PROMPT:-} == no ]]; then
  prompt() { return 1; }
fi

if [[ $SMOKE_UDISKIE_CASE == parse-args ]]; then
  if ! type parse_args >/dev/null 2>&1; then
    echo "parse_args missing in ${INSTALL_SH}" >&2
    exit 1
  fi
  # shellcheck disable=SC2206
  args=( ${SMOKE_UDISKIE_ARGS-} )
  parse_args "${args[@]}"
  print_flags
  exit 0
fi

if ! type configure_udiskie_unlock >/dev/null 2>&1; then
  echo "configure_udiskie_unlock missing in ${INSTALL_SH}" >&2
  exit 1
fi
if ! type release_supports_udiskie_unlock >/dev/null 2>&1; then
  echo "release_supports_udiskie_unlock missing in ${INSTALL_SH}" >&2
  exit 1
fi

if [[ -n ${SMOKE_UDISKIE_ARGS-} ]]; then
  if ! type parse_args >/dev/null 2>&1; then
    echo "parse_args missing in ${INSTALL_SH}" >&2
    exit 1
  fi
  # shellcheck disable=SC2206
  args=( ${SMOKE_UDISKIE_ARGS} )
  parse_args "${args[@]}"
fi

if release_supports_udiskie_unlock "$EXTRACTED"; then
  printf 'RELEASE_SUPPORTS=1\n'
else
  printf 'RELEASE_SUPPORTS=0\n'
fi
if command -v udiskie >/dev/null 2>&1; then
  printf 'UDISKIE_WHICH=%s\n' "$(command -v udiskie)"
  printf 'UDISKIE_ON_PATH=1\n'
else
  printf 'UDISKIE_WHICH=\n'
  printf 'UDISKIE_ON_PATH=0\n'
fi
print_flags

configure_udiskie_unlock "$EXTRACTED" "$OMARCHY_MAJOR" "$ARCH_BASED"
if [[ ${SMOKE_CALL_TWICE:-} == 1 ]]; then
  configure_udiskie_unlock "$EXTRACTED" "$OMARCHY_MAJOR" "$ARCH_BASED"
fi
RUN

set +e
PATH="$stub_bin" \
  HOME="$home" \
  XDG_CONFIG_HOME="$xdg_config" \
  XDG_DATA_HOME="$xdg_data" \
  BIN_PATH="$bin" \
  STRATA_INSTALLER_TESTING=1 \
  INSTALL_SH="$install_sh" \
  EXTRACTED="$extracted" \
  OMARCHY_MAJOR="${SMOKE_OMARCHY_MAJOR-}" \
  ARCH_BASED="${SMOKE_ARCH_BASED:-no}" \
  SMOKE_UDISKIE_CASE="$case_name" \
  SMOKE_UDISKIE_ARGS="${SMOKE_UDISKIE_ARGS-}" \
  SMOKE_PROMPT="${SMOKE_PROMPT-}" \
  SMOKE_CALL_TWICE="${SMOKE_CALL_TWICE-}" \
  "$bash_bin" "$tmp/run.sh" >"$child_out"
child_rc=$?
set -e

if [[ -f $xdg_config/udiskie/config.yml ]] || [[ -f $xdg_config/udiskie/config.json ]]; then
  echo "stub wrote udiskie config under isolated XDG_CONFIG_HOME" >&2
  exit 1
fi
if [[ -d $xdg_config/udiskie ]]; then
  echo "stub created isolated XDG udiskie directory" >&2
  exit 1
fi

exit "$child_rc"
