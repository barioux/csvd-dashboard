---
name: new-disease
description: Use when turning this repository into a dashboard for another disease - the nine-question interview, what each answer writes under disease/, the empty-data start, the csvd/ trees to delete, the gates and the cost estimate, and the fork and deploy hand-off.
---

# Setting the repository up for another disease

## When to use

A researcher has forked the repository and wants the dashboard, the pipeline
and the tests to serve a disease of their own. Everything that names the
disease lives under `disease/` (the root `CLAUDE.md`, "The disease seam"), so
the job is to fill that directory from an interview, start from empty data,
remove the tests that name cSVD, and prove every gate green before the first
live run. The worked example is the live `disease/` directory: read each file
there before drafting its replacement, because the shapes are enforced by
schema and by test and the example is the shortest description of both.

The interview is `interview.md` in this directory; the flat list this skill
ticks off is `checklist.md`. Keep both open.

## The rule that never bends

**Every `<example>` block in `disease/prompt.md` cites a PMID the researcher
supplied in question 8 and quotes a sentence the researcher supplied or that
you copied verbatim from the abstract `efetch` returned for that PMID.** You
never compose a paper, a gene-paper pair, a finding or a source quote, and you
never adapt a cSVD example by swapping names. The examples are the part of the
prompt that teaches the model what a finding for this disease reads like; an
invented one teaches it to invent. With fewer than two papers, the `## examples`
section is the single line `<!-- examples: none yet -->` and you say so in the
hand-off, naming question 8 as the way to fill it.

The same discipline applies, more loosely, to everything else you draft: a
term, a trait, a mechanism or an OMIM row comes from the researcher's answer or
from a lookup you ran and can show, never from what a disease "probably" has.

## Order of work

1. **Interview** — the nine questions of `interview.md`, in order, because
   each feeds the next. Run the pre-fill lookups before asking, and show the
   researcher what came back; a count they can see is a decision they can
   make.
2. **Draft `disease/`** in dependency order, validating each file as it lands
   (`deno task check` and `uv run pytest tests/pipeline/test_disease.py` are
   cheap and name the key that is wrong):
   1. `manifest.json` — questions 1 and 6. `hosting.url` is `null` until the
      deploy hand-off. `about.citation`, `board`, `contactUs` and
      `acknowledgements` are `null` until the researcher has text;
      `additionalSources` is `[]`. `citationStandard` is `null` unless the
      field has a phenotype standard the researcher names (question 5).
   2. `pipeline.json` — questions 2, 3, 6 and 7. Rewrite the `$comment`s; the
      ones there quote cSVD measurements.
   3. `vocabulary.json` — question 5. `synonyms` and `untracked` start `[]`.
   4. `timeline.json` — question 6: the populations in the manifest's order,
      each with `label` (an array of lines), `color` and `band`; the
      mechanisms and families. **At least one family with at least one
      mechanism is required** (`tests/timeline_encoding_test.ts` refuses an
      empty family list), so question 6 asks for one.
   5. `phenogram.json` — one family per `family` value in the vocabulary,
      with `hue` and `tint`. Reuse the cSVD palette in the same order — the
      seven pairs there pass the lightness band, the tint floor and the
      adjacent-ΔE check `tests/phenogram_encoding_test.ts` enforces; a new
      pair has to be checked by that test before it stays.
   6. `omim_info.csv` — question 7, one row per confirmed OMIM phenotype
      entry, the header line alone when nothing is confirmed.
   7. `prompt.md` — every `## id` section the template reads, in the same
      order as the cSVD file; `## strategy.monogenic_genes` is exactly
      `pipeline.json`'s `monogenicGenes` joined by `, `
      (`tests/pipeline/test_prompt_assembly.py` pins the two equal), and the
      `## traits.canonical` list names every `traits[*].key` of the
      vocabulary and nothing else (`tests/pipeline/test_prompt_vocabulary.py`
      reconciles the two in both directions). `## examples` follows the rule
      above. Do not wrap lines: every newline reaches the model.
   8. `recall_gold.csv` — question 9. `recall_baseline.json` is **deleted**;
      `scripts.measure_recall --write-baseline` writes the fork's own.
   9. `disease/README.md` — name, institute, maintainer, hosted URL (or
      "not yet hosted"), the trait table, the populations, the query
      rationale from questions 2 and 3, "How to cite: pending". Then
      `deno fmt disease/README.md`; the check gate formats Markdown too.
3. **`deno task data:empty`** — every `data/*.json` empty or `null`, the OMIM
   table from the CSV, the cytobands untouched. No database is needed.
4. **Remove the four cSVD test trees** with one command:
   `git rm -r tests/csvd e2e/tests/csvd tests/pipeline/csvd tests/scripts/csvd`.
   Everything left holds for any disease, bar one recording: the cSVD recall
   cassettes sit beside the generic replay test, so remove them too with
   `git rm -r tests/pipeline/cassettes/test_query_recall_gold` -- the recall
   step records the fork's own, and one left in place is replayed instead.
5. **The test-side edits** `checklist.md` names — the ones a fork must make by
   hand because they carry cSVD content outside `disease/` for a reason each.
6. **Gates**, then the **cost estimate**, then the **recall measurement**
   (below).
7. **Fork setup** and the **deploy hand-off** (below).

## What the code reads

| File                            | Read by                                                                                                                                       |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `disease/manifest.json`         | `lib/disease/manifest.ts` (and through it `site.ts`, `populations.ts`, `cell_types.ts`, `citation.ts`); `pipeline/disease.py`. **Ships in a public client chunk served before login**, so no gene symbol may live in it. |
| `disease/manifest.schema.json`  | `tests/disease_manifest_test.ts`, `tests/pipeline/test_disease.py` — `additionalProperties: false` on both sides                              |
| `disease/pipeline.json`         | `pipeline/disease.py` only: `pubmed_search.py` (the query), `clinical_trials_fetch.py` (search terms, the condition gate), `data_merger.py` (aliases, monogenic genes), `config.py` (run label, gene cap) |
| `disease/pipeline.schema.json`  | the same two tests                                                                                                                            |
| `disease/vocabulary.json`       | `lib/constants.ts` (`GWAS_TRAIT_CHOICES`), `lib/phenogram.ts`, `pipeline/extraction_models.py` (`CANONICAL_TRAITS`, the tool-schema enum), `pipeline/export/tables.py` (synonym folds), `scripts/phenogram_figure.py` |
| `disease/timeline.json`         | `lib/timeline.ts`, `lib/timeline_encoding.json`, `scripts/timeline_figure.py`                                                                 |
| `disease/phenogram.json`        | `lib/phenogram.ts`, `lib/phenogram_encoding.json`, `scripts/phenogram_figure.py`                                                              |
| `disease/omim_info.csv`         | `pipeline/export/tables.py` (`read_omim_csv`) → `data/omim_info.json`; `scripts/reconcile_omim.py`                                            |
| `disease/prompt.md`             | `pipeline/disease.py` → `pipeline/prompts.py` (the v7 template's slots)                                                                       |
| `disease/recall_gold.csv`       | `pipeline/query_eval.py` → `scripts/measure_recall.py`, `tests/pipeline/test_query_recall_gold.py`                                            |
| `disease/recall_baseline.json`  | `tests/pipeline/test_query_recall_gold.py` (skips while absent)                                                                               |
| `disease/README.md`             | people                                                                                                                                        |

## Verification

The five gates, in the worktree, on the empty data with the trees removed —
exactly what the `empty-data` CI job runs:

```bash
deno task check && deno task test:coverage
uv run ruff check . && uv run ty check
uv run pytest --cov=pipeline --cov=pipeline/alembic --cov-branch --cov-report=term-missing:skip-covered
uv run pytest tests/scripts
npx --prefix e2e playwright test -c e2e/playwright.config.ts
```

Two disease-literal tests scan the code for the *new* disease's terms
(`tests/no_disease_literals_test.ts`, `tests/pipeline/test_no_disease_literals.py`).
A hit is any line holding one of them as a whole word: the name and the
institute's name in any case, the abbreviation, short form, institute short
name and gene symbols as written. Reword a line that is about the disease. A
hit is often a unit, an identifier or an unrelated code instead (OMIM's `AD`
for autosomal dominant); allow-list that with a reason -- an `ALLOWED` entry
`[path regex, line regex, reason]` in the TypeScript test, a `_ALLOWED` entry
`(file, term as written): reason` in the Python one. A one- or two-letter short
form matches ordinary words and codes, so expect a few.

**Cost estimate**, with no database:

```bash
uv run python -m pipeline.main --test-mode --days-back 365
```

It prints the papers the query retrieves in a year. The `run-long-window`
skill's figure for the cSVD query is ~795 papers ≈ $70 of extraction, so
estimate **papers × 0.09 USD**, print it, and above roughly **2,000 papers a
year send the researcher back to question 3** to remove the broadest marker
terms or make them more specific. Each one adds the papers that use it beside
a disease phrase, and a shorter phrase matches more, so shortening a term
raises the count. A MeSH heading that is a broad parent inflates it too, even
behind the genetics gate; question 2 replaces it with a narrower one.

**Recall**, once `recall_gold.csv` has its ten to thirty PMIDs:

```bash
uv run python -m scripts.measure_recall                    # prints matched and missed, with titles
uv run python -m scripts.measure_recall --write-baseline   # records disease/recall_baseline.json
uv run pytest tests/pipeline/test_query_recall_gold.py --record-mode=once
```

The third command records the cassette the CI test replays; commit it with
the baseline. A missed gold paper is a question for the researcher (is it
about the disease, or is the query narrow?), never a reason to edit the gold
list.

`gold_pmids()` also counts every PMID `data/table1.json` cites. That file is
empty at this point, and the first live run's export fills it: that export,
and any later one that adds a reference (a weekly run's too), moves the
denominator, and four replay tests fail until the measurement and its
recording are made afresh. Say so in the hand-off when the first live run is
still ahead, with the three commands:

```bash
uv run python -m scripts.measure_recall --write-baseline
rm -r tests/pipeline/cassettes/test_query_recall_gold
uv run pytest tests/pipeline/test_query_recall_gold.py --record-mode=once
```

## Fork setup

Run from the fork's checkout, after the drafting above is green:

```bash
git remote add upstream https://github.com/mathieubpoiriericm/csvd-dashboard.git
cp .env.example .env            # filled as below
deno install
uv sync --group dev --group figure
deno task e2e:install           # npm ci, then the Chromium build the e2e gate drives
brew services start postgresql@18                        # macOS: Homebrew's formula is keg-only,
export PATH="$(brew --prefix postgresql@18)/bin:$PATH"   # so neither started nor on PATH
createuser -s -P <key>_user && createdb -O <key>_user <key>_dashboard   # -P asks for DB_PASSWORD
cd pipeline && uv run alembic upgrade head && cd ..
git add -A && git commit -m "Set the repository up for <disease name>"
```

`<key>` is `disease.key` from the manifest. The `upstream` remote is what the
`update-from-upstream` skill merges from. `npm ci --prefix e2e` alone installs
no browser (Playwright's packages have no install script), and the e2e gate
fails with "Executable doesn't exist"; on Linux,
`npx --prefix e2e playwright install --with-deps chromium` adds the system
libraries Chromium needs.

`.env` takes `DB_NAME=<key>_dashboard`, `DB_USER=<key>_user` and a
`DB_PASSWORD` (the pipeline and alembic refuse to start without all three),
the two login values, `ANTHROPIC_API_KEY`, `ENTREZ_EMAIL` and
`UNPAYWALL_EMAIL`. Three of them bite:

- **The passphrase is single-quoted** (`DASHBOARD_PASSPHRASE='…'`) and holds
  no apostrophe: `--env-file` ends an unquoted value at a `#` and expands a
  `$NAME`, so the login refuses the sentence the researcher meant.
  `fly secrets set` takes it in the same quotes, since a shell reads it the
  same way.
- **`NCBI_API_KEY` stays commented out** unless the researcher has a real
  key. NCBI answers any other value, an empty one included, with HTTP 400 on
  every call, and the pipeline reports only "Bad Request".
- **The role carries `DB_PASSWORD`.** The pipeline and alembic log in over
  TCP; Homebrew's server trusts localhost, but a `scram-sha-256` one (Linux
  packages, the official image) refuses a role created without `-P`. On
  Linux, skip the two Homebrew lines and run `createuser` and `createdb`
  after `sudo -u postgres`; or use the container recipe in the
  `regenerate-data` skill.

## Deploy hand-off

Hand the researcher to the `deploy` skill, whose "Setting up an app" section
they run themselves, because it touches their Fly.io account and secrets. Four
values come with them:

- the Fly app name `<key>-dashboard`, with each `_` of the key written `-`,
  because the name becomes the host name `<key>-dashboard.fly.dev`, to go in
  `fly.toml`'s `app`;
- a `primary_region` near their readers;
- the two runtime secrets `DASHBOARD_PASSPHRASE`, in the same single quotes as
  `.env`, and `DASHBOARD_SESSION_SECRET`, from their `.env`.

The `FLY_API_TOKEN` repository secret is what lets CI deploy. Once the site
answers, write its URL into `hosting.url` in `disease/manifest.json` and
`disease/README.md`, and commit.

## What the hand-off says

One message: the files written, the gates' results, the yearly paper count and
its cost, the recall figure and every missed PMID by title, whether the prompt
has examples or the `none yet` marker, the test-side edits made, and that the
first live run's export moves the recall denominator, with the three commands
that measure it again, and leaves the README's cSVD screenshots for
`deno task screenshots` to replace. If anything was skipped — a lookup that
failed, a question the researcher deferred — it is named there, not left for
the next run to find.
