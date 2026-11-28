"""Compare single-frame and multi-frame gallery scores without changing a workspace."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from tracking3.engine import Encoder
from tracking3.track_rerank import compare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True, help='已有索引的工作区')
    parser.add_argument('--task', required=True, help='已有查询任务 ID')
    parser.add_argument('--output', type=Path, required=True, help='新建本地 JSON 文件，不覆盖')
    parser.add_argument('--osnet', type=Path, default=APP / 'models/osnet_x0_25_msmt17.pth',
                        help='与原任务相同的 OSNet 权重')
    parser.add_argument('--compare-gallery', action='store_true',
                        help='同时比较同组选帧的最高两个外观相似度均值')
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error('输出文件已经存在，请另用路径')

    import torch
    torch.set_num_threads(4)
    try:
        encoder = Encoder(osnet=args.osnet, device='cpu')
        result = compare(args.data, encoder, args.task, compare_gallery=args.compare_gallery)
        payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Publish a complete report atomically, including when another run
        # arrives between the first existence check and this one.
        with tempfile.TemporaryDirectory(dir=args.output.parent) as directory:
            temporary = Path(directory) / 'comparison.json'
            temporary.write_text(payload, encoding='utf-8')
            os.link(temporary, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')
    print(f'已比较 {result["candidate_count"]} 个候选：{args.output}')


if __name__ == '__main__':
    main()
