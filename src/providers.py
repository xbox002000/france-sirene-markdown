"""French company JSON providers → one normalized, contact-free SIRENE card.

Providers (official / government JSON over HTTPS only — no HTML scraping):
- recherche : https://recherche-entreprises.api.gouv.fr/search?q={siren|siret}  (keyless, default)
- insee     : https://api.insee.fr/api-sirene/3.11/siren/{siren}
              https://api.insee.fr/api-sirene/3.11/siret/{siret}
              https://api.insee.fr/api-sirene/3.11/siret?q=siren:{siren}&nombre=N
              Header: X-INSEE-Api-Key-Integration (buyer BYOK from portail-api.insee.fr)

Email / telephone / contact keys are dropped recursively from every payload BEFORE normalization.
No lead / contact fields are ever emitted.
"""
from __future__ import annotations

import asyncio
import os
import random
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from sirene import format_id, format_siren, format_siret, siren_of

UA = "Mozilla/5.0 (compatible; FranceSireneMarkdown/0.1; +https://apify.com)"

BASES = {
    "recherche": "https://recherche-entreprises.api.gouv.fr",
    "insee": "https://api.insee.fr/api-sirene/3.11",
}
_test_base = os.getenv("SIRENE_TEST_BASE", "")
if _test_base.startswith(("http://127.0.0.1", "http://localhost")):
    BASES = {k: f"{_test_base.rstrip('/')}/{k}" for k in BASES}

SOURCE_LABELS = {
    "recherche": "Recherche Entreprises (api.gouv.fr)",
    "insee": "INSEE API Sirene",
}

KEY_HINT = (
    "For production volume and richer INSEE fields, bring your own free INSEE API key from "
    "https://portail-api.insee.fr (subscribe to API Sirene, then use the integration key as apiKey). "
    "The default keyless path uses https://recherche-entreprises.api.gouv.fr. Failed rows are not charged."
)

INSEE_APPLY_URL = "https://portail-api.insee.fr"

# --- contact stripping --------------------------------------------------------------------------
_CONTACT_KEY_RE = re.compile(
    r"(e_?mail|tele?phone|phone|fax|ddd|celular|mobile|whats|contato|contact|courriel|portable)",
    re.I,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def strip_contacts(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: strip_contacts(v) for k, v in obj.items() if not _CONTACT_KEY_RE.search(str(k))}
    if isinstance(obj, list):
        return [strip_contacts(v) for v in obj]
    return obj


def scrub_values(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: scrub_values(v) for k, v in strip_contacts(obj).items()}
    if isinstance(obj, list):
        return [scrub_values(v) for v in obj]
    if isinstance(obj, str) and _EMAIL_RE.search(obj):
        return _EMAIL_RE.sub("[redacted]", obj)
    return obj


def redact(text: str, secret: str | None) -> str:
    if not text:
        return text
    if secret:
        text = text.replace(secret, "***")
    return re.sub(
        r"(?i)(bearer\s+|x-insee-api-key-integration[=:]\s*|apikey[=:]\s*|token=)[^\s&\"']+",
        r"\1***",
        text,
    )


# --- small normalizers --------------------------------------------------------------------------
def _s(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _date(v: Any) -> str | None:
    s = _s(v)
    if not s:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else s


# État administratif: A = active, C = ceased
ETAT_LABELS = {"A": "Active (A)", "C": "Cessée (C)", "F": "Fermé (F)"}

# Common nature juridique (INSEE codes) — small subset for readability; unknown codes show as-is.
NATURE_JURIDIQUE = {
    "1000": "Entrepreneur individuel",
    "5499": "SARL",
    "5498": "EURL",
    "5710": "SAS",
    "5720": "SASU",
    "5599": "SA à conseil d'administration",
    "5699": "SA à directoire",
    "5800": "Société européenne",
    "5202": "Société en nom collectif",
    "5306": "Société en commandite simple",
    "5308": "Société en commandite par actions",
    "9220": "Association déclarée",
    "7343": "Commune",
    "7389": "Établissement public",
    "7112": "Autorité administrative indépendante",
}

# Tranche d'effectifs INSEE
TRANCHE_EFFECTIFS = {
    "NN": "Non renseigné",
    "00": "0 salarié",
    "01": "1 ou 2 salariés",
    "02": "3 à 5 salariés",
    "03": "6 à 9 salariés",
    "11": "10 à 19 salariés",
    "12": "20 à 49 salariés",
    "21": "50 à 99 salariés",
    "22": "100 à 199 salariés",
    "31": "200 à 249 salariés",
    "32": "250 à 499 salariés",
    "41": "500 à 999 salariés",
    "42": "1 000 à 1 999 salariés",
    "51": "2 000 à 4 999 salariés",
    "52": "5 000 à 9 999 salariés",
    "53": "10 000 salariés et plus",
}


def etat_label(code: Any) -> str | None:
    c = _s(code)
    if not c:
        return None
    return ETAT_LABELS.get(c.upper(), c)


def nature_label(code: Any) -> dict | None:
    c = _s(code)
    if not c:
        return None
    c = re.sub(r"\D", "", c) or c
    return {"code": c, "libelle": NATURE_JURIDIQUE.get(c)}


def tranche_label(code: Any) -> dict | None:
    c = _s(code)
    if not c:
        return None
    return {"code": c, "libelle": TRANCHE_EFFECTIFS.get(c)}


def naf_of(code: Any, libelle: Any = None) -> dict | None:
    c = _s(code)
    if not c and not _s(libelle):
        return None
    return {"code": c, "libelle": _s(libelle)}


def _period(obj: dict | None, *keys: str) -> Any:
    """Read first matching key from the latest INSEE période (list[0] is current)."""
    if not obj:
        return None
    periods = obj.get("periodesUniteLegale") or obj.get("periodesEtablissement") or []
    cur = periods[0] if periods and isinstance(periods[0], dict) else {}
    for k in keys:
        if obj.get(k) not in (None, ""):
            return obj.get(k)
        if cur.get(k) not in (None, ""):
            return cur.get(k)
    return None


def address_from_recherche_siege(siege: dict | None) -> dict | None:
    if not siege:
        return None
    addr = {
        "line": _s(siege.get("adresse")),
        "numeroVoie": _s(siege.get("numero_voie")),
        "typeVoie": _s(siege.get("type_voie")),
        "libelleVoie": _s(siege.get("libelle_voie")),
        "complement": _s(siege.get("complement_adresse")),
        "codePostal": _s(siege.get("code_postal")),
        "commune": _s(siege.get("libelle_commune")) or _s(siege.get("commune")),
        "cedex": _s(siege.get("cedex")) or _s(siege.get("libelle_cedex")),
        "departement": _s(siege.get("departement")),
        "region": _s(siege.get("region")),
        "pays": _s(siege.get("libelle_pays_etranger")),
        "siret": _s(siege.get("siret")),
        "estSiege": bool(siege.get("est_siege")) if siege.get("est_siege") is not None else True,
    }
    return {k: v for k, v in addr.items() if v not in (None, False)} or None


def address_from_insee(etab: dict | None) -> dict | None:
    if not etab:
        return None
    a = etab.get("adresseEtablissement") or {}
    street = " ".join(
        x for x in (_s(a.get("numeroVoieEtablissement")), _s(a.get("typeVoieEtablissement")),
                    _s(a.get("libelleVoieEtablissement"))) if x
    )
    line_parts = [street, _s(a.get("complementAdresseEtablissement"))]
    city = " ".join(x for x in (_s(a.get("codePostalEtablissement")), _s(a.get("libelleCommuneEtablissement"))) if x)
    line = ", ".join(x for x in (*line_parts, city) if x)
    addr = {
        "line": line or None,
        "numeroVoie": _s(a.get("numeroVoieEtablissement")),
        "typeVoie": _s(a.get("typeVoieEtablissement")),
        "libelleVoie": _s(a.get("libelleVoieEtablissement")),
        "complement": _s(a.get("complementAdresseEtablissement")),
        "codePostal": _s(a.get("codePostalEtablissement")),
        "commune": _s(a.get("libelleCommuneEtablissement")),
        "codeCommune": _s(a.get("codeCommuneEtablissement")),
        "cedex": _s(a.get("libelleCedexEtablissement")),
        "pays": _s(a.get("libellePaysEtrangerEtablissement")),
        "siret": _s(etab.get("siret")),
        "estSiege": bool(etab.get("etablissementSiege")),
    }
    return {k: v for k, v in addr.items() if v not in (None, False)} or None


def address_line(a: dict | None) -> str | None:
    if not a:
        return None
    if a.get("line"):
        return a["line"]
    street = " ".join(x for x in (a.get("numeroVoie"), a.get("typeVoie"), a.get("libelleVoie")) if x)
    city = " ".join(x for x in (a.get("codePostal"), a.get("commune")) if x)
    parts = [street, a.get("complement"), city, a.get("pays")]
    s = ", ".join(x for x in parts if x)
    return s or None


def _base_record(digits: str, kind: str) -> dict[str, Any]:
    siren = siren_of(digits)
    rec: dict[str, Any] = {
        "siren": siren,
        "sirenFormatted": format_siren(siren),
        "inputId": digits,
        "inputKind": kind,
        "inputFormatted": format_id(digits, kind),
    }
    if kind == "siret":
        rec["siret"] = digits
        rec["siretFormatted"] = format_siret(digits)
    else:
        rec["siret"] = None
        rec["siretFormatted"] = None
    return rec


def etablissements_summary_recherche(d: dict, include: bool) -> dict | None:
    if not include:
        return None
    siege = d.get("siege") or {}
    matching = d.get("matching_etablissements") or []
    sample = []
    # Head office first
    if siege.get("siret"):
        sample.append({
            "siret": _s(siege.get("siret")),
            "estSiege": True,
            "etatAdministratif": _s(siege.get("etat_administratif")),
            "etatLabel": etat_label(siege.get("etat_administratif")),
            "adresse": _s(siege.get("adresse")),
            "naf": naf_of(siege.get("activite_principale")),
        })
    for m in matching:
        if not isinstance(m, dict):
            continue
        siret = _s(m.get("siret"))
        if siret and siret == (sample[0].get("siret") if sample else None):
            continue
        sample.append({
            "siret": siret,
            "estSiege": bool(m.get("est_siege")),
            "etatAdministratif": _s(m.get("etat_administratif")),
            "etatLabel": etat_label(m.get("etat_administratif")),
            "adresse": _s(m.get("adresse")),
            "naf": naf_of(m.get("activite_principale")),
        })
        if len(sample) >= 5:
            break
    return {
        "count": d.get("nombre_etablissements"),
        "countOpen": d.get("nombre_etablissements_ouverts"),
        "headOfficeSiret": _s(siege.get("siret")),
        "sample": sample,
        "note": "Summary only — not a full dump of all establishments.",
    }


def etablissements_summary_insee(etabs: list[dict], include: bool, max_sample: int = 5) -> dict | None:
    if not include:
        return None
    open_n = 0
    sample = []
    head = None
    for e in etabs:
        if not isinstance(e, dict):
            continue
        etat = _s(_period(e, "etatAdministratifEtablissement"))
        if etat == "A":
            open_n += 1
        addr = address_from_insee(e)
        row = {
            "siret": _s(e.get("siret")),
            "estSiege": bool(e.get("etablissementSiege")),
            "etatAdministratif": etat,
            "etatLabel": etat_label(etat),
            "adresse": address_line(addr),
            "naf": naf_of(_period(e, "activitePrincipaleEtablissement")),
        }
        if e.get("etablissementSiege"):
            head = row
        elif len(sample) < max_sample:
            sample.append(row)
    out_sample = ([head] if head else []) + sample
    return {
        "count": len(etabs),
        "countOpen": open_n,
        "headOfficeSiret": (head or {}).get("siret"),
        "sample": out_sample[:max_sample],
        "note": "Summary only — capped sample from INSEE (not a full dump).",
    }


def normalize_recherche(d: dict, digits: str, kind: str, include_etab: bool) -> dict[str, Any]:
    d = strip_contacts(d)
    siege = d.get("siege") or {}
    rec = _base_record(digits, kind)
    # Prefer matching SIRET from input / siege
    siret = digits if kind == "siret" else _s(siege.get("siret"))
    if siret:
        rec["siret"] = siret
        rec["siretFormatted"] = format_siret(siret)
    etat = _s(d.get("etat_administratif"))
    rec.update({
        "denomination": _s(d.get("nom_raison_sociale")) or _s(d.get("nom_complet")),
        "nomComplet": _s(d.get("nom_complet")),
        "sigle": _s(d.get("sigle")),
        "etatAdministratif": etat,
        "etatLabel": etat_label(etat),
        "dateCreation": _date(d.get("date_creation")),
        "dateFermeture": _date(d.get("date_fermeture")),
        "natureJuridique": nature_label(d.get("nature_juridique")),
        "naf": naf_of(d.get("activite_principale")),
        "nafSection": _s(d.get("section_activite_principale")),
        "trancheEffectifs": tranche_label(d.get("tranche_effectif_salarie")),
        "categorieEntreprise": _s(d.get("categorie_entreprise")),
        "address": address_from_recherche_siege(siege),
        "etablissementsSummary": etablissements_summary_recherche(d, include_etab),
        "tvaIntracommunautaire": (d.get("tva") or [None])[0] if isinstance(d.get("tva"), list) else _s(d.get("tva")),
        "sourceUpdatedAt": _s(d.get("date_mise_a_jour_insee")) or _s(d.get("date_mise_a_jour")),
    })
    return rec


def normalize_insee(
    ul: dict, digits: str, kind: str, include_etab: bool, etabs: list[dict] | None = None,
    siege_etab: dict | None = None,
) -> dict[str, Any]:
    ul = strip_contacts(ul)
    etabs = strip_contacts(etabs or [])
    siege_etab = strip_contacts(siege_etab) if siege_etab else None
    rec = _base_record(digits, kind)
    etat = _s(_period(ul, "etatAdministratifUniteLegale"))
    nic = _s(_period(ul, "nicSiegeUniteLegale"))
    siret = digits if kind == "siret" else (f"{ul.get('siren')}{nic}" if nic else None)
    if siret and len(siret) == 14:
        rec["siret"] = siret
        rec["siretFormatted"] = format_siret(siret)
    denom = _s(_period(ul, "denominationUniteLegale"))
    # Individus: nom + prenom
    if not denom:
        nom = _s(_period(ul, "nomUniteLegale")) or _s(ul.get("nomUniteLegale"))
        prenom = _s(ul.get("prenomUsuelUniteLegale")) or _s(ul.get("prenom1UniteLegale"))
        denom = " ".join(x for x in (prenom, nom) if x) or None
    addr_src = siege_etab
    if not addr_src and etabs:
        addr_src = next((e for e in etabs if e.get("etablissementSiege")), etabs[0] if etabs else None)
    rec.update({
        "denomination": denom,
        "nomComplet": denom,
        "sigle": _s(ul.get("sigleUniteLegale")) or _s(_period(ul, "sigleUniteLegale")),
        "etatAdministratif": etat,
        "etatLabel": etat_label(etat),
        "dateCreation": _date(ul.get("dateCreationUniteLegale")),
        "dateFermeture": None,
        "natureJuridique": nature_label(_period(ul, "categorieJuridiqueUniteLegale")),
        "naf": naf_of(_period(ul, "activitePrincipaleUniteLegale")),
        "nafSection": None,
        "trancheEffectifs": tranche_label(_period(ul, "trancheEffectifsUniteLegale")),
        "categorieEntreprise": _s(ul.get("categorieEntreprise")),
        "address": address_from_insee(addr_src) if addr_src else None,
        "etablissementsSummary": etablissements_summary_insee(etabs, include_etab) if etabs else (
            etablissements_summary_insee([addr_src], include_etab) if addr_src and include_etab else None
        ),
        "tvaIntracommunautaire": None,
        "sourceUpdatedAt": None,
    })
    return rec


def finalize(rec: dict[str, Any]) -> dict[str, Any]:
    if rec.get("address"):
        rec["addressLine"] = address_line(rec["address"])
    return scrub_values(rec)


def pick_recherche_result(payload: dict, digits: str, kind: str) -> dict | None:
    results = payload.get("results") or []
    if not results:
        return None
    siren = siren_of(digits)
    # Exact SIREN match preferred
    for r in results:
        if not isinstance(r, dict):
            continue
        if _s(r.get("siren")) == siren:
            if kind == "siret":
                # Prefer if matching_etablissements or siege siret matches
                siege_siret = _s((r.get("siege") or {}).get("siret"))
                matching = [m for m in (r.get("matching_etablissements") or []) if isinstance(m, dict)]
                if siege_siret == digits or any(_s(m.get("siret")) == digits for m in matching):
                    return r
                return r  # still the right company
            return r
    # Fallback: first result whose SIREN prefixes the query
    for r in results:
        if isinstance(r, dict) and _s(r.get("siren")) and digits.startswith(r["siren"]):
            return r
    return None


# --- HTTP ---------------------------------------------------------------------------------------
def classify(status: int, body: Any, text: str = "") -> str:
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status == 400:
        return "bad_request"
    if status >= 500:
        return "server_error"
    return f"http_{status}"


@dataclass
class Attempt:
    provider: str
    ok: bool
    record: dict | None = None
    error_class: str | None = None
    http_status: int | None = None
    message: str | None = None
    attempts: int = 0
    keyed: bool = False


async def _get_json(
    client: httpx.AsyncClient, url: str, headers: dict, timeout: float, api_key: str | None,
) -> tuple[int, Any, str, float | None]:
    resp = await client.get(url, headers=headers, timeout=timeout)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = None
    text = redact(resp.text[:300], api_key)
    ra = resp.headers.get("retry-after")
    retry_after = None
    if ra:
        try:
            retry_after = float(ra)
        except ValueError:
            retry_after = None
    return resp.status_code, body, text, retry_after


async def fetch_recherche(
    client: httpx.AsyncClient, digits: str, kind: str, *, include_etab: bool,
    max_retries: int = 3, timeout: float = 30.0, backoff_base: float = 1.0, max_wait: float = 30.0,
) -> Attempt:
    url = f"{BASES['recherche']}/search?q={quote(digits)}&per_page=5"
    headers = {"Accept": "application/json", "User-Agent": UA}
    last = Attempt("recherche", False, keyed=False)
    for attempt in range(max_retries + 1):
        last.attempts = attempt + 1
        retry_after: float | None = None
        try:
            status, body, text, retry_after = await _get_json(client, url, headers, timeout, None)
            if status == 200 and isinstance(body, dict):
                hit = pick_recherche_result(body, digits, kind)
                if not hit:
                    return Attempt("recherche", False, error_class="not_found", http_status=200,
                                   message="No matching SIREN/SIRET in Recherche Entreprises.",
                                   attempts=last.attempts, keyed=False)
                rec = normalize_recherche(hit, digits, kind, include_etab)
                if not rec.get("denomination") and not rec.get("nomComplet"):
                    return Attempt("recherche", False, error_class="bad_response", http_status=200,
                                   message="Recherche result missing denomination.",
                                   attempts=last.attempts, keyed=False)
                rec["source"] = "recherche"
                rec["sourceLabel"] = SOURCE_LABELS["recherche"]
                rec["sourceUrl"] = f"https://recherche-entreprises.api.gouv.fr/search?q={digits}"
                return Attempt("recherche", True, record=rec, http_status=200,
                               attempts=last.attempts, keyed=False)
            if status == 200:
                last.error_class, last.http_status, last.message = "bad_response", 200, "Non-JSON 200."
            else:
                cls = classify(status, body, text)
                msg = (body.get("message") or body.get("erreur")) if isinstance(body, dict) else None
                last.error_class, last.http_status = cls, status
                last.message = redact(str(msg or text or f"HTTP {status}"), None)[:200]
                if status in (400, 401, 403, 404) or (400 <= status < 500 and status != 429):
                    return Attempt("recherche", False, error_class=cls, http_status=status,
                                   message=last.message, attempts=last.attempts, keyed=False)
        except httpx.TimeoutException:
            last.error_class, last.http_status, last.message = "timeout", None, f"Timed out after {timeout:.0f}s"
        except httpx.HTTPError as e:
            last.error_class, last.http_status = "network", None
            last.message = f"{type(e).__name__}: {e}"[:200]
        if attempt < max_retries:
            wait = retry_after if retry_after is not None else backoff_base * (2 ** attempt) + random.uniform(0, 0.5)
            await asyncio.sleep(min(max(wait, 0.0), max_wait))
    return last


async def fetch_insee(
    client: httpx.AsyncClient, digits: str, kind: str, *, api_key: str, include_etab: bool,
    max_retries: int = 3, timeout: float = 30.0, backoff_base: float = 1.0, max_wait: float = 30.0,
) -> Attempt:
    siren = siren_of(digits)
    headers = {
        "Accept": "application/json",
        "User-Agent": UA,
        "X-INSEE-Api-Key-Integration": api_key,
    }
    last = Attempt("insee", False, keyed=True)

    async def once() -> Attempt:
        # 1) unité légale by SIREN
        url = f"{BASES['insee']}/siren/{siren}"
        status, body, text, retry_after = await _get_json(client, url, headers, timeout, api_key)
        if status != 200 or not isinstance(body, dict):
            cls = classify(status, body, text) if status != 200 else "bad_response"
            msg = (body.get("message") or body.get("fault", {}).get("message")
                   if isinstance(body, dict) else None)
            if isinstance(msg, dict):
                msg = msg.get("message") or str(msg)
            a = Attempt("insee", False, error_class=cls, http_status=status,
                        message=redact(str(msg or text or f"HTTP {status}"), api_key)[:200], keyed=True)
            a._retry_after = retry_after  # type: ignore[attr-defined]
            return a
        ul = body.get("uniteLegale") or body
        if not isinstance(ul, dict) or not ul.get("siren"):
            return Attempt("insee", False, error_class="bad_response", http_status=200,
                           message="INSEE response missing uniteLegale.", keyed=True)

        etabs: list[dict] = []
        siege_etab: dict | None = None
        if kind == "siret":
            s_url = f"{BASES['insee']}/siret/{digits}"
            st2, body2, text2, _ = await _get_json(client, s_url, headers, timeout, api_key)
            if st2 == 200 and isinstance(body2, dict):
                siege_etab = body2.get("etablissement") or body2
            elif st2 == 404:
                return Attempt("insee", False, error_class="not_found", http_status=404,
                               message=f"SIRET {digits} not found at INSEE.", keyed=True)

        if include_etab:
            # Cap to a small page — summary only
            q = quote(f"siren:{siren}")
            e_url = f"{BASES['insee']}/siret?q={q}&nombre=20"
            st3, body3, text3, _ = await _get_json(client, e_url, headers, timeout, api_key)
            if st3 == 200 and isinstance(body3, dict):
                etabs = [e for e in (body3.get("etablissements") or []) if isinstance(e, dict)]
            # If we already have a single siret and list failed, keep what we have
            if not etabs and siege_etab:
                etabs = [siege_etab]

        rec = normalize_insee(ul, digits, kind, include_etab, etabs=etabs, siege_etab=siege_etab)
        if not rec.get("denomination") and not rec.get("nomComplet"):
            return Attempt("insee", False, error_class="bad_response", http_status=200,
                           message="INSEE unité légale missing denomination.", keyed=True)
        rec["source"] = "insee"
        rec["sourceLabel"] = SOURCE_LABELS["insee"]
        rec["sourceUrl"] = f"https://api.insee.fr/api-sirene/3.11/siren/{siren}"
        return Attempt("insee", True, record=rec, http_status=200, keyed=True)

    for attempt in range(max_retries + 1):
        last.attempts = attempt + 1
        try:
            a = await once()
            a.attempts = last.attempts
            if a.ok:
                return a
            if a.http_status in (400, 401, 403, 404) or (
                a.http_status and 400 <= a.http_status < 500 and a.http_status != 429
            ):
                return a
            last = a
            retry_after = getattr(a, "_retry_after", None)
        except httpx.TimeoutException:
            last.error_class, last.http_status, last.message = "timeout", None, f"Timed out after {timeout:.0f}s"
            retry_after = None
        except httpx.HTTPError as e:
            last.error_class, last.http_status = "network", None
            last.message = redact(f"{type(e).__name__}: {e}", api_key)[:200]
            retry_after = None
        if attempt < max_retries:
            wait = retry_after if retry_after is not None else backoff_base * (2 ** attempt) + random.uniform(0, 0.5)
            await asyncio.sleep(min(max(wait, 0.0), max_wait))
    return last


def provider_chain(provider: str, api_key: str | None) -> list[tuple[str, str | None]]:
    """Ordered (provider, key) pairs to try."""
    if provider == "recherche":
        return [("recherche", None)]
    if provider == "insee":
        return [("insee", api_key)]
    # auto
    if api_key:
        # Prefer INSEE for richer fields when key present; fall back to recherche
        return [("insee", api_key), ("recherche", None)]
    return [("recherche", None)]


@dataclass
class LookupResult:
    ok: bool
    item: dict[str, Any]
    auth_failed: bool = False
    tried: list[dict] = field(default_factory=list)


HINTS = {
    "not_found": "SIREN/SIRET not found in the government database. Check the number; very new registrations can lag.",
    "invalid_input": "Not a valid SIREN (9 digits) or SIRET (14 digits) with correct Luhn check digit.",
    "rate_limited": "Provider rate limit hit after retries. Lower maxConcurrency, or use your own INSEE API key.",
    "unauthorized": "Your INSEE API key was rejected (HTTP 401). Apply at https://portail-api.insee.fr and paste the integration key as apiKey.",
    "forbidden": "INSEE refused the request (HTTP 403). Check your portal subscription to API Sirene.",
    "server_error": "Provider server error after retries. Try again later.",
    "timeout": "Provider timed out after retries.",
    "network": "Network error reaching the provider.",
    "bad_response": "Provider returned an unexpected payload.",
    "skipped_auth": "Skipped INSEE after an earlier authentication failure (stopOnAuthError).",
    "missing_key": "provider=insee requires apiKey (INSEE X-INSEE-Api-Key-Integration). Apply at https://portail-api.insee.fr.",
}


async def lookup_sirene(
    client: httpx.AsyncClient, digits: str, kind: str, *, provider: str, api_key: str | None,
    include_etab: bool, max_retries: int, timeout: float, skip_keyed: bool = False,
    backoff_base: float = 1.0,
) -> LookupResult:
    chain = provider_chain(provider, api_key)
    tried: list[dict] = []
    errors: list[Attempt] = []
    auth_failed = False
    total_attempts = 0
    for prov, key in chain:
        if prov == "insee" and not key:
            a = Attempt("insee", False, error_class="missing_key", message=HINTS["missing_key"], keyed=True)
        elif key and skip_keyed:
            a = Attempt(prov, False, error_class="skipped_auth", message=HINTS["skipped_auth"], keyed=True)
        elif prov == "recherche":
            a = await fetch_recherche(client, digits, kind, include_etab=include_etab,
                                      max_retries=max_retries, timeout=timeout, backoff_base=backoff_base)
        else:
            a = await fetch_insee(client, digits, kind, api_key=key or "", include_etab=include_etab,
                                  max_retries=max_retries, timeout=timeout, backoff_base=backoff_base)
        total_attempts += a.attempts
        tried.append({"provider": prov, "keyed": a.keyed, "ok": a.ok, "errorClass": a.error_class,
                      "httpStatus": a.http_status})
        if a.ok and a.record:
            rec = finalize(a.record)
            rec.update({"status": "ok", "errorClass": None, "attempts": total_attempts, "triedSources": tried})
            return LookupResult(True, rec, auth_failed, tried)
        if a.keyed and a.error_class in ("unauthorized", "forbidden", "missing_key"):
            auth_failed = True
        errors.append(a)
    pick = next((e for e in errors if e.error_class == "invalid_input"), None) \
        or next((e for e in errors if e.error_class == "not_found"), None) \
        or next((e for e in errors if e.error_class in ("unauthorized", "forbidden", "missing_key")), None) \
        or (errors[0] if errors else None)
    item = _base_record(digits, kind)
    item.update({
        "status": "failed",
        "source": pick.provider if pick else "none",
        "sourceLabel": SOURCE_LABELS.get(pick.provider) if pick else None,
        "errorClass": (pick.error_class if pick else "error") or "error",
        "errorMessage": pick.message if pick else None,
        "httpStatus": pick.http_status if pick else None,
        "hint": HINTS.get(pick.error_class if pick else "", None),
        "attempts": total_attempts,
        "triedSources": tried,
    })
    return LookupResult(False, scrub_values(item), auth_failed, tried)
