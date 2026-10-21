#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -f models/osnet_x0_25_msmt17.pth ]; then
  echo '缺少专用权重，参见 models/README.md。'
  exit 1
fi
export OMP_NUM_THREADS=4
export MKL_NUM_THREADS=4
exec sh run.sh --reid-osnet models/osnet_x0_25_msmt17.pth --data ./data-reid "$@"
