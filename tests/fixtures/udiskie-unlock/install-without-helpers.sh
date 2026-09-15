#!/usr/bin/env bash
# Installer fixture without udiskie helpers. The smoke fail-closes when
# configure_udiskie_unlock / parse_args --with-udiskie-unlock are missing.
if [[ ${STRATA_INSTALLER_TESTING:-0} != 1 ]]; then
  echo "fixture is not a real installer" >&2
  exit 1
fi
