# sansadsaar-proceedings-data

Index data for the **debates** and **questions** corpora on [SansadSaar](https://github.com/NakliTechie/SansadSaar).

- **Debates** — Lok Sabha speech-level records (~57K target) + Rajya Sabha daily-proceedings PDFs.
- **Questions** — Lok Sabha per-question records (~280K target) + Rajya Sabha per-sitting-day bundle PDFs.

## Why a separate repo?

These two corpora are high-volume and person-centric (they belong to MPs more than to documents). Splitting them off keeps the document-centric [parliamentwatch-data](https://github.com/NakliTechie/parliamentwatch-data) comfortably under Cloudflare Pages's per-project file count cap as backfill fills out. They're also the natural future input for a per-MP analytic app.

## Credits

Sourced from sansad.in's `api_ls/*` and `api_rs/*` endpoints. All scraper code is original.
