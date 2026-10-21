"""Generate a read-only, source-bound shared-event time calibration review."""
import argparse
import json
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from tracking3.calibration_review import prepare, LABELS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = prepare(args.data, args.input, args.output)
    print(json.dumps({k:result[k] for k in ('status','point_count','median_offset_to_reference_s','observed_offset_range_s','time_metadata_changed')},ensure_ascii=False))


if __name__=='__main__': main()
