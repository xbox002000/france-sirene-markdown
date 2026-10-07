"""Markdown due-diligence card, REPORT.md and RAG chunks. Inputs are already contact-free."""
from __future__ import annotations

import json
from typing import Any

PRIVACY_NOTE = "Email, telephone and other contact fields are intentionally not collected or output."

SOURCE_DESC = {
    "recherche": "Recherche Entreprises (api.gouv.fr) — official French government company search API",
    "insee": "INSEE API Sirene (api.insee.fr) — official French business register",
}


def esc(v: Any, limit: int = 300) -> str:
    if v is None or v == "":
        return "—"
    s = str(v).replace("|", "\\|").replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def card_sections(r: dict) -> list[tuple[str, list[str]]]:
    nat = r.get("natureJuridique") or {}
    nat_s = " — ".join(x for x in (nat.get("code"), nat.get("libelle")) if x) or None
    te = r.get("trancheEffectifs") or {}
    te_s = " — ".join(x for x in (te.get("code"), te.get("libelle")) if x) or None
    identity = [
        "| Field | Value |", "|---|---|",
        f"| Dénomination | {esc(r.get('denomination') or r.get('nomComplet'))} |",
        f"| Nom complet | {esc(r.get('nomComplet'))} |",
        f"| Sigle | {esc(r.get('sigle'))} |",
        f"| SIREN | `{esc(r.get('sirenFormatted') or r.get('siren'))}` |",
        f"| SIRET (siège / input) | `{esc(r.get('siretFormatted') or r.get('siret'))}` |",
        f"| Nature juridique | {esc(nat_s)} |",
        f"| Catégorie entreprise | {esc(r.get('categorieEntreprise'))} |",
        f"| Date de création | {esc(r.get('dateCreation'))} |",
        f"| Tranche d'effectifs | {esc(te_s)} |",
    ]
    if r.get("tvaIntracommunautaire"):
        identity.append(f"| TVA intracommunautaire | `{esc(r.get('tvaIntracommunautaire'))}` |")

    etat = r.get("etatLabel") or r.get("etatAdministratif")
    status = [
        "| Field | Value |", "|---|---|",
        f"| État administratif | **{esc(etat)}** |",
    ]
    if r.get("dateFermeture"):
        status.append(f"| Date de fermeture | {esc(r.get('dateFermeture'))} |")
    if r.get("etatAdministratif") and r.get("etatAdministratif") != "A":
        status += ["", f"> ⚠️ Administrative status is **{esc(etat)}** — not active."]

    naf = r.get("naf") or {}
    naf_lines = [
        f"- **Code NAF/APE:** `{esc(naf.get('code'))}`"
        + (f" — {esc(naf.get('libelle'))}" if naf.get("libelle") else ""),
    ]
    if r.get("nafSection"):
        naf_lines.append(f"- **Section:** `{esc(r.get('nafSection'))}`")

    a = r.get("address") or {}
    address = [f"{esc(r.get('addressLine') or a.get('line'))}", ""]
    if a.get("departement") or a.get("region"):
        address.append(f"- Département / région: {esc(a.get('departement'))} / {esc(a.get('region'))}")
    if a.get("codeCommune"):
        address.append(f"- Code commune INSEE: `{a['codeCommune']}`")
    address.append(f"- _{PRIVACY_NOTE}_")

    sections = [
        ("Identity", identity),
        ("Status", status),
        ("NAF/APE", naf_lines),
        ("Address", address),
    ]

    es = r.get("etablissementsSummary")
    if es is not None:
        lines = [
            f"- Establishments (total): **{esc(es.get('count'))}** · open: **{esc(es.get('countOpen'))}**",
            f"- Head office SIRET: `{esc(es.get('headOfficeSiret'))}`",
        ]
        sample = es.get("sample") or []
        if sample:
            lines += ["", "| SIRET | Siège | État | Adresse |", "|---|---|---|---|"]
            for s in sample:
                lines.append(
                    f"| `{esc(s.get('siret'), 20)}` | {'yes' if s.get('estSiege') else 'no'} | "
                    f"{esc(s.get('etatLabel') or s.get('etatAdministratif'))} | {esc(s.get('adresse'), 80)} |"
                )
        if es.get("note"):
            lines += ["", f"_{esc(es.get('note'))}_"]
        sections.append(("Établissements", lines))

    src = [
        f"- Source: {SOURCE_DESC.get(r.get('source') or '', esc(r.get('sourceLabel')))}",
        f"- Endpoint: <{r.get('sourceUrl')}>" if r.get("sourceUrl") else "- Endpoint: —",
    ]
    if r.get("sourceUpdatedAt"):
        src.append(f"- Source record updated: {r['sourceUpdatedAt']}")
    if r.get("checkedAt"):
        src.append(f"- Fetched: {r['checkedAt']}")
    src.append("- Underlying data: répertoire SIRENE (INSEE) — Licence Ouverte / Open Licence 2.0.")
    src.append(f"- {PRIVACY_NOTE}")
    sections.append(("Source", src))
    return sections


def company_markdown(r: dict) -> str:
    title = r.get("denomination") or r.get("nomComplet") or r.get("sirenFormatted")
    L = [
        f"## {esc(title, 160)}", "",
        f"**SIREN** `{esc(r.get('sirenFormatted') or r.get('siren'))}`"
        + (f" · **SIRET** `{esc(r.get('siretFormatted') or r.get('siret'))}`" if r.get("siret") else "")
        + f" · **{esc(r.get('etatLabel') or r.get('etatAdministratif'))}** · source: {esc(r.get('sourceLabel'))}",
        "",
    ]
    for name, lines in card_sections(r):
        L += [f"### {name}", "", *lines, ""]
    return "\n".join(L).rstrip() + "\n"


def failed_markdown(r: dict) -> str:
    return (
        f"## SIREN/SIRET `{esc(r.get('inputFormatted') or r.get('siren') or r.get('inputId'))}` — failed (not charged)\n\n"
        f"- Error: `{esc(r.get('errorClass'))}`"
        + (f" (HTTP {r['httpStatus']})" if r.get("httpStatus") else "") + "\n"
        f"- Message: {esc(r.get('errorMessage'))}\n- Hint: {esc(r.get('hint'))}\n"
    )


def build_markdown(rows: list[dict], summary: dict) -> str:
    ok = [r for r in rows if r.get("status") == "ok"]
    bad = [r for r in rows if r.get("status") != "ok"]
    L = [
        "# France SIRENE — company due-diligence cards", "",
        f"- Generated: {summary.get('finishedAt')}",
        f"- Companies OK: **{len(ok)}** · failed (not charged): **{len(bad)}**",
        f"- Provider mode: `{summary.get('provider')}` · sources used: `{summary.get('bySource') or {}}`",
        f"- {PRIVACY_NOTE}", "",
    ]
    for r in ok:
        L += [r.get("markdown") or company_markdown(r), "---", ""]
    if bad:
        L += ["## Failed (not charged)", "", "| Id | Error | HTTP | Hint |", "|---|---|---|---|"]
        for r in bad:
            L.append(
                f"| `{esc(r.get('inputFormatted') or r.get('siren') or r.get('inputId'), 40)}` | "
                f"{esc(r.get('errorClass'))} | {esc(r.get('httpStatus'))} | "
                f"{esc(r.get('hint') or r.get('errorMessage'), 160)} |"
            )
        L.append("")
    return "\n".join(L).rstrip() + "\n"


def rag_chunks(r: dict, max_chars: int = 1200) -> list[dict]:
    if r.get("status") != "ok":
        return []
    name = r.get("denomination") or r.get("nomComplet") or r.get("sirenFormatted")
    head = f"{name} (SIREN {r.get('sirenFormatted') or r.get('siren')})"
    out: list[dict] = []
    for title, lines in card_sections(r):
        buf: list[str] = []
        parts: list[list[str]] = []
        size = 0
        for ln in lines:
            if buf and size + len(ln) + 1 > max_chars:
                parts.append(buf)
                buf, size = [], 0
            buf.append(ln)
            size += len(ln) + 1
        if buf:
            parts.append(buf)
        for i, p in enumerate(parts, 1):
            body = "\n".join(p).strip()
            if i > 1 and lines and lines[0].startswith("|") and not body.startswith(lines[0]):
                body = "\n".join(lines[:2]) + "\n" + body
            sec = title if len(parts) == 1 else f"{title} (part {i}/{len(parts)})"
            out.append({
                "id": f"{r.get('siren')}-{len(out) + 1}",
                "siren": r.get("siren"),
                "siret": r.get("siret"),
                "denomination": r.get("denomination"),
                "section": sec,
                "source": r.get("source"),
                "text": f"# {head} — {sec}\n\n{body}",
            })
    for i, c in enumerate(out, 1):
        c["chunkIndex"], c["chunkCount"] = i, len(out)
    return out


def build_chunks_jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(c, ensure_ascii=False) + "\n" for r in rows for c in rag_chunks(r))
