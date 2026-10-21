"""Export one task's version-consistent evaluation inputs without a server."""
import argparse
import json
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from tracking3.evaluation_export import export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',required=True)
    parser.add_argument('--task',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--expected-task-version',type=int)
    parser.add_argument('--limit',type=int,help='留空导出当前范围全部候选；显式限制会记录截断')
    args = parser.parse_args()
    result = export(args.data,args.task,args.output,
                    expected_task_version=args.expected_task_version,limit=args.limit)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
