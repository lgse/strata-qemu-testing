#!/usr/bin/env bash
# Fixture install.sh implementing the udiskie-unlock helper surface from
# docs/grok-design-doc-61aebc82.md. Not a real installer.
NON_INTERACTIVE=no
WITH_SMB=ask
WITH_RAW=ask
WITH_DESKTOP_ENTRY=ask
WITH_FOLDER_ASSOCIATION=ask
WITH_FILE_MANAGER=ask
WITH_FILE_CHOOSER=ask
WITH_OMARCHY_KEYBINDS=ask
WITH_UDISKIE_UNLOCK=ask

warn() {
  printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2
}

die() {
  printf '\033[1;31merror:\033[0m %s\n' "$*" >&2
  exit 1
}

prompt() {
  local question=$1 default=${2:-yes} answer suffix
  if [[ $default == yes ]]; then
    suffix="[Y/n]"
  else
    suffix="[y/N]"
  fi

  if [[ -n ${PROMPT_DEVICE:-} && -r ${PROMPT_DEVICE} ]]; then
    printf '%s %s ' "$question" "$suffix" >"$PROMPT_DEVICE"
    IFS= read -r answer <"$PROMPT_DEVICE" || die "Could not read your answer."
  else
    # Enter / no TTY: take the default (No for udiskie unlock).
    answer=""
  fi
  answer=${answer:-$default}
  [[ $answer == [Yy] || $answer == [Yy][Ee][Ss] ]]
}

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  --non-interactive             Never prompt; install required components only
  --with-omarchy-keybinds       Replace Omarchy's file-manager keybinds
  --with-udiskie-unlock         Use Strata for udiskie encrypted-volume unlock
  -h, --help                    Show this help

Integration flags imply --non-interactive.
EOF
}

parse_args() {
  while (($# > 0)); do
    case $1 in
      --non-interactive) NON_INTERACTIVE=yes ;;
      --with-smb) NON_INTERACTIVE=yes; WITH_SMB=yes ;;
      --with-raw) NON_INTERACTIVE=yes; WITH_RAW=yes ;;
      --with-desktop-entry) NON_INTERACTIVE=yes; WITH_DESKTOP_ENTRY=yes ;;
      --with-folder-association)
        NON_INTERACTIVE=yes
        WITH_DESKTOP_ENTRY=yes
        WITH_FOLDER_ASSOCIATION=yes
        WITH_FILE_MANAGER=yes
        ;;
      --with-file-manager) NON_INTERACTIVE=yes; WITH_FILE_MANAGER=yes ;;
      --with-file-chooser) NON_INTERACTIVE=yes; WITH_FILE_CHOOSER=yes ;;
      --without-file-chooser) NON_INTERACTIVE=yes; WITH_FILE_CHOOSER=no ;;
      --with-omarchy-keybinds) NON_INTERACTIVE=yes; WITH_OMARCHY_KEYBINDS=yes ;;
      --with-udiskie-unlock) NON_INTERACTIVE=yes; WITH_UDISKIE_UNLOCK=yes ;;
      -h | --help) usage; exit 0 ;;
      *) die "Unknown option: $1 (run with --help for usage)." ;;
    esac
    shift
  done
}

want_option() {
  local selection=$1 question=$2 default=${3:-no}
  case $selection in
    yes) return 0 ;;
    no) return 1 ;;
    ask)
      [[ $NON_INTERACTIVE == no ]] && prompt "$question" "$default"
      ;;
    *) die "Invalid installer option state: $selection" ;;
  esac
}

udiskie_on_path() {
  command -v udiskie >/dev/null 2>&1
}

release_supports_udiskie_unlock() {
  local extracted=$1
  [[ -r $extracted/udiskie/unlock ]]
}

configure_udiskie_unlock() {
  local extracted=$1
  local omarchy_major=$2
  local arch_based=${3:-no}

  if [[ -n $omarchy_major ]]; then
    if ! udiskie_on_path; then
      if [[ $WITH_UDISKIE_UNLOCK == yes ]]; then
        die "udiskie is not on PATH; --with-udiskie-unlock requires udiskie."
      fi
      warn "Omarchy usually ships udiskie, but it is not on PATH. Skipping encrypted-volume unlock."
      return 0
    fi
  elif [[ $arch_based == yes ]] && udiskie_on_path; then
    :
  elif [[ $WITH_UDISKIE_UNLOCK == yes ]]; then
    die "--with-udiskie-unlock requires Omarchy 3 or 4, or an Arch-based system with udiskie on PATH."
  else
    return 0
  fi

  if ! release_supports_udiskie_unlock "$extracted"; then
    [[ $WITH_UDISKIE_UNLOCK == yes ]] \
      && die "This release does not include encrypted-volume integration. Install a newer release."
    warn "This Strata release does not include encrypted-volume unlock; skipping."
    return 0
  fi

  local question restore_hint
  if [[ -n $omarchy_major ]]; then
    restore_hint='Restore later in Settings or with strata --uninstall-udiskie-unlock.'
  else
    restore_hint='Restore later with strata --uninstall-udiskie-unlock.'
  fi
  question="Use Strata to unlock encrypted drives instead of udiskie's dialog? (Writes your udiskie config.yml, turns off LUKS automount, and restarts udiskie. ${restore_hint})"

  if want_option "$WITH_UDISKIE_UNLOCK" "$question" no; then
    if ! "$BIN_PATH" --install-udiskie-unlock; then
      if [[ -n $omarchy_major ]]; then
        die "Encrypted-volume unlock setup failed. Strata is installed; retry in Settings → General → Unlock encrypted volumes or run: $BIN_PATH --install-udiskie-unlock"
      fi
      die "Encrypted-volume unlock setup failed. Strata is installed; retry with: $BIN_PATH --install-udiskie-unlock"
    fi
  fi
}

if [[ ${STRATA_INSTALLER_TESTING:-0} != 1 ]]; then
  echo "fixture is not a real installer" >&2
  exit 1
fi
