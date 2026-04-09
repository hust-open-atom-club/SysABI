#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/configs/phase1_rules.json"

read_config() {
  python3 - <<'PY' "$CONFIG" "$1"
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
cursor = config
for part in sys.argv[2].split("."):
    cursor = cursor[part]
print(cursor)
PY
}

resolve_path() {
  local value="$1"
  if [[ "$value" = /* ]]; then
    printf '%s\n' "$value"
  else
    printf '%s\n' "$ROOT/$value"
  fi
}

SYZKALLER_REPO="$(read_config syzkaller.repo)"
SYZKALLER_COMMIT="$(read_config syzkaller.commit)"
GO_VERSION="$(read_config syzkaller.go_version)"
GO_PLATFORM="$(read_config syzkaller.go_platform)"
SYZKALLER_DIR="$(resolve_path "$(read_config paths.syzkaller_dir)")"
GO_ARCHIVE_DIR="$(resolve_path "$(read_config paths.go_archive_dir)")"
GO_ROOT="$(resolve_path "$(read_config paths.go_root)")"
GO_PARENT="$(dirname "$GO_ROOT")"
GO_ARCHIVE="$GO_ARCHIVE_DIR/go${GO_VERSION}.${GO_PLATFORM}.tar.gz"
BUILD_BIN_DIR="$ROOT/build/bin"
LEGACY_GO_ROOT="$ROOT/artifacts/toolchains/go/current"
LEGACY_TMP_GO_ROOT="$ROOT/tmp/fuzzasterinas-go/current"

mkdir -p "$GO_ARCHIVE_DIR" "$GO_PARENT" "$BUILD_BIN_DIR"
rm -rf "$LEGACY_GO_ROOT"
rm -rf "$LEGACY_TMP_GO_ROOT"

if [[ ! -f "$GO_ARCHIVE" ]]; then
  curl -L "https://dl.google.com/go/go${GO_VERSION}.${GO_PLATFORM}.tar.gz" -o "$GO_ARCHIVE"
fi

rm -rf "$GO_PARENT"
mkdir -p "$GO_PARENT"
tar -xf "$GO_ARCHIVE" -C "$GO_PARENT"

export GOROOT="$GO_ROOT"
export PATH="$GOROOT/bin:$PATH"
export GO111MODULE=on
export GOBIN="$ROOT/build/bin"

if [[ ! -d "$SYZKALLER_DIR/.git" ]]; then
  git clone --filter=blob:none "$SYZKALLER_REPO" "$SYZKALLER_DIR"
fi

git -C "$SYZKALLER_DIR" fetch --depth 1 origin "$SYZKALLER_COMMIT"
git -C "$SYZKALLER_DIR" checkout --detach "$SYZKALLER_COMMIT"

CURRENT_COMMIT="$(git -C "$SYZKALLER_DIR" rev-parse HEAD)"
if [[ "$CURRENT_COMMIT" != "$SYZKALLER_COMMIT" ]]; then
  echo "syzkaller revision mismatch: expected $SYZKALLER_COMMIT got $CURRENT_COMMIT" >&2
  exit 1
fi

(cd "$SYZKALLER_DIR" && go install ./sys/syz-sysgen)
(cd "$SYZKALLER_DIR" && "$GOBIN/syz-sysgen" >/dev/null)
(cd "$SYZKALLER_DIR" && go build -o ./bin/syz-prog2c ./tools/syz-prog2c)
(cd "$ROOT" && go mod tidy)
(cd "$ROOT" && go build -o "$BUILD_BIN_DIR/syzabi_inspect" ./cmd/syzabi_inspect)
(cd "$ROOT" && go build -o "$BUILD_BIN_DIR/syzabi_generate" ./cmd/syzabi_generate)
(cd "$ROOT" && go build -o "$BUILD_BIN_DIR/syzabi_mutate" ./cmd/syzabi_mutate)

python3 "$ROOT/tools/init_layout.py"

echo "Bootstrap complete"
