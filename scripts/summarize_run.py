"""Summarize the last local run: dataset rows, KV reports, simulated PPE charges."""
import collections
import glob
import json
import os


def load(pattern):
    return [json.load(open(f)) for f in sorted(glob.glob(pattern)) if not f.endswith("__metadata__.json")]


items = load("storage/datasets/default/*.json")
print(f"{len(items)} dataset item(s)")
print("  by status:", dict(collections.Counter(f"{i.get('status')}:{i.get('errorClass') or ''}" for i in items)))
kv = "storage/key_value_stores/default"
for k in ("REPORT.md", "SUMMARY.json", "OUTPUT.json", "CHUNKS.jsonl"):
    hits = [h for h in glob.glob(os.path.join(kv, k.split(".json")[0] + "*")) if "__metadata__" not in h]
    print(f"  KV {k}: {'present ' + str(os.path.getsize(hits[0])) + ' B' if hits else 'MISSING'}")
prices = {k: v["eventPriceUsd"] for k, v in json.load(open(".actor/pay_per_event.json")).items()}
counts = collections.Counter()
for e in load("storage/datasets/charging-log/*.json"):
    counts[e["event_name"]] += e["charged_count"]
total = 0.0
for k, n in counts.items():
    if k not in prices:
        print(f"  (simulated at $0) {k}: {n} x")
        continue
    total += n * prices[k]
    print(f"  charge {k}: {n} x ${prices[k]} = ${n * prices[k]:.5f}")
print(f"  TOTAL simulated custom-event charge: ${total:.5f}")
