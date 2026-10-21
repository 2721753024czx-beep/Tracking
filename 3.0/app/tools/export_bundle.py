"""Export a Tracking 3.0 report/evidence bundle without a browser."""

import argparse
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from tracking3.bundle import create
from tracking3.server import report
from tracking3.store import Store


def main():
    parser = argparse.ArgumentParser(description="导出 Tracking 3.0 任务证据包")
    parser.add_argument("--data", default=str(APP / "data"), help="工作台 data 目录")
    parser.add_argument("--task", required=True, help="任务 ID")
    parser.add_argument("--include-media", action="store_true",
                        help="显式复制 SHA 校验有效的源录像；默认只打包截图和元数据")
    args = parser.parse_args()
    store = Store.open_readonly(args.data)
    try:
        report_path = report(store, args.task)
        bundle_path = create(store, args.task, report_path, args.include_media)
        print(store.root / bundle_path)
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
