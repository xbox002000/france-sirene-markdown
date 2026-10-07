# France SIRENE → Company Markdown DD Card

Batch French **SIREN** (9 digits) / **SIRET** (14 digits) lookups into Markdown due-diligence cards + JSON dataset + optional RAG chunks.

**Official / government APIs only.** No HTML scraping of commercial directories. **No email, phone, or lead fields** — ever.

## Providers

| Mode | Endpoint | Key |
|---|---|---|
| **Default / demo** | `https://recherche-entreprises.api.gouv.fr/search?q={siren_or_siret}` | None (keyless) |
| **Production BYOK** | `https://api.insee.fr/api-sirene/3.11/siren/{siren}` (+ `/siret/{siret}`, `/siret?q=siren:…`) | Your INSEE integration key as header `X-INSEE-Api-Key-Integration` |

`provider=auto` (default): with `apiKey` tries **INSEE** first, then falls back to Recherche Entreprises; without a key uses Recherche only.

### Get an INSEE API key (buyer self-serve)

1. Open **https://portail-api.insee.fr**
2. Sign in (CONNEXION-POUR-LES-EXTERNES) and create an application
3. Subscribe the app to **API Sirene**
4. Copy the **Clé d'API / integration key** into this Actor’s `apiKey` input (secret)

The Actor publisher does **not** sign INSEE contracts, does **not** top-up quotas, and does **not** store a shared key on the Actor. INSEE bills / rate-limits your own account (typically ~30 req/min).

## Input

- `sirens` — string list of SIREN and/or SIRET (punctuation OK; Luhn-validated)
- `datasetId` / `keyValueStoreId` + `keyValueRecordKey` (default `INPUT_SIRENS`) / `sirenField`
- `provider` — `auto` | `recherche` | `insee`
- `apiKey` — optional secret (INSEE integration key). Use `YOUR_KEY` as a placeholder in docs only.
- `stopOnAuthError` (default true)
- `includeEtablissements` (default true) — summary only, not a full dump
- `includeRagChunks` (default true)
- `maxItems` 100 · `maxConcurrency` 2 (max 5) · `maxRetries` 3 · `requestTimeoutSecs` 30

## Output

One dataset row per id: `siren`, `siret`, `denomination` / `nomComplet`, `sigle`, `etatAdministratif` / `etatLabel`, `dateCreation`, `naf` (code + libelle when available), `natureJuridique`, `trancheEffectifs`, `address` (**no** phone/email), `etablissementsSummary`, `markdown`, `source`, `sourceUrl`, `status`, error fields, `checkedAt`.

Key-value store: `REPORT.md`, `CHUNKS.jsonl`, `SUMMARY`, `OUTPUT`.

Markdown sections: **Identity · Status · NAF/APE · Address · Établissements · Source**.

## Pricing (pay-per-event)

| Event | Price |
|---|---|
| `apify-actor-start` | $0.001 |
| `sirene-card` (primary) | **$0.004** per successful card |

Invalid input, not found, auth errors, rate limits after retries, and other failures are **free** (dataset row written, not charged).

## Example

```json
{
  "sirens": ["380129866", "552081317"],
  "provider": "auto",
  "apiKey": "YOUR_KEY",
  "includeEtablissements": true,
  "includeRagChunks": true
}
```

## Limits

Default run: **256 MB**, timeout **3600 s** (max memory 1024 MB). Keep concurrency low on the keyless path.

## Licence

Actor source is AGPL-3.0. Underlying SIRENE data is published under Licence Ouverte / Open Licence 2.0 (INSEE / Etalab).

## Source code

Open source (AGPL-3.0): https://github.com/xbox002000/france-sirene-markdown
