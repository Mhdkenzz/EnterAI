#!/usr/bin/env sh
set -eu
if command -v tryme >/dev/null 2>&1; then
  tryme --help >/dev/null
else
  echo "Tryme CLI is not installed; install it in CI before running this smoke gate." >&2
  exit 127
fi
