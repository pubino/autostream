#!/usr/bin/env bash
set -euo pipefail

# Build AutoSignDisplay and launch it on a tvOS simulator.
#
# Usage (runnable from any directory):
#   ./scripts/run.sh                      # build, install, launch (unmanaged)
#   ./scripts/run.sh --udid <UDID>        # target a specific simulator
#   ./scripts/run.sh --managed            # launch with a managed payload applied
#   ./scripts/run.sh --release            # build the Release configuration
#   ./scripts/run.sh --clean              # wipe the app's data first
#   ./scripts/run.sh --no-open            # don't bring Simulator.app forward
#   ./scripts/run.sh --dry-run            # print what would happen

# shellcheck source=scripts/lib/simulator.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/simulator.sh"

UDID=""
MANAGED_MODE="unmanaged"
CONFIGURATION="Debug"
CLEAN_DATA=0
OPEN_SIMULATOR=1
DRY_RUN=0

usage() {
  sed -n '4,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

APP=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --udid) UDID="$2"; shift 2 ;;
    --managed) MANAGED_MODE="managed"; shift ;;
    --unmanaged) MANAGED_MODE="unmanaged"; shift ;;
    --release) CONFIGURATION="Release"; shift ;;
    --debug) CONFIGURATION="Debug"; shift ;;
    --app)     APP="${2:?--app needs a scheme name}"; shift 2 ;;
    --clean) CLEAN_DATA=1; shift ;;
    --no-open) OPEN_SIMULATOR=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

select_app "$APP"

UDID="$(resolve_udid "$UDID")"
BUILD_DIR="$REPO_ROOT/build/$CONFIGURATION-appletvsimulator"
# Named by the scheme, not the project. Both targets build into this one directory,
# so $PROJECT_NAME.app here installed AutoSignDisplay.app no matter what --app said.
APP_PATH="$BUILD_DIR/$APP_BUNDLE_NAME"

echo "[run] configuration=$CONFIGURATION udid=$UDID mode=$MANAGED_MODE"

BUILD_CMD=(
  xcodebuild
  -project "$PROJECT_NAME.xcodeproj"
  -scheme "$SCHEME_NAME"
  -sdk appletvsimulator
  -configuration "$CONFIGURATION"
  CONFIGURATION_BUILD_DIR="$BUILD_DIR"
  build
)

echo "[run] ${BUILD_CMD[*]}"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "DRY: ${BUILD_CMD[*]}"
else
  # Surface errors and the result line; the full transcript is noise here.
  set +e
  "${BUILD_CMD[@]}" 2>&1 | grep -E "error:|warning: .*(deprecated|never used)|BUILD (SUCCEEDED|FAILED)"
  build_status=${PIPESTATUS[0]}
  set -e
  if [[ $build_status -ne 0 ]]; then
    echo "[run] build failed" >&2
    exit "$build_status"
  fi
fi

ensure_booted "$UDID" "$DRY_RUN"

if [[ $OPEN_SIMULATOR -eq 1 ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "DRY: open -a Simulator"
  else
    open -a Simulator
  fi
fi

if [[ $DRY_RUN -eq 1 ]]; then
  [[ $CLEAN_DATA -eq 1 ]] && echo "DRY: xcrun simctl uninstall $UDID $BUNDLE_ID"
  echo "DRY: xcrun simctl install $UDID $APP_PATH"
  echo "DRY: apply_managed_state $UDID $MANAGED_MODE"
  echo "DRY: xcrun simctl launch $UDID $BUNDLE_ID"
  exit 0
fi

xcrun simctl terminate "$UDID" "$BUNDLE_ID" >/dev/null 2>&1 || true

if [[ $CLEAN_DATA -eq 1 ]]; then
  echo "[run] removing existing app and its data"
  xcrun simctl uninstall "$UDID" "$BUNDLE_ID" >/dev/null 2>&1 || true
fi

echo "[run] installing $APP_PATH"
xcrun simctl install "$UDID" "$APP_PATH"

# Applied after install so the container exists, and before launch so the app
# reads it during AppConfig.applyConfiguration() in init().
apply_managed_state "$UDID" "$MANAGED_MODE"

echo "[run] launching $BUNDLE_ID"
xcrun simctl launch "$UDID" "$BUNDLE_ID"

cat <<EOF

[run] done. Useful follow-ups:
  xcrun simctl io $UDID screenshot /tmp/autosigndisplay.png
  ./scripts/check-managed-status.sh --udid $UDID
EOF
