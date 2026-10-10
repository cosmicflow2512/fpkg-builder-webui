#!/bin/sh
set -eu

: "${FPKG_CONFIG_ROOT:=/config}"
: "${FPKG_IMPORT_ROOT:=${FPKG_CONFIG_ROOT}/imports}"
: "${FPKG_DEFAULT_WORK:=${FPKG_CONFIG_ROOT}/work}"
: "${LIBPROSPERO_TEMP_DIR:=${FPKG_DEFAULT_WORK}/publisher-temp}"
: "${WINEPREFIX:=${FPKG_DEFAULT_WORK}/wine}"

export FPKG_CONFIG_ROOT
export FPKG_IMPORT_ROOT
export FPKG_DEFAULT_WORK
export LIBPROSPERO_TEMP_DIR
export WINEPREFIX

mkdir -p "$FPKG_CONFIG_ROOT" "$FPKG_IMPORT_ROOT" "$FPKG_DEFAULT_WORK" "$LIBPROSPERO_TEMP_DIR" "$WINEPREFIX"

if [ "$(id -u)" = "0" ]; then
  chown -R 0:0 "$FPKG_IMPORT_ROOT" "$FPKG_DEFAULT_WORK" 2>/dev/null || true
fi

exec "$@"
