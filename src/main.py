"""Apify Actor: France SIREN/SIRET list → Markdown due-diligence cards (+ RAG chunks).

Providers: Recherche Entreprises (keyless, default) and buyer BYOK INSEE API Sirene
(X-INSEE-Api-Key-Integration). Official / government APIs only. Never outputs email / telephone.
Output: dataset row per id + KV REPORT.md, SUMMARY, OUTPUT, optional CHUNKS.jsonl.
Pay-per-event: `sirene-card` per successful company card. Invalid / not found / failed rows are free.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import resource
import time
from collections import Counter

import httpx
from apify import Actor

from charging import Charger
from providers import KEY_HINT, UA, lookup_sirene, scrub_values
from report import build_chunks_jsonl, build_markdown, company_markdown
from sirene import format_id, id_from_item, normalize_ids, split_payload

EVENT_CARD = "sirene-card"
PROVIDERS = ("auto", "recherche", "insee")
PLACEHOLDER_KEYS = {"", "YOUR_KEY", "YOUR_TOKEN", "xxx", "TODO", "changeme"}

for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def _int(inp: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = inp.get(key)
    try:
        v = int(default if v is None or v == "" else v)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def _clean_key(raw) -> str | None:
    if not raw:
        return None
    k = str(raw).strip()
    return None if k in PLACEHOLDER_KEYS or k.upper() in PLACEHOLDER_KEYS else k


async def load_from_dataset(dataset_id: str, field: str | None, limit: int | None) -> tuple[list, list[str]]:
    vals: list = []
    try:
        ds = await Actor.open_dataset(id=dataset_id, force_cloud=bool(os.getenv("APIFY_IS_AT_HOME")))
        async for item in ds.iterate_items():
            if isinstance(item, dict):
                v = id_from_item(item, field)
                if v is not None:
                    vals.append(v)
            if limit and len(vals) >= limit:
                break
    except Exception as e:  # noqa: BLE001
        return [], [f"Could not read dataset {dataset_id}: {e}"]
    return vals, ([] if vals else [f"No SIREN/SIRET fields found in dataset {dataset_id}"])


async def load_from_kv(store_id: str, key: str, field: str | None) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    try:
        store = await Actor.open_key_value_store(id=store_id, force_cloud=bool(os.getenv("APIFY_IS_AT_HOME")))
        rec = await store.get_value(key)
    except Exception as e:  # noqa: BLE001
        return [], [], [f"Could not read KV {store_id}/{key}: {e}"]
    if rec is None:
        return [], [], [f"KV record {key} not found in store {store_id}"]
    good, bad = split_payload(rec, field)
    return good, bad, ([] if good or bad else [f"No SIREN/SIRET found in KV record {key}"])


async def main() -> None:
    async with Actor:
        t0 = time.time()
        inp = await Actor.get_input() or {}
        charger = Charger(EVENT_CARD)

        provider = str(inp.get("provider") or "auto").strip().lower()
        if provider not in PROVIDERS:
            raise ValueError(f"`provider` must be one of {'|'.join(PROVIDERS)}, got {provider!r}")
        api_key = _clean_key(inp.get("apiKey"))
        key_source = "input" if api_key else None
        # Private smoke only — never set as Actor env vars
        if not api_key and os.getenv("INSEE_API_KEY", "").strip() and provider in ("auto", "insee"):
            api_key, key_source = os.environ["INSEE_API_KEY"].strip(), "env:INSEE_API_KEY"
        if provider == "recherche" and api_key:
            Actor.log.info("provider=recherche is keyless; apiKey is ignored.")
            api_key = None

        include_etab = bool(inp.get("includeEtablissements", True))
        include_chunks = bool(inp.get("includeRagChunks", True))
        max_items = _int(inp, "maxItems", 100, 0, 100000) or None
        conc = _int(inp, "maxConcurrency", 2, 1, 5)
        max_retries = _int(inp, "maxRetries", 3, 0, 6)
        timeout = float(_int(inp, "requestTimeoutSecs", 30, 5, 120))
        stop_on_auth = bool(inp.get("stopOnAuthError", True))
        field = (str(inp.get("sirenField") or "").strip() or None)

        raw_vals = list(inp.get("sirens") or []) if not isinstance(inp.get("sirens"), str) else [inp["sirens"]]
        warnings: list[str] = []
        if inp.get("datasetId"):
            v, w = await load_from_dataset(str(inp["datasetId"]), field, max_items)
            raw_vals += v
            warnings += w
            Actor.log.info(f"Loaded {len(v)} value(s) from dataset {inp['datasetId']}")
        ids, bad = normalize_ids(raw_vals, field)
        if inp.get("keyValueStoreId"):
            key = str(inp.get("keyValueRecordKey") or "INPUT_SIRENS")
            g, b, w = await load_from_kv(str(inp["keyValueStoreId"]), key, field)
            # merge unique
            seen = {d for d, _ in ids}
            for d, k in g:
                if d not in seen:
                    ids.append((d, k))
                    seen.add(d)
            bad += b
            warnings += w
            Actor.log.info(f"Loaded {len(g)} id(s) from KV {inp['keyValueStoreId']}/{key}")
        for w in warnings:
            Actor.log.warning(w)
        if max_items is not None and len(ids) > max_items:
            Actor.log.warning(f"{len(ids)} ids; capped to maxItems={max_items}.")
            ids = ids[:max_items]
        if not ids and not bad:
            raise ValueError("Provide at least one SIREN/SIRET in `sirens` (or a datasetId / keyValueStoreId).")

        Actor.log.info(
            f"{len(ids)} id(s) (+{len(bad)} invalid); provider={provider}; concurrency={conc}; "
            f"key={'yes (' + key_source + ', value hidden)' if api_key else 'no (keyless recherche-entreprises)'}"
        )
        await Actor.set_status_message(f"Looking up {len(ids)} SIREN/SIRET via {provider}…")

        rows: list[tuple[int, dict]] = []
        cls_counts: Counter[str] = Counter()
        src_counts: Counter[str] = Counter()
        state = {"done": 0, "ok": 0, "fail": 0, "skipped": 0, "inflight": 0, "auth_failed": False}

        for i, raw in enumerate(bad):
            item = {
                "siren": None, "siret": None, "inputId": raw, "inputFormatted": raw,
                "status": "failed", "source": "none",
                "errorClass": "invalid_input", "httpStatus": None, "attempts": 0,
                "errorMessage": "Not a valid SIREN (9) or SIRET (14) after normalization (Luhn check digit).",
                "hint": "Use 9-digit SIREN or 14-digit SIRET (punctuation is fine). Check digits use the French Luhn rule.",
            }
            rows.append((-100000 + i, item))
            cls_counts["invalid_input"] += 1
            state["fail"] += 1
            await charger.push_free(item)

        sem = asyncio.Semaphore(conc)
        lock = asyncio.Lock()
        async with httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept": "application/json"},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=conc + 2),
        ) as client:

            async def one(idx: int, digits: str, kind: str) -> None:
                async with sem:
                    async with lock:
                        if charger.limit_reached:
                            state["skipped"] += 1
                            return
                        b = charger.budget()
                        if b is not None and b - state["inflight"] <= 0:
                            state["skipped"] += 1
                            charger.limit_reached = True
                            return
                        skip_keyed = stop_on_auth and state["auth_failed"]
                        state["inflight"] += 1
                    try:
                        res = await lookup_sirene(
                            client, digits, kind, provider=provider, api_key=api_key,
                            include_etab=include_etab, max_retries=max_retries, timeout=timeout,
                            skip_keyed=skip_keyed,
                        )
                        if res.auth_failed and not state["auth_failed"]:
                            state["auth_failed"] = True
                            Actor.log.warning(
                                "INSEE rejected the API key (401/403)"
                                + ("; skipping INSEE for remaining ids." if stop_on_auth else ".")
                            )
                    finally:
                        async with lock:
                            state["inflight"] -= 1
                    item = res.item
                    item["checkedAt"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
                    if res.ok:
                        item["markdown"] = company_markdown(item)
                    item = scrub_values(item)
                    async with lock:
                        state["done"] += 1
                        if res.ok:
                            n = await charger.push_and_charge([item], event=EVENT_CARD)
                            if n:
                                state["ok"] += 1
                                cls_counts["ok"] += 1
                                src_counts[item.get("source") or "unknown"] += 1
                                rows.append((idx, item))
                            else:
                                state["skipped"] += 1
                        else:
                            state["fail"] += 1
                            cls_counts[item.get("errorClass") or "error"] += 1
                            rows.append((idx, item))
                            await charger.push_free(item)
                        Actor.log.info(
                            f"[{state['done']}/{len(ids)}] {format_id(digits, kind)}: "
                            + (
                                f"OK {item.get('denomination')} [{item.get('etatLabel')}] via {item.get('source')}"
                                if res.ok else
                                f"FAILED {item.get('errorClass')} (HTTP {item.get('httpStatus')}, free)"
                            )
                        )
                        await Actor.set_status_message(
                            f"{state['done']}/{len(ids)} done — ok {state['ok']}, failed {state['fail']}"
                        )

            await asyncio.gather(*(one(i, d, k) for i, (d, k) in enumerate(ids)))

        rows.sort(key=lambda t: t[0])
        out_rows = [r for _, r in rows]
        peak_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
        summary = {
            "provider": provider,
            "apiKeyProvided": bool(api_key),
            "idsPlanned": len(ids),
            "invalidInputs": len(bad),
            "idsOk": state["ok"],
            "idsFailed": state["fail"],
            "skipped": state["skipped"],
            "byClass": dict(cls_counts),
            "bySource": dict(src_counts),
            "includeEtablissements": include_etab,
            "includeRagChunks": include_chunks,
            "authFailed": state["auth_failed"],
            "keyHint": KEY_HINT if (cls_counts.get("rate_limited") or state["auth_failed"] or cls_counts.get("missing_key")) else None,
            "charged": dict(charger.counts),
            "spendingLimitReached": charger.limit_reached,
            "durationSecs": round(time.time() - t0, 1),
            "peakMemoryMb": peak_mb,
            "finishedAt": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
        store = await Actor.open_key_value_store()
        await store.set_value("REPORT.md", build_markdown(out_rows, summary), content_type="text/markdown; charset=utf-8")
        chunk_count = 0
        if include_chunks:
            jsonl = build_chunks_jsonl(out_rows)
            chunk_count = jsonl.count("\n")
            await store.set_value("CHUNKS.jsonl", jsonl, content_type="application/x-ndjson; charset=utf-8")
        summary["ragChunks"] = chunk_count
        await store.set_value("SUMMARY", summary)
        await store.set_value("OUTPUT", {
            "summary": summary,
            "companies": [{
                k: r.get(k) for k in (
                    "siren", "siret", "status", "denomination", "nomComplet",
                    "etatAdministratif", "etatLabel", "source", "errorClass",
                )
            } for r in out_rows],
        })
        msg = (
            f"Done: {state['ok']} card(s) OK, {state['fail']} failed (free), {state['skipped']} skipped. "
            f"Charged: {charger.counts or 'nothing'}."
        )
        Actor.log.info(msg + f" Peak memory {peak_mb} MB, {summary['durationSecs']} s.")
        await Actor.set_status_message(msg, is_terminal=True)


if __name__ == "__main__":
    asyncio.run(main())
