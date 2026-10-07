#!/usr/bin/env bash
# Local Actor run with simulated PPE (Apify SDK). Usage: ./scripts/local_test.sh [input.json]
set -euo pipefail
cd "$(dirname "$0")/.."
INPUT="${1:-test-inputs/live-local.json}"
export APIFY_LOCAL_STORAGE_DIR="${APIFY_LOCAL_STORAGE_DIR:-./storage}"
export PYTHONPATH=src
mkdir -p "$APIFY_LOCAL_STORAGE_DIR"
python3 - << PY
import asyncio, json, os, sys
sys.path.insert(0, "src")
from apify import Actor
# Write INPUT
inp = json.load(open("$INPUT"))
os.makedirs("storage/key_value_stores/default", exist_ok=True)
json.dump(inp, open("storage/key_value_stores/default/INPUT.json", "w"))
print("INPUT:", json.dumps(inp)[:200])
from main import main
asyncio.run(main())
PY
