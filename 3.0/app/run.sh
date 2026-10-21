#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo '请先按 README.md 创建 Python 3.12 虚拟环境并安装依赖。'
  exit 1
fi
exec .venv/bin/python -m tracking3.server "$@"
