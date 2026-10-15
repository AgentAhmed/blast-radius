"""Try the graph tools from a terminal.

    python -m blastradius.cli blast payment
    python -m blastradius.cli root-cause payment checkout frontend frontend-proxy
    python -m blastradius.cli changes checkout --hours 24
    python -m blastradius.cli page payment
    python -m blastradius.cli experts payment
"""
import argparse
import json

from blastradius import queries


def main() -> None:
    ap = argparse.ArgumentParser(prog="blastradius")
    ap.add_argument("--tenant", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("blast", help="Q1: what breaks if X goes down").add_argument("name")
    sub.add_parser("root-cause", help="Q2: rank root-cause candidates").add_argument("alerting", nargs="+")
    ch = sub.add_parser("changes", help="Q3: recent changes in X and its dependencies")
    ch.add_argument("name")
    ch.add_argument("--hours", type=int, default=24)
    sub.add_parser("page", help="Q4: who to page for X now").add_argument("name")
    sub.add_parser("experts", help="Q5: who knows X best").add_argument("name")
    args = ap.parse_args()

    if args.cmd == "blast":
        out = queries.blast_radius(args.name, args.tenant)
    elif args.cmd == "root-cause":
        out = queries.root_cause_candidates(args.alerting, args.tenant)
    elif args.cmd == "changes":
        out = queries.recent_changes(args.name, args.tenant, window_hours=args.hours)
    elif args.cmd == "page":
        out = queries.who_to_page(args.name, args.tenant)
    else:
        out = queries.experts(args.name, args.tenant)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
