"""Command line entry point."""

import argparse
import sys

from .jobs import JOBS, run_batch, run_job


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="stocksync")
    sub = parser.add_subparsers(dest="cmd", required=True)
    one = sub.add_parser("job", help="run one job")
    one.add_argument("name", choices=sorted(JOBS))
    sub.add_parser("nightly", help="run every nightly job")
    args = parser.parse_args(argv)
    if args.cmd == "job":
        print(run_job(args.name))
    else:
        for name, text in run_batch().items():
            print(text)
            print("-" * 40)
    return 0


if __name__ == "__main__":
    sys.exit(main())
