#!/usr/bin/env bash
# Provision the pinned upstream land-plant model without changing the image.
set -euo pipefail
destination="${1:-models}"
model_sha=a15906dbd8d60ec8dd01eb1b905d4886c2554fbf04851adfd2242a5ceac41fdb
mkdir -p "$destination"
model="$destination/land_plant.h5"
if [[ -e "$model" ]]; then
    printf '%s  %s\n' "$model_sha" "$model" | sha256sum -c -
    exit 0
fi
temp_model=$(mktemp "$destination/.land_plant.XXXXXX")
trap 'rm -f "$temp_model"' EXIT
curl --fail --location --retry 3 --output "$temp_model" \
    'https://zenodo.org/records/10836346/files/land_plant_v0.3_a_0080.h5?download=1'
printf '%s  %s\n' "$model_sha" "$temp_model" | sha256sum -c -
# Do not clobber a model installed by another process during the download.
ln "$temp_model" "$model"
printf 'Set Input.helixer_model_dir to %s and bind that directory into Singularity.\n' "$(cd "$destination" && pwd)"
