# Checklist

Tick every line. A line that cannot be ticked is named in the hand-off with
the reason; nothing is quietly skipped.

## The `disease/` directory

- [ ] `manifest.json` — questions 1, 5, 6. `hosting.url: null`; the four
      `about.*` texts `null`; `additionalSources: []`. Validates against
      `manifest.schema.json` (`deno test tests/disease_manifest_test.ts`).
- [ ] `pipeline.json` — questions 2, 3, 6, 7. `$comment`s rewritten.
      Validates against `pipeline.schema.json`
      (`uv run pytest tests/pipeline/test_disease.py`).
- [ ] `vocabulary.json` — question 5. `synonyms: []`, `untracked: []`.
- [ ] `timeline.json` — question 6. Populations in the manifest's order; at
      least one family with at least one mechanism.
- [ ] `phenogram.json` — one family per vocabulary family, hues from the
      cSVD palette (`deno test tests/phenogram_encoding_test.ts`).
- [ ] `omim_info.csv` — question 7, or the header line alone.
- [ ] `prompt.md` — every section id the cSVD file has, in its order;
      `## strategy.monogenic_genes` equals `monogenicGenes`;
      `## traits.canonical` equals the vocabulary keys; `## examples` obeys
      the rule in `SKILL.md`, or is `<!-- examples: none yet -->`. Lines not
      wrapped.
- [ ] `recall_gold.csv` — question 9. `recall_baseline.json` deleted.
- [ ] `README.md` — the disease page, then `deno fmt disease/README.md`:
      `deno task check` formats Markdown too, and a hand-written table is
      the usual failure.

## The start

- [ ] `deno task data:empty`.
- [ ] `git rm -r tests/csvd e2e/tests/csvd tests/pipeline/csvd tests/scripts/csvd`.
- [ ] `git rm -r tests/pipeline/cassettes/test_query_recall_gold` — the cSVD
      recall recordings, which `--record-mode=once` would replay in place of
      the fork's own.

## Test-side edits a fork makes by hand

Each carries cSVD content outside `disease/` for a reason, and each is a
judgement the fork's owner makes once:

- [ ] `_ADMITTED_UNASKED` in `tests/pipeline/test_prompt_vocabulary.py` →
      `{}`. Its entries explain why two cSVD trait keys are absent from the
      cSVD prompt's canonical sentence; a fork whose sentence names every key
      has nothing to explain, and a stale entry fails
      `test_the_admitted_unasked_list_does_not_go_stale`.
- [ ] `_ALLOWED` in `tests/pipeline/test_no_disease_literals.py` and
      `ALLOWED` in `tests/no_disease_literals_test.ts` — only if the greplist
      tests hit. They scan for the **new** manifest's terms, so the cSVD
      entries (comments quoting HTRA1 record counts, the `svd-` CSS
      namespace) go on matching their own text and stay; add an entry only
      for a hit that is about something other than the disease.
- [ ] `PROTECTED_DATA_SENTINELS` in `server/protected_data_build.ts` — the
      leak canaries: strings the build asserts appear in no public chunk. On
      empty data they appear nowhere, which passes, so this waits for the
      first regenerated `data/`; then pick seven strings that exist only in
      the new data (a merged gene symbol with a slash, a registry id, a
      facility name, an OMIM phenotype, a DOI, a UniProt accession) and
      replace the list.
- [ ] `e2e/package.json` — `name` and `description` name the cSVD dashboard.
- [ ] `docs/screenshots/` — the README's six screenshots, of the cSVD
      dashboard. Like the sentinels this waits for the first regenerated
      `data/`; then `deno task screenshots` (libwebp's `cwebp` on the `PATH`)
      recaptures all six from the fork's build. Named in the hand-off while
      the live run is still ahead.
- [ ] `tests/pipeline/conftest.py` — the throwaway container
      `csvd-pg-pytest` and the `csvd_pytest` database, user and password, if
      the fork prefers its own names. Cosmetic; the container is destroyed
      with the session either way.
- [ ] `.gitignore` — the four `tests/pipeline/csvd/...` lines and
      `deno.json`'s `tests/pipeline/csvd/cassettes` exclude match nothing
      now; remove them or leave them, they are inert.
- [ ] `static/institute/logo-light.svg` and `logo-dark.svg` — replaced by
      the fork's, or `institute.logo.srcOnDark: null` with one file, which is
      then drawn on the dark navbar and the light login card and must read
      on both.

## Gates

- [ ] `deno task check`
- [ ] `deno task test:coverage` (100 % under `lib/`; 85/95/90 global)
- [ ] `uv run ruff check .`
- [ ] `uv run ty check`
- [ ] `uv run pytest --cov=pipeline --cov=pipeline/alembic --cov-branch --cov-report=term-missing:skip-covered`
      (99.5 % floor)
- [ ] `uv run pytest tests/scripts`
- [ ] `npx --prefix e2e playwright test -c e2e/playwright.config.ts`

## Measurements

- [ ] `uv run python -m pipeline.main --test-mode --days-back 365` — papers a
      year, × 0.09 USD; above ~2,000 papers, back to question 3 to remove the
      broadest marker terms or make them more specific (never shorter: a
      shorter phrase matches more), or to question 2 for a narrower heading
      than a broad parent.
- [ ] `uv run python -m scripts.measure_recall`, then `--write-baseline`, then
      `uv run pytest tests/pipeline/test_query_recall_gold.py --record-mode=once`;
      baseline and cassette committed.
- [ ] After the first live run's export, and any later one that adds a
      reference: `--write-baseline` again,
      `rm -r tests/pipeline/cassettes/test_query_recall_gold`, then
      `--record-mode=once` again. `gold_pmids()` counts every PMID
      `data/table1.json` cites, so four replay tests fail until then; named
      in the hand-off while the live run is still ahead.

## Fork setup and hand-off

- [ ] `git remote add upstream …`
- [ ] `.env` from `.env.example`, `DB_NAME=<key>_dashboard`,
      `DB_USER=<key>_user` and a `DB_PASSWORD` (the pipeline and alembic
      refuse to start without them), the two login values, `ANTHROPIC_API_KEY`,
      `ENTREZ_EMAIL` and `UNPAYWALL_EMAIL` filled. The passphrase
      single-quoted, with no apostrophe; `NCBI_API_KEY` left commented out
      unless the key is real (NCBI answers any other value with HTTP 400).
- [ ] `deno install`; `uv sync --group dev --group figure`;
      `deno task e2e:install` (npm ci alone downloads no browser; on Linux,
      `npx --prefix e2e playwright install --with-deps chromium` as well).
- [ ] On macOS, `brew services start postgresql@18` and
      `export PATH="$(brew --prefix postgresql@18)/bin:$PATH"` (keg-only).
      `createuser -s -P <key>_user`, typing the `DB_PASSWORD` value, and
      `createdb -O <key>_user <key>_dashboard`, each after `sudo -u postgres`
      on Linux; then `cd pipeline && uv run alembic upgrade head`.
- [ ] First commit.
- [ ] `deploy` skill: `fly.toml`'s `app = "<key>-dashboard"` (each `_` of
      the key written `-`) and `primary_region`; `fly secrets set` with
      `DASHBOARD_PASSPHRASE` (single-quoted) and `DASHBOARD_SESSION_SECRET`;
      `FLY_API_TOKEN` stored on GitHub.
- [ ] `hosting.url` written into `manifest.json` and `disease/README.md`
      once the site answers; committed.
- [ ] The hand-off message: files, gates, papers and cost, recall and the
      misses by title, examples present or `none yet`, test-side edits made,
      anything skipped and why.
