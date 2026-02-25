#!/usr/bin/env bash
set -euo pipefail

# Package Open-AutoGLM training data for migration to another machine.
#
# Included by default:
# - ~/.openautoglm/experience.db
# - ~/.openautoglm/navigation_map.db
# - ~/.openautoglm/coord_profiles.json (if exists)
# - <project>/artifacts/x_extract/x_learning_rules.json (if exists)
#
# Usage:
#   scripts/pack_openautoglm_training.sh
#   scripts/pack_openautoglm_training.sh --output ~/Desktop/my_bundle.tgz
#   scripts/pack_openautoglm_training.sh --no-artifacts

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OPENAUTOGLM_HOME="${HOME}/.openautoglm"
PROJECT_DIR="$REPO_ROOT"
INCLUDE_ARTIFACTS=1
OUTPUT_ARCHIVE=""

usage() {
  cat <<'EOF'
Usage:
  pack_openautoglm_training.sh [options]

Options:
  --output <path>         Output archive path (.tgz). Default: ./openautoglm_training_bundle_YYYYmmdd_HHMMSS.tgz
  --home-dir <path>       Source Open-AutoGLM home dir. Default: ~/.openautoglm
  --project-dir <path>    Source project dir (for artifacts). Default: repo root
  --no-artifacts          Do not include project artifacts/x_extract/x_learning_rules.json
  -h, --help              Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      OUTPUT_ARCHIVE="${2:-}"
      shift 2
      ;;
    --home-dir)
      OPENAUTOGLM_HOME="${2:-}"
      shift 2
      ;;
    --project-dir)
      PROJECT_DIR="${2:-}"
      shift 2
      ;;
    --no-artifacts)
      INCLUDE_ARTIFACTS=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

timestamp="$(date +%Y%m%d_%H%M%S)"
if [[ -z "$OUTPUT_ARCHIVE" ]]; then
  OUTPUT_ARCHIVE="$PWD/openautoglm_training_bundle_${timestamp}.tgz"
fi

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

bundle_root="$tmp_dir/openautoglm_training_bundle"
mkdir -p "$bundle_root/openautoglm_home"

copied_any=0

copy_if_exists() {
  local src="$1"
  local dst="$2"
  if [[ -f "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    cp -f "$src" "$dst"
    copied_any=1
    echo "Included: $src"
  else
    echo "Skip (not found): $src"
  fi
}

copy_if_exists "$OPENAUTOGLM_HOME/experience.db" "$bundle_root/openautoglm_home/experience.db"
copy_if_exists "$OPENAUTOGLM_HOME/navigation_map.db" "$bundle_root/openautoglm_home/navigation_map.db"
copy_if_exists "$OPENAUTOGLM_HOME/coord_profiles.json" "$bundle_root/openautoglm_home/coord_profiles.json"

if [[ "$INCLUDE_ARTIFACTS" -eq 1 ]]; then
  copy_if_exists \
    "$PROJECT_DIR/artifacts/x_extract/x_learning_rules.json" \
    "$bundle_root/project_artifacts/x_extract/x_learning_rules.json"
fi

if [[ "$copied_any" -eq 0 ]]; then
  echo "No migration files were found. Nothing to package." >&2
  exit 2
fi

manifest="$bundle_root/MANIFEST.txt"
{
  echo "bundle_created_at=${timestamp}"
  echo "source_home_dir=${OPENAUTOGLM_HOME}"
  echo "source_project_dir=${PROJECT_DIR}"
  echo "include_artifacts=${INCLUDE_ARTIFACTS}"
  echo
  echo "files:"
  find "$bundle_root" -type f ! -name "MANIFEST.txt" | sed "s|$bundle_root/|- |"
} > "$manifest"

tar -czf "$OUTPUT_ARCHIVE" -C "$tmp_dir" "openautoglm_training_bundle"

echo
echo "Package complete:"
echo "  $OUTPUT_ARCHIVE"
echo
echo "Next step on target machine:"
echo "  scripts/unpack_openautoglm_training.sh --archive \"$OUTPUT_ARCHIVE\""
