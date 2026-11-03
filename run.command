#!/bin/bash
# Finder 可双击；终端可 ./run.command --stage 1。仅使用现有 Miniforge 虚拟环境。
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MMBOT_PYTHON="${MMBOT_PYTHON:-/opt/homebrew/Caskroom/miniforge/base/envs/option/bin/python}"
case "$MMBOT_PYTHON" in
  */envs/*/bin/python*) ;;
  *) echo 'MMBOT_PYTHON 必须指向现有虚拟环境的 Python，不能使用系统 Python 或 base。' >&2; exit 1 ;;
esac
if [[ ! -x "$MMBOT_PYTHON" ]]; then
  echo "找不到环境解释器：$MMBOT_PYTHON。请设置 MMBOT_PYTHON 为已有 Miniforge 环境的完整路径。" >&2
  exit 1
fi
cd "$PROJECT_ROOT"
exec "$MMBOT_PYTHON" -m mmbot "$@"
