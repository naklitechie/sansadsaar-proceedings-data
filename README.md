# sansadsaar-proceedings-data

Data + scrapers for the `proceedings` macro-group on SansadSaar:
**Debates** (LS speech-level + RS daily-proceedings) and **Questions**
(LS per-question + RS per-bundle-PDF). Split out from
`parliamentwatch-data` on 2026-05-14.

## Why a separate repo?

The two corpora here are person-centric and high-volume:
- Debates: ~57K records target across LS-15..18 + RS sessions.
- Questions: ~280K records target across LS-15..18; bundled-PDF RS.

Combined they're the bulk of parliamentwatch-data's footprint and
the natural future input for [Netas Explorer](https://github.com/NakliTechie/sansadsaar)
(the per-MP analytic app). Splitting keeps the document-centric
`parliamentwatch-data` (DRSC / CAG / Bills / LC / FC) under the
CF Pages 20K-files cap with comfortable margin.

## Layout

```
sansadsaar-proceedings-data/
├── docs/
│   ├── debates/                  ← served at debates.sansadsaar-data.naklitechie.com
│   │   ├── reports-meta.json + reports-{ls,rs}-NN.json shards
│   │   ├── texts-NN.json + texts-meta.json
│   │   ├── markers.json          ← consolidated per-record markers
│   │   ├── search-bundle-NN.json + search-index-NN.json
│   │   └── manifest.json / audit.json / meta.json
│   └── questions/                ← served at questions.sansadsaar-data.naklitechie.com
│       └── (same shape)
├── debates/                      ← scraper module
│   ├── common.py
│   └── scrapers/{loksabha,rajyasabha}.py
├── questions/                    ← scraper module
│   ├── common.py
│   └── scrapers/{loksabha,rajyasabha}.py
├── build_debates.py              ← orchestrator (extract + derive)
├── build_questions.py            ← orchestrator (extract + derive)
├── parliamentwatch_text_shards.py  ← shared helpers (vendored from
│                                     parliamentwatch-data per
│                                     Independence Principle)
└── .github/workflows/
    ├── debates.yml + debates-derive.yml + debates-ocr.yml
    └── questions.yml + questions-derive.yml
```

## Upstream

- **Debates LS**: sansad.in/api_ls/debate/*. Speech-level records.
- **Debates RS**: sansad.in/api_rs/debate/* + cms.rajyasabha.nic.in PDFs.
- **Questions LS**: sansad.in/api_ls/question/qetFilteredQuestionsAns.
- **Questions RS**: sansad.in/api_rs/business/{starredQuestionsItemWise,unStarredQuestionsItemWise}.

Same archive.org-first policy as the rest of the project: if a public
mirror exists, prefer it over re-driving the upstream.

## Output

Two CF Pages projects (one per corpus) serve the JSON shards. The
SansadSaar app's `app/corpora/debates/index.js` and
`app/corpora/questions/index.js` consume them via per-corpus
dataBaseUrl overrides.

## License

Code: original. Data flows from public-domain Government of India
publications via sansad.in.
