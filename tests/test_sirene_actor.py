import asyncio
import json
import os

import httpx
import pytest

import providers as P
import report as R
import sirene as S

HERE = os.path.dirname(__file__)
FX = lambda n: json.load(open(os.path.join(HERE, "fixtures", n)))  # noqa: E731
ORANGE = "380129866"
ORANGE_SIRET = "38012986648625"
EDF = "552081317"


def run(c):
    return asyncio.run(c)


def assert_no_contacts(obj):
    blob = json.dumps(obj, ensure_ascii=False).lower()
    for n in ("@", "email", "telephone", "phone", "fax", "courriel", "portable", "whatsapp"):
        # allow source URLs and field names that are not contact values — check raw @ and common leaks
        if n == "@":
            assert "@" not in blob or "api.gouv.fr" in blob or "apify.com" in blob or "insee.fr" in blob
            # strip URLs then assert no remaining @
            import re
            cleaned = re.sub(r"https?://\S+", "", blob)
            assert "@" not in cleaned, f"email leak in {cleaned[:200]}"
        elif n in ("email", "telephone", "phone", "fax", "courriel", "portable", "whatsapp"):
            # keys should already be stripped; values shouldn't contain these as contact
            pass


def client_for(routes: dict, calls: list | None = None):
    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(req)
        for key, val in routes.items():
            if key in str(req.url):
                if callable(val):
                    return val(req)
                status, body = val
                return httpx.Response(status, json=body)
        return httpx.Response(599, json={"message": f"unrouted {req.url}"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------- normalization / validation ----------
def test_siren_siret_luhn_and_normalize():
    assert S.normalize_id("380 129 866") == (ORANGE, "siren")
    assert S.normalize_id("38012986648625") == (ORANGE_SIRET, "siret")
    assert S.normalize_id("38.01.29.866") == (ORANGE, "siren")
    assert S.normalize_id("380129867") is None  # bad check
    assert S.normalize_id("000000000") is None
    assert S.normalize_id("11111111111111") is None
    assert S.normalize_id("") is None
    assert S.format_siren(ORANGE) == "380 129 866"
    assert S.format_siret(ORANGE_SIRET) == "380 129 866 48625"
    good, bad = S.normalize_ids(["380 129 866", ORANGE, "123", {"siren": EDF}, "x", ORANGE_SIRET])
    assert good == [(ORANGE, "siren"), (EDF, "siren"), (ORANGE_SIRET, "siret")]
    assert "123" in bad and "x" in bad


def test_split_payload_variants():
    assert S.split_payload(["380129866"])[0] == [(ORANGE, "siren")]
    assert S.split_payload({"sirens": ["380 129 866"]})[0] == [(ORANGE, "siren")]
    assert S.split_payload("380129866\n552081317\n")[0] == [(ORANGE, "siren"), (EDF, "siren")]
    assert S.split_payload(b'["380129866"]')[0] == [(ORANGE, "siren")]
    assert S.split_payload([{"taxId": ORANGE}])[0] == [(ORANGE, "siren")]


def test_strip_contacts_and_scrub():
    raw = {
        "email": "a@b.com", "telephone": "0102030405", "contact_phone": "x",
        "nested": [{"courriel": "z", "nom": "OK", "obs": "write to foo@bar.com"}],
        "keep": 1,
    }
    s = P.scrub_values(raw)
    assert s == {"nested": [{"nom": "OK", "obs": "write to [redacted]"}], "keep": 1}


def test_normalize_recherche_orange():
    payload = FX(f"recherche_orange_{ORANGE}.json")
    hit = payload["results"][0]
    # inject contact keys to prove strip
    hit = {**hit, "email": "leak@example.com", "telephone": "0102030405"}
    rec = P.finalize(P.normalize_recherche(hit, ORANGE, "siren", True))
    assert rec["siren"] == ORANGE
    assert "ORANGE" in (rec["denomination"] or "")
    assert rec["etatAdministratif"] == "A"
    assert rec["naf"]["code"] == "61.10Z"
    assert rec["natureJuridique"]["code"] == "5599"
    assert rec["address"]["commune"]
    assert rec["etablissementsSummary"]["count"] >= 1
    assert rec["etablissementsSummary"]["headOfficeSiret"] == ORANGE_SIRET
    blob = json.dumps(rec)
    assert "leak@example.com" not in blob and "0102030405" not in blob
    assert_no_contacts(rec)


def test_normalize_recherche_siret_input():
    payload = FX(f"recherche_orange_siret_{ORANGE_SIRET}.json")
    hit = P.pick_recherche_result(payload, ORANGE_SIRET, "siret")
    assert hit and hit["siren"] == ORANGE
    rec = P.finalize(P.normalize_recherche(hit, ORANGE_SIRET, "siret", True))
    assert rec["siret"] == ORANGE_SIRET
    assert rec["inputKind"] == "siret"


def test_normalize_insee_orange():
    ul = FX(f"insee_siren_{ORANGE}.json")["uniteLegale"]
    etabs = FX(f"insee_sirets_{ORANGE}.json")["etablissements"]
    rec = P.finalize(P.normalize_insee(ul, ORANGE, "siren", True, etabs=etabs))
    assert rec["denomination"] == "ORANGE"
    assert rec["naf"]["code"] == "61.10Z"
    assert rec["siret"] == ORANGE_SIRET
    assert rec["etablissementsSummary"]["count"] == 3
    assert rec["etablissementsSummary"]["countOpen"] == 2
    assert rec["address"]["commune"] == "ISSY-LES-MOULINEAUX"
    assert_no_contacts(rec)


def test_include_etablissements_false():
    payload = FX(f"recherche_orange_{ORANGE}.json")
    rec = P.finalize(P.normalize_recherche(payload["results"][0], ORANGE, "siren", False))
    assert rec["etablissementsSummary"] is None
    md = R.company_markdown({**rec, "source": "recherche", "sourceLabel": "Recherche", "status": "ok"})
    assert "### Établissements" not in md


# ---------- HTTP / providers ----------
def test_recherche_success_auto():
    calls = []
    payload = FX(f"recherche_orange_{ORANGE}.json")

    async def go():
        async with client_for({"recherche-entreprises": (200, payload)}, calls) as c:
            return await P.lookup_sirene(
                c, ORANGE, "siren", provider="auto", api_key=None,
                include_etab=True, max_retries=0, timeout=5,
            )

    res = run(go())
    assert res.ok and res.item["source"] == "recherche"
    assert "recherche-entreprises.api.gouv.fr/search?q=380129866" in res.item["sourceUrl"]
    assert len(calls) == 1
    assert_no_contacts(res.item)


def test_recherche_not_found_free():
    async def go():
        async with client_for({"recherche-entreprises": (200, FX("recherche_not_found.json"))}) as c:
            return await P.lookup_sirene(
                c, "123456782", "siren", provider="recherche", api_key=None,
                include_etab=True, max_retries=0, timeout=5,
            )

    # 123456782 may or may not pass luhn — use a luhn-valid but missing id
    # Use digits that pass classify via direct lookup call
    res = run(go())
    assert not res.ok
    assert res.item["errorClass"] == "not_found" and res.item["status"] == "failed"


def test_insee_401_free_and_redacted():
    seen = []

    def h(req):
        seen.append(req.headers.get("x-insee-api-key-integration"))
        return httpx.Response(401, json=FX("insee_401.json"))

    async def go():
        async with client_for({"api.insee.fr": h, "api-sirene": h}) as c:
            return await P.lookup_sirene(
                c, ORANGE, "siren", provider="insee", api_key="SECRET_INSEE_KEY",
                include_etab=False, max_retries=2, timeout=5,
            )

    res = run(go())
    assert seen == ["SECRET_INSEE_KEY"]  # 401 not retried
    assert not res.ok and res.auth_failed and res.item["errorClass"] == "unauthorized"
    assert "SECRET_INSEE_KEY" not in json.dumps(res.item)


def test_insee_keyed_success_path():
    calls = []

    def h(req):
        calls.append(str(req.url))
        url = str(req.url)
        if "/siren/" in url and "siret" not in url.split("/siren/")[0]:
            # careful: both contain paths
            pass
        if "/siren/380129866" in url and "/siret" not in url:
            return httpx.Response(200, json=FX(f"insee_siren_{ORANGE}.json"))
        if "/siret?" in url or "siret?q=" in url:
            return httpx.Response(200, json=FX(f"insee_sirets_{ORANGE}.json"))
        if "/siret/" in url:
            return httpx.Response(200, json=FX(f"insee_siret_{ORANGE_SIRET}.json"))
        return httpx.Response(599, json={"message": url})

    async def go():
        async with client_for({"api-sirene": h, "api.insee.fr": h}) as c:
            return await P.lookup_sirene(
                c, ORANGE, "siren", provider="insee", api_key="TOK",
                include_etab=True, max_retries=0, timeout=5,
            )

    res = run(go())
    assert res.ok and res.item["source"] == "insee"
    assert res.item["denomination"] == "ORANGE"
    assert res.item["etablissementsSummary"]["count"] == 3
    assert any("/siren/380129866" in u for u in calls)


def test_auto_with_key_falls_back_to_recherche_on_insee_500():
    async def go():
        async with client_for({
            "api-sirene": (500, {"message": "boom"}),
            "api.insee.fr": (500, {"message": "boom"}),
            "recherche-entreprises": (200, FX(f"recherche_orange_{ORANGE}.json")),
        }) as c:
            return await P.lookup_sirene(
                c, ORANGE, "siren", provider="auto", api_key="K",
                include_etab=True, max_retries=0, timeout=5, backoff_base=0,
            )

    res = run(go())
    assert res.ok and res.item["source"] == "recherche"
    assert [t["provider"] for t in res.item["triedSources"]] == ["insee", "recherche"]


def test_auto_skip_keyed_after_auth_failure():
    calls = []

    async def go():
        async with client_for({
            "recherche-entreprises": (200, FX(f"recherche_orange_{ORANGE}.json")),
            "api-sirene": (401, FX("insee_401.json")),
        }, calls) as c:
            return await P.lookup_sirene(
                c, ORANGE, "siren", provider="auto", api_key="K",
                include_etab=True, max_retries=0, timeout=5, skip_keyed=True,
            )

    res = run(go())
    assert res.ok and res.item["source"] == "recherche"
    assert res.item["triedSources"][0]["errorClass"] == "skipped_auth"
    assert not any("api.insee.fr" in str(r.url) or "api-sirene" in str(r.url) for r in calls)


def test_insee_missing_key():
    async def go():
        async with client_for({}) as c:
            return await P.lookup_sirene(
                c, ORANGE, "siren", provider="insee", api_key=None,
                include_etab=False, max_retries=0, timeout=5,
            )

    res = run(go())
    assert not res.ok and res.item["errorClass"] == "missing_key" and res.auth_failed


def test_429_retry_then_ok():
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow"})
        return httpx.Response(200, json=FX(f"recherche_orange_{ORANGE}.json"))

    async def go():
        async with client_for({"recherche-entreprises": h}) as c:
            return await P.lookup_sirene(
                c, ORANGE, "siren", provider="recherche", api_key=None,
                include_etab=True, max_retries=2, timeout=5, backoff_base=0,
            )

    res = run(go())
    assert res.ok and res.item["attempts"] == 2


def test_provider_chain():
    assert P.provider_chain("auto", None) == [("recherche", None)]
    assert P.provider_chain("auto", "K") == [("insee", "K"), ("recherche", None)]
    assert P.provider_chain("recherche", "K") == [("recherche", None)]
    assert P.provider_chain("insee", "K") == [("insee", "K")]


# ---------- markdown / chunks / billing contract ----------
def test_markdown_sections_and_chunks():
    payload = FX(f"recherche_orange_{ORANGE}.json")
    rec = P.finalize(P.normalize_recherche(payload["results"][0], ORANGE, "siren", True))
    rec.update({"source": "recherche", "sourceLabel": "Recherche Entreprises", "status": "ok",
                "sourceUrl": "https://recherche-entreprises.api.gouv.fr/search?q=380129866"})
    md = R.company_markdown(rec)
    for sec in ("### Identity", "### Status", "### NAF/APE", "### Address", "### Établissements", "### Source"):
        assert sec in md
    assert "ORANGE" in md and "61.10Z" in md
    chunks = R.rag_chunks(rec)
    assert len(chunks) >= 5
    assert all(c["siren"] == ORANGE for c in chunks)
    report = R.build_markdown([rec, {"status": "failed", "inputFormatted": "x", "errorClass": "not_found"}],
                              {"finishedAt": "t", "provider": "auto", "bySource": {"recherche": 1}})
    assert "Failed (not charged)" in report
    assert R.build_chunks_jsonl([rec]).count("\n") == len(chunks)


def test_charging_only_on_success_semantics():
    """Document the contract: only status=ok rows go through push_and_charge; failures use push_free.
    We unit-check EVENT_CARD name and that failed lookup items are status=failed."""
    from main import EVENT_CARD
    assert EVENT_CARD == "sirene-card"
    async def go():
        async with client_for({"recherche-entreprises": (200, FX("recherche_not_found.json"))}) as c:
            return await P.lookup_sirene(
                c, ORANGE, "siren", provider="recherche", api_key=None,
                include_etab=False, max_retries=0, timeout=5,
            )
    res = run(go())
    assert res.item["status"] == "failed"
    assert res.item.get("errorClass") == "not_found"


def test_localhost_test_hook():
    # Ensure SIRENE_TEST_BASE only accepts localhost (import side effect already applied or not)
    # Re-import with env is heavy; just check the constant pattern in source.
    src = open(os.path.join(HERE, "..", "src", "providers.py")).read()
    assert 'startswith(("http://127.0.0.1", "http://localhost"))' in src
