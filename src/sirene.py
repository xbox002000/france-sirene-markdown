"""SIREN (9) / SIRET (14) normalization + Luhn check-digit validation (French mod-10)."""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

SIREN_FIELDS = (
    "siren", "SIREN", "siret", "SIRET", "sirenOrSiret", "taxId", "tax_id",
    "numero", "companyId", "id",
)
_DIGITS = re.compile(r"\D+")


def luhn_ok(digits: str) -> bool:
    """French SIREN/SIRET Luhn (mod-10). Double every second digit from the right."""
    if not digits or not digits.isdigit():
        return False
    total = 0
    reverse = digits[::-1]
    for i, ch in enumerate(reverse):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def clean_id(raw: Any) -> str:
    """Strip punctuation/spaces. Pure-digit values shorter than 9 are zero-padded carefully
    only when clearly a truncated SIREN (never invent a SIRET from a short string)."""
    s = _DIGITS.sub("", str(raw or ""))
    # Spreadsheets occasionally drop a leading zero on a 9-digit SIREN → 8 digits.
    if s.isdigit() and len(s) == 8:
        s = s.zfill(9)
    return s


def classify_id(digits: str) -> str | None:
    """Return 'siren', 'siret', or None."""
    if not digits or not digits.isdigit():
        return None
    if len(set(digits)) == 1:  # 000000000 / 11111111111111
        return None
    if len(digits) == 9 and luhn_ok(digits):
        return "siren"
    if len(digits) == 14 and luhn_ok(digits) and luhn_ok(digits[:9]):
        return "siret"
    return None


def normalize_id(raw: Any) -> tuple[str, str] | None:
    """Return (digits, kind) where kind is 'siren'|'siret', or None if invalid."""
    d = clean_id(raw)
    kind = classify_id(d)
    return (d, kind) if kind else None


def format_siren(s: str) -> str:
    if not s or len(s) != 9:
        return s or ""
    return f"{s[:3]} {s[3:6]} {s[6:]}"


def format_siret(s: str) -> str:
    if not s or len(s) != 14:
        return s or ""
    return f"{s[:3]} {s[3:6]} {s[6:9]} {s[9:]}"


def format_id(digits: str, kind: str | None = None) -> str:
    kind = kind or classify_id(digits)
    if kind == "siret":
        return format_siret(digits)
    if kind == "siren":
        return format_siren(digits)
    return digits or ""


def siren_of(digits: str) -> str:
    return digits[:9] if digits else ""


def id_from_item(item: dict, field: str | None = None) -> Any:
    fields = ((field,) if field else ()) + SIREN_FIELDS
    for f in fields:
        if f and item.get(f) not in (None, ""):
            return item.get(f)
    return None


def normalize_ids(values: Iterable[Any] | str | None, field: str | None = None) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (unique [(digits, kind), ...], invalid raw strings). Order preserved."""
    if values is None:
        return [], []
    if isinstance(values, str):
        values = [v for v in re.split(r"[\r\n,;]+", values) if v.strip()]
    out: list[tuple[str, str]] = []
    bad: list[str] = []
    seen: set[str] = set()
    for v in values:
        raw = id_from_item(v, field) if isinstance(v, dict) else v
        if raw is None or str(raw).strip() == "":
            if isinstance(v, dict):
                bad.append(json.dumps(v, ensure_ascii=False)[:80])
            continue
        parsed = normalize_id(raw)
        if parsed:
            digits, kind = parsed
            # Deduplicate by SIREN when both SIREN and a SIRET of same company appear: keep first.
            key = digits if kind == "siret" else digits
            # Also skip a later SIREN if we already have a SIRET for that SIREN? Keep both distinct inputs
            # as separate lookups only when digits differ; same digits collapsed.
            if key not in seen:
                seen.add(key)
                out.append((digits, kind))
        else:
            bad.append(str(raw).strip()[:80])
    return out, bad


def split_payload(payload: Any, field: str | None = None) -> tuple[list[tuple[str, str]], list[str]]:
    """KV record → (ids, invalid). Accepts list, newline/comma text, or {"sirens": [...]}."""
    if payload is None:
        return [], []
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        s = payload.strip()
        if s.startswith(("[", "{")):
            try:
                return split_payload(json.loads(s), field)
            except ValueError:
                pass
        return normalize_ids(s, field)
    if isinstance(payload, dict):
        arr = (
            payload.get("sirens") or payload.get("SIRENs") or payload.get("sirets")
            or payload.get("ids") or payload.get("items")
        )
        if arr:
            return normalize_ids(arr, field)
        return normalize_ids([payload], field)
    if isinstance(payload, list):
        return normalize_ids(payload, field)
    return [], []
