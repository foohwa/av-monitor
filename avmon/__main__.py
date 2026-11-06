import argparse
import sys

from avmon.run import fetch_all, report
from avmon.web import export_static, serve


def main() -> int:
    parser = argparse.ArgumentParser(description="Check monitoring data for saved actresses")
    parser.add_argument("command", choices=("fetch", "show", "serve", "export"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--out", default="docs")
    args = parser.parse_args()
    if args.command == "fetch":
        fetch_all()
    elif args.command == "show":
        report()
    elif args.command == "export":
        count = export_static(args.out)
        print(f"wrote {count} pages to {args.out}")
    else:
        serve(port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
