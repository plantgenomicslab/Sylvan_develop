#!/usr/bin/env bash
# check_container.sh <sylvan.sif> [singularity exec args...]
#
# Fast check (a few seconds, no TensorFlow import) that an image can start Helixer.
# Snakefile_annotate runs it at pipeline start (onstart; skipped on dry runs), so a
# wrong or outdated image fails here with a clear message instead of hours later
# inside the `helixer` rule. You can also run it by hand on a downloaded image.
#
# WHY
#   Every image built 2026-07-08 to 2026-08-27 (Sylvan_20260708.sif through
#   Sylvan_20260827_evm.sif) ships Helixer v0.3.7 without PyYAML, scikit-learn and
#   scipy, so Helixer dies at startup with "No module named 'yaml'" (fixed in
#   a60b5ac). Those images also carry an ICU-linked libsqlite that needs
#   CXXABI_1.3.15, which breaks `import sqlite3` once TensorFlow has loaded the
#   base image's older libstdc++; the probe preloads that libstdc++ to reproduce
#   the same load order. The 2024 library://wyim/sylvan image predates the
#   /opt/envs + /opt/mamba layout and fails both tests.
#
# Set SYLVAN_SKIP_CONTAINER_CHECK=1 to skip (not recommended).
set -euo pipefail

SIF="${1:?usage: check_container.sh <sylvan.sif> [singularity exec args...]}"
shift

if [ "${SYLVAN_SKIP_CONTAINER_CHECK:-0}" = "1" ]; then
	echo "check_container: skipped (SYLVAN_SKIP_CONTAINER_CHECK=1)" >&2
	exit 0
fi

fail() {
	cat >&2 <<EOF

================================================================================
ERROR: Sylvan container check failed: $1
  Image: $SIF

  This image cannot run Helixer, or predates the Sylvan v2.1.0 container layout.
  Images built 2026-07-08 to 2026-08-27 (Sylvan_20260708*.sif ... Sylvan_20260827_evm.sif)
  ship a Helixer that dies at startup (No module named 'yaml'), and the 2024
  library://wyim/sylvan image is incompatible with this pipeline.

  Use the Sylvan release image (README, "Container image": download it and
  verify it with sha256sum), or build one from this repository:
    singularity build --fakeroot singularity/sylvan.sif singularity/Sylvan.def

  To skip this check (not recommended): SYLVAN_SKIP_CONTAINER_CHECK=1
================================================================================
EOF
	exit 1
}

[ -f "$SIF" ] || fail "image file not found"
command -v singularity >/dev/null 2>&1 || fail "'singularity' is not on PATH"

# Preload the base image's libstdc++ the way `import tensorflow` does, then import
# the modules Helixer needs but v0.3.7 does not declare.
PROBE='import ctypes, os
lib = "/usr/lib/x86_64-linux-gnu/libstdc++.so.6"
if os.path.exists(lib):
    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
import yaml, sklearn, scipy, sqlite3'

# --home mirrors Snakemake's own `singularity exec --home <workdir>`, so rc files
# in the host home do not leak into the check.
if ! singularity exec --home "$PWD" "$@" "$SIF" sh -c \
	'test -s /opt/mamba/etc/mamba_hook.sh || { echo "missing /opt/mamba/etc/mamba_hook.sh" >&2; exit 1; }
	 test -x /opt/envs/helixer/bin/python || { echo "missing /opt/envs/helixer" >&2; exit 1; }
	 exec /opt/envs/helixer/bin/python -s -c "$1"' sh "$PROBE"; then
	fail "Helixer environment is incomplete (see the error above)"
fi
echo "check_container: OK ($SIF)" >&2
