#!/usr/bin/env bash
set -euo pipefail

# Restore Open-AutoGLM training data from a migration bundle.
#
# Usage:
#   scripts/unpack_openautoglm_training.sh --archive /path/to/openautoglm_training_bundle_xxx.tgz
#   scripts/unpack_openautoglm_training.sh --archive ./bundle.tgz --no-artifacts

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ARCHIVE_PATH=""
OPENAUTOGLM_HOME="${HOME}/.openautoglm"
PROJECT_DIR="$REPO_ROOT"
RESTORE_ARTIFACTS=1
BACKUP_DIR=""

usage() {
  cat <<'EOF'
Usage:
  unpack_openautoglm_training.sh --archive <bundle.tgz> [options]

Options:
  --archive <path>        Migration archive created by pack_openautoglm_training.sh (required)
  --home-dir <path>       Target Open-AutoGLM home dir. Default: ~/.openautoglm
  --project-dir <path>    Target project dir for artifacts. Default: repo root
  --backup-dir <path>     Backup dir for overwritten files. Default: ~/.openautoglm_backup_YYYYmmdd_HHMMSS
  --no-artifacts          Do not restore project artifacts/x_extract/x_learning_rules.json
  -h, --help              Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive)
      ARCHIVE_PATH="${2:-}"
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
    --backup-dir)
      BACKUP_DIR="${2:-}"
      shift 2
      ;;
    --no-artifacts)
      RESTORE_ARTIFACTS=0
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

if [[ -z "$ARCHIVE_PATH" ]]; then
  echo "--archive is required" >&2
  usage
  exit 1
fi

if [[ ! -f "$ARCHIVE_PATH" ]]; then
  echo "Archive not found: $ARCHIVE_PATH" >&2
  exit 1
fi

timestamp="$(date +%Y%m%d_%H%M%S)"
if [[ -z "$BACKUP_DIR" ]]; then
  BACKUP_DIR="${HOME}/.openautoglm_backup_${timestamp}"
fi
mkdir -p "$BACKUP_DIR"

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

tar -xzf "$ARCHIVE_PATH" -C "$tmp_dir"
bundle_root="$tmp_dir/openautoglm_training_bundle"
if [[ ! -d "$bundle_root" ]]; then
  echo "Invalid archive layout: openautoglm_training_bundle not found" >&2
  exit 2
fi

mkdir -p "$OPENAUTOGLM_HOME"

backup_and_copy() {
  local src="$1"
  local dst="$2"
  local backup_base="$3"

  if [[ ! -f "$src" ]]; then
    echo "Skip (not in bundle): $src"
    return 0
  fi

  if [[ -f "$dst" ]]; then
    mkdir -p "$backup_base"
    cp -f "$dst" "$backup_base/$(basename "$dst")"
    echo "Backed up: $dst -> $backup_base/$(basename "$dst")"
  fi

  mkdir -p "$(dirname "$dst")"
  cp -f "$src" "$dst"
  echo "Restored: $dst"
}

backup_and_copy \
  "$bundle_root/openautoglm_home/experience.db" \
  "$OPENAUTOGLM_HOME/experience.db" \
  "$BACKUP_DIR/openautoglm_home"

backup_and_copy \
  "$bundle_root/openautoglm_home/navigation_map.db" \
  "$OPENAUTOGLM_HOME/navigation_map.db" \
  "$BACKUP_DIR/openautoglm_home"

backup_and_copy \
  "$bundle_root/openautoglm_home/coord_profiles.json" \
  "$OPENAUTOGLM_HOME/coord_profiles.json" \
  "$BACKUP_DIR/openautoglm_home"

if [[ "$RESTORE_ARTIFACTS" -eq 1 ]]; then
  backup_and_copy \
    "$bundle_root/project_artifacts/x_extract/x_learning_rules.json" \
    "$PROJECT_DIR/artifacts/x_extract/x_learning_rules.json" \
    "$BACKUP_DIR/project_artifacts/x_extract"
fi

echo
echo "Restore complete."
echo "Backup dir:"
echo "  $BACKUP_DIR"

if command -v sqlite3 >/dev/null 2>&1; then
  if [[ -f "$OPENAUTOGLM_HOME/experience.db" ]]; then
    exp_count="$(sqlite3 "$OPENAUTOGLM_HOME/experience.db" 'select count(*) from action_stats;' 2>/dev/null || echo "N/A")"
    echo "experience.action_stats: $exp_count"
  fi
  if [[ -f "$OPENAUTOGLM_HOME/navigation_map.db" ]]; then
    nav_count="$(sqlite3 "$OPENAUTOGLM_HOME/navigation_map.db" 'select count(*) from navigation_states;' 2>/dev/null || echo "N/A")"
    echo "navigation.navigation_states: $nav_count"
  fi
fi
