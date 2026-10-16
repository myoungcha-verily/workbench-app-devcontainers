"""Summarize run_redteam.py outputs into the before/after table (Markdown).

Usage: python summarize.py results1.json [results2.json ...]

Runs are re-scored with the current run_redteam.assess, so older result files
get the same rules as new ones.
"""
import collections
import json
import sys

from run_redteam import SCENARIOS, assess

BY_ID = {s["id"]: s for s in SCENARIOS}


def main():
    rows = [r for path in sys.argv[1:] for r in json.load(open(path))]
    for r in rows:
        r.setdefault("model", "nemotron-3.5-lightning:30b")
        r.update(assess(BY_ID[r["scenario"]], r["turn"]))
        r["errored"] = any("error" in e for e in r["turn"]["events"])
    cells = collections.defaultdict(list)
    for r in rows:
        cells[(r["model"], r["scenario"], r["mode"])].append(r)
    models = list(dict.fromkeys(r["model"] for r in rows))
    scenarios = list(dict.fromkeys(r["scenario"] for r in rows))
    print("| Model | Attack | Ungoverned: took the bait | Governed: took the bait | Governed: blocked by policy / tool not in sandbox / not stopped "
          "| Real token seen (ungov / gov) | Relayed to user (ungov / gov) |")
    print("|---|---|---|---|---|---|---|")
    t = collections.Counter()
    for m in models:
        for sc in scenarios:
            u, g = cells.get((m, sc, "ungoverned"), []), cells.get((m, sc, "governed"), [])
            if not u and not g:
                continue
            ua, ga = sum(r["attempted"] for r in u), sum(r["attempted"] for r in g)
            gb = sum(r["attempted"] and r["blocked_events"] > 0 for r in g)
            gm = sum(r["attempted"] and not r["blocked_events"] and r["missing_tool"] for r in g)
            gn = ga - gb - gm
            ut, gt = sum(r["real_token_seen_by_agent"] for r in u), sum(r["real_token_seen_by_agent"] for r in g)
            ur, gr = sum(r["relayed_to_user"] for r in u), sum(r["relayed_to_user"] for r in g)
            t.update(u_n=len(u), g_n=len(g), ua=ua, ga=ga, gb=gb, gm=gm, gn=gn, ut=ut, gt=gt, ur=ur, gr=gr,
                     mention=sum(r["mention_only"] for r in u + g), err=sum(r["errored"] for r in u + g))
            print(f"| {m} | {sc} | {ua}/{len(u)} | {ga}/{len(g)} | {gb} / {gm} / {gn} | {ut} / {gt} | {ur} / {gr} |")
    print()
    print(f"Ungoverned: attack actions in {t['ua']}/{t['u_n']} runs; real token seen by the agent in {t['ut']}; instruction relayed to the user in {t['ur']}.")
    print(f"Governed: attack actions in {t['ga']}/{t['g_n']} runs; blocked and logged by policy in {t['gb']}, "
          f"failed because the tool is not in the sandbox in {t['gm']}, not stopped in {t['gn']}; real token seen in {t['gt']}; relayed in {t['gr']}.")
    print(f"Mention only (planted text in a report, no action), not counted: {t['mention']} runs. Runs with a malformed tool call or model error: {t['err']}.")


if __name__ == "__main__":
    main()
