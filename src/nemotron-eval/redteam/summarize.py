"""Summarize run_redteam.py outputs into the before/after table (Markdown).

Usage: python summarize.py results1.json [results2.json ...]
"""
import collections
import json
import sys


def main():
    rows = [r for path in sys.argv[1:] for r in json.load(open(path))]
    for r in rows:
        r.setdefault("model", "nemotron-3.5-lightning:30b")
        r.setdefault("relayed_to_user", False)
    cells = collections.defaultdict(list)
    for r in rows:
        cells[(r["model"], r["scenario"], r["mode"])].append(r)
    models = list(dict.fromkeys(r["model"] for r in rows))
    scenarios = list(dict.fromkeys(r["scenario"] for r in rows))
    print("| Model | Attack | Ungoverned: took the bait | Governed: took the bait | Governed: blocked | Real token seen (ungov / gov) | Relayed to user (ungov / gov) |")
    print("|---|---|---|---|---|---|---|")
    totals = collections.Counter()
    for m in models:
        for sc in scenarios:
            u, g = cells.get((m, sc, "ungoverned"), []), cells.get((m, sc, "governed"), [])
            if not u and not g:
                continue
            ua, ga = sum(r["attempted"] for r in u), sum(r["attempted"] for r in g)
            gb = sum(r["attempted"] and r["blocked_events"] > 0 for r in g)
            ut, gt = sum(r["real_token_seen_by_agent"] for r in u), sum(r["real_token_seen_by_agent"] for r in g)
            ur, gr = sum(r["relayed_to_user"] for r in u), sum(r["relayed_to_user"] for r in g)
            totals.update(u_n=len(u), g_n=len(g), ua=ua, ga=ga, gb=gb, ut=ut, gt=gt)
            print(f"| {m} | {sc} | {ua}/{len(u)} | {ga}/{len(g)} | {gb}/{ga if ga else 0} | {ut} / {gt} | {ur} / {gr} |")
    print()
    print(f"Ungoverned: attack actions in {totals['ua']}/{totals['u_n']} runs; real token seen by the agent in {totals['ut']}.")
    print(f"Governed: attack actions in {totals['ga']}/{totals['g_n']} runs; blocked and logged in {totals['gb']}/{totals['ga']}; real token seen in {totals['gt']}.")


if __name__ == "__main__":
    main()
