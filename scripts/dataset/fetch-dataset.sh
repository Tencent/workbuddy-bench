#!/usr/bin/env bash
# Download dataset subsets from HuggingFace and extract them into datasets/.
#
# WorkBuddy Bench does NOT ship its subsets in git. Each subset is one .tar.gz on
# a HuggingFace dataset repo; this script fetches, checksum-verifies, and unpacks
# them so the framework finds datasets/<subset>/tasks where job YAMLs point.
#
# Usage:
#   scripts/dataset/fetch-dataset.sh <subset>...   # code | office | sec | web
#   scripts/dataset/fetch-dataset.sh all           # all four
#   scripts/dataset/fetch-dataset.sh --force code  # re-download / overwrite
#
# Config (env):
#   WB_BENCH_HF_REPO   HuggingFace dataset repo id (default tencent/workbuddy-bench).
#   WB_BENCH_HF_ENDPOINT   base URL (default https://huggingface.co)
#
# Prefers `huggingface-cli` if installed; otherwise falls back to curl/wget.
# After download it verifies each archive against the repo's SHA256SUMS.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DATASETS_DIR="$REPO_ROOT/datasets"

HF_REPO="${WB_BENCH_HF_REPO:-tencent/workbuddy-bench}"
HF_ENDPOINT="${WB_BENCH_HF_ENDPOINT:-https://huggingface.co}"
BASE_URL="$HF_ENDPOINT/datasets/$HF_REPO/resolve/main"

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

FORCE=0
ARGS=()
for a in "$@"; do
    case "$a" in
        --force) FORCE=1;;
        -h|--help) usage 0;;
        -*) echo "ERROR: unknown flag '$a'" >&2; usage 1;;
        *) ARGS+=("$a");;
    esac
done
[ "${#ARGS[@]}" -gt 0 ] || usage 1

# Expand the requested subset list to full archive base names (dir names).
declare -a KNOWN=(code office sec web)
resolve_names() {
    local out=()
    for a in "${ARGS[@]}"; do
        if [ "$a" = "all" ]; then
            for k in "${KNOWN[@]}"; do out+=("wb-bench-$k-v1.0"); done
        elif printf '%s\n' "${KNOWN[@]}" | grep -qx "$a"; then
            out+=("wb-bench-$a-v1.0")
        elif [[ "$a" == wb-bench-*-v* ]]; then
            out+=("$a")
        else
            echo "ERROR: unknown subset '$a' (use: ${KNOWN[*]} | all)" >&2; exit 1
        fi
    done
    printf '%s\n' "${out[@]}" | sort -u
}

have() { command -v "$1" >/dev/null 2>&1; }

download() {
    local name="$1" dest="$2"
    if have huggingface-cli; then
        huggingface-cli download "$HF_REPO" "$name.tar.gz" \
            --repo-type dataset --local-dir "$(dirname "$dest")" >/dev/null
    elif have curl; then
        curl -fSL "$BASE_URL/$name.tar.gz" -o "$dest"
    elif have wget; then
        wget -q "$BASE_URL/$name.tar.gz" -O "$dest"
    else
        echo "ERROR: need huggingface-cli, curl, or wget to download." >&2; exit 1
    fi
}

verify() {
    local name="$1" dest="$2" sums; sums="$(dirname "$dest")/SHA256SUMS"
    # Best-effort: pull the repo-side SHA256SUMS and check this archive's line.
    if have curl; then curl -fsSL "$BASE_URL/SHA256SUMS" -o "$sums" 2>/dev/null || return 0
    elif have wget; then wget -q "$BASE_URL/SHA256SUMS" -O "$sums" 2>/dev/null || return 0
    else return 0; fi
    ( cd "$(dirname "$dest")" && grep "  $name.tar.gz\$" SHA256SUMS | sha256sum -c - )
}

mkdir -p "$DATASETS_DIR"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP" "$DATASETS_DIR"/.stage-*.$$' EXIT

names_out="$(resolve_names)" || exit 1
mapfile -t NAMES <<< "$names_out"
for name in "${NAMES[@]}"; do
    target="$DATASETS_DIR/$name"
    if [ -d "$target" ] && [ "$FORCE" -ne 1 ]; then
        echo "  skip (exists): $name  — pass --force to re-download"
        continue
    fi
    echo "  fetching: $name.tar.gz"
    archive="$TMP/$name.tar.gz"
    download "$name" "$archive"
    verify "$name" "$archive" || { echo "ERROR: checksum failed for $name" >&2; exit 1; }
    # Extract to a staging dir on the same filesystem, then swap in atomically so
    # a mid-extract failure can never leave a half-written dataset in place.
    stage="$DATASETS_DIR/.stage-$name.$$"
    rm -rf "$stage"; mkdir -p "$stage"
    tar xzf "$archive" -C "$stage"
    rm -rf "$target"
    mv "$stage/$name" "$target"
    rm -rf "$stage"
    echo "  installed: datasets/$name/"
done
echo "=== done ==="
