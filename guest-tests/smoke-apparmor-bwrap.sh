#!/usr/bin/env bash
# Enforce AppArmor's bwrap-userns-restrict profile the way elementary OS 8
# does, then assert that nested user namespaces are denied inside bwrap
# (lgse/strata#1926). Ubuntu 24.04 ships the profile only under
# apparmor-profiles' extra-profiles, so it is installed from there.
#
# With the profile loaded, unprivileged bwrap runs as `bwrap` and its children
# as `bwrap//&unpriv_bwrap`, which denies every capability: a plain sandbox
# still works, but unshare(CLONE_NEWUSER|CLONE_NEWNS) inside it gets EPERM.
#
# Inputs (paths are overridable for host tests):
#   APPARMOR_EXTRA_PROFILE  profile to install
#   APPARMOR_PROFILE_DEST   where AppArmor loads it from
#   APPARMOR_SYSCTL         kernel.apparmor_restrict_unprivileged_userns
#   APPARMOR_PROFILES_LIST  securityfs list of loaded profiles
#
# Stdout KEY=value: RESTRICT_UNPRIVILEGED_USERNS=, PROFILES=, PLAIN_BWRAP_RC=,
# CHILD_LABEL=, NESTED_UNSHARE_RC=, NESTED_UNSHARE_ERR=
set -euo pipefail

src="${APPARMOR_EXTRA_PROFILE:-/usr/share/apparmor/extra-profiles/bwrap-userns-restrict}"
dest="${APPARMOR_PROFILE_DEST:-/etc/apparmor.d/bwrap-userns-restrict}"
sysctl_path="${APPARMOR_SYSCTL:-/proc/sys/kernel/apparmor_restrict_unprivileged_userns}"
profiles_list="${APPARMOR_PROFILES_LIST:-/sys/kernel/security/apparmor/profiles}"

one_line() {
  tr -d '\0' | tr '\n' ' ' | sed 's/[[:space:]]*$//'
}

if [[ ! -r $src ]]; then
  sudo -n env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y -q apparmor-profiles >&2
fi
if [[ ! -r $src ]]; then
  echo "apparmor-bwrap: ${src} missing after installing apparmor-profiles" >&2
  exit 1
fi
sudo -n install -m 0644 "$src" "$dest"
sudo -n apparmor_parser -r "$dest" >&2

restrict="$(cat "$sysctl_path" 2>/dev/null || echo missing)"
profiles="$(sudo -n cat "$profiles_list" \
  | sed -n -E 's/^(bwrap|unpriv_bwrap) \(enforce\)$/\1/p' | sort -u | paste -sd, -)"

sandbox=(bwrap --unshare-all --ro-bind / / --dev /dev --proc /proc)
set +e
"${sandbox[@]}" true >&2
plain_rc=$?
label="$("${sandbox[@]}" cat /proc/self/attr/current 2>&1 | one_line)"
nested_out="$("${sandbox[@]}" unshare -Urm true 2>&1)"
nested_rc=$?
nested_err="$(printf '%s' "$nested_out" | one_line)"
set -e

printf 'RESTRICT_UNPRIVILEGED_USERNS=%s\n' "$restrict"
printf 'PROFILES=%s\n' "$profiles"
printf 'PLAIN_BWRAP_RC=%s\n' "$plain_rc"
printf 'CHILD_LABEL=%s\n' "$label"
printf 'NESTED_UNSHARE_RC=%s\n' "$nested_rc"
printf 'NESTED_UNSHARE_ERR=%s\n' "$nested_err"

fail() {
  echo "apparmor-bwrap: $*" >&2
  exit 1
}
[[ $restrict == 1 ]] ||
  fail "kernel.apparmor_restrict_unprivileged_userns is ${restrict}, want 1"
[[ $profiles == bwrap,unpriv_bwrap ]] ||
  fail "bwrap and unpriv_bwrap are not both enforcing (got '${profiles}')"
[[ $plain_rc == 0 ]] || fail "plain bwrap sandbox failed (rc=${plain_rc})"
[[ $label == *unpriv_bwrap* ]] ||
  fail "bwrap child is not confined by unpriv_bwrap (label '${label}')"
[[ $nested_rc != 0 ]] || fail "nested unshare -Urm inside bwrap succeeded"
[[ $nested_err == *"Operation not permitted"* ]] ||
  fail "nested unshare -Urm failed for another reason: ${nested_err}"
