# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Commands

Tasks live in `deno.json`. Run `deno install` first — `nodeModulesDir` is
`"manual"`, so dependencies are never resolved implicitly.

A single end-to-end spec:
`cd e2e && npx playwright test
tests/genes-filters.spec.ts`.

`uv run pytest` collects only `tests/pipeline` (`testpaths` in
`pyproject.toml`), so after touching `scripts/` run
`uv run pytest tests/scripts` explicitly — CI names that path in a step of its
own for the same reason. `deno task check` (fmt, lint, `deno check`) and
`deno task test:coverage` are the TypeScript gates; `uv run ruff check .` and
`uv run ty check` the Python ones. Do not run `ruff format`.

`uv run python -m scripts.reconcile_omim` compares the curated
`gene_monogenic_links` rows against ClinVar's OMIM numbers in
`data/gene_annotations.json`. It is a report and writes nothing: the curated
column is a scientific judgement, and every disagreement it prints needs a
human.

A long `--days-back` run is resumable: every paper is checkpointed to
`logs/pipeline_checkpoint.jsonl` as it finishes, so a crash costs the papers in
flight and nothing else — re-run the same command. **`--batch` is the exception
to "and nothing else"**: every paper is in flight until the batch's results
arrive, and the batch id is held only in the run log, so a crash inside the
up-to-24h poll loses the whole submission. Re-running resubmits every paper and
pays again while the first batch completes at Anthropic unretrieved.
`uv run python -m scripts.backfill_pubmed` walks the window in committed chunks
instead, for when the database should be updated as it goes. See "Long windows"
in `pipeline/CLAUDE.md`.

Regenerating `data/*.json` needs PostgreSQL reachable and `.env` populated. The
commands, the hardened `dhi.io/postgres:18.6` container with its three
load-bearing details, and the print-figure and cytoband tasks are in the
`regenerate-data` skill (`.claude/skills/regenerate-data/SKILL.md`). **Migration
014 renames `clinical_trials.svd_population` to `target_population`, and the
export, the merge and the clinical-trials sync all read the new name, so run
`cd pipeline && uv run alembic upgrade head` before the first `deno task data`
after merging the `disease-reuse` branch.**

`--export` does that same work at the end of a live run, so the figures, the
About page's date and the run widget track the database without a second
command. It **stops at the working tree** — the files are left modified and
nothing is committed, because the curated `genes` table is a scientific
judgement and the e2e fixtures pin exact row counts. It is refused with
`--local-pdfs` / `--pmids`, which publish no data; it is skipped after a
`--dry-run` or `--test-mode` preview, which writes nothing; and it runs even
when a pipeline **failed**, because `_record_failed_run` persists that failure
for the widget and not exporting is what would keep it invisible. An export
failure is folded into the exit code — the database having moved while the files
did not is the condition the flag exists to remove.

**A PubMed run refreshes the three lookup caches before that export**, for its
own new gene keys and PMIDs only (`_refresh_lookup_caches`, skipped when
`--sync-external-data` ran in the same invocation, which covers them anyway).
Nothing inside a run writes `ncbi_gene_info`, `uniprot_info` or
`pubmed_citations`, and `complete_lookup` turns a key it cannot find into a row
of nulls rather than failing — so `--export` used to publish every newly
inserted gene with a null NCBI uid, description and alias list and every newly
attached reference as `"(citation not available)"`. Each source fetches only its
own cache misses, so this costs what the run added and nothing more. It is
best-effort: a source that cannot be reached is logged as an error and the
export still runs, because the database has moved and leaving the files behind
it is worse. A run that _raised_ leaves no report to read those keys out of, so
whatever it merged before it died waits for the next `--sync-external-data`.

`deno task data:empty` (`pipeline/export/empty.py`) writes the other end of the
range with no database at all: every row file `[]`, both run files `null`, the
map file with no sites, `omim_info.json` from the CSV, and `cytobands_hg38.json`
untouched because it is genome reference data. It is where a fork starts and
what the `empty-data` CI job runs before every gate.
`uv run python -m scripts.measure_recall` measures the PubMed query against
`disease/recall_gold.csv` and, with `--write-baseline`, records the figure in
`disease/recall_baseline.json`; `tests/pipeline/test_query_recall_gold.py`
replays that measurement from a cassette.

`deno task geocode` stays separate because it calls out to clinicaltrials.gov,
so a run that adds a trial needs it too or `tests/data_contract_test.ts` fails
on the `nctIds` set. `deno task figure` stays manual as well; `figures/` is
gitignored.

Tests never touch that database. `tests/pipeline/conftest.py` clears the `DB_*`
variables — and the whole `PIPELINE_*` namespace, so a value left in `.env` for
a local experiment cannot change what the default-config tests measure — and a
test needing real SQL uses the throwaway database `CSVD_TEST_DB_URL` names (CI's
`postgres` service) or, with that unset, brings up its own container — from
`dhi.io/postgres:18-alpine3.23`, not the glibc image the skill documents for a
mounted data directory — on its own address under Apple's `container` runtime.
See "Database tests" in `pipeline/CLAUDE.md`.

Vite is pinned to 7.x because `@fresh/plugin-vite` requires `vite@^7.1.4`.

`deno task perf` measures every route and fourteen interactions under 4x CPU
throttling; the `perf` skill holds how to run it and how to read it.

`deno task screenshots` builds and recaptures the README's six screenshots into
`docs/screenshots/` as lossless WebP, through libwebp's `cwebp`. Unlike the perf
harness it never reuses a listening server: it starts its own on a free port
with made-up credentials, so an older build cannot be captured as current. The
images show the committed data as the build bundles it, so rerun it after a data
regeneration or a visible change to a page, and commit them with that change.

**CI's five test jobs run on `${{ vars.CI_RUNNER || 'ubuntu-latest' }}`.** The
private repository sets `CI_RUNNER` to `csvd-ci`, the label of six self-hosted
runners in an Ubuntu VM on a home mini PC, registered to that repository alone;
the public repository and every fork leave it unset and stay on GitHub's
runners. One host runs several jobs at once, so no job may take a fixed host
port -- the postgres services publish on a free one and each runner sets its own
`E2E_PORT` -- and `container` and `deploy` stay hosted. When the machine is
down, `gh variable delete CI_RUNNER` is the fallback. The `ci-runner` skill
(`.claude/skills/ci-runner/SKILL.md`) has the machine, the provisioning script
and the Playwright-upgrade step the workflow cannot do for it.

**`compression()` in `main.ts` is the outermost middleware, and that position is
load-bearing**: nothing before it compressed the built bundles, and it must stay
outside `protectDataAssets()` to cover the protected chunk.
`.claude/rules/compression.md` loads with `main.ts` and `server/compression.ts`
and carries the measurements, the skips and why it steps aside in development
mode.

**Playwright writes into `.playwright-mcp/` inside the project, and Vite watches
the project.** `vite.config.ts` ignores that directory and `logs/` for exactly
this reason. Before it did, screenshotting or logging during `deno task dev` put
the page into a reload loop — every write triggered a full reload, and a
`console.log` in an island made it self-sustaining. It looks exactly like an
island that will not hydrate. It is not.

## Worktrees

`.venv`, `.env` and `node_modules` live in the main checkout only. In a
`.claude/worktrees/*` checkout:

- Run every uv command with `UV_PROJECT_ENVIRONMENT=<main checkout>/.venv`, the
  absolute path of the main checkout's `.venv`. Without it `uv run` builds a
  second venv (torch included) inside the worktree, and its auto-sync can drop
  the `figure` group from the shared one -- `uv sync --group dev --group figure`
  restores it, and until then `tests/scripts` skips and `ty` warns about
  matplotlib.
- `deno install` once per worktree, and `npm ci --prefix e2e` for Playwright.
  The whole suite runs from the repo root:
  `npx --prefix e2e playwright test -c e2e/playwright.config.ts [tests/x.spec.ts]`.
- Copy `.env` in before `deno task data` or a live run; it is gitignored.
- The branch is `worktree-<name>`; push it as `git push origin HEAD:<name>` -- a
  bare `git push` refuses the mismatch.

## Architecture

One pipeline, one language, meeting the web app at a JSON file boundary.

**The disease seam is `disease/`.** Everything that names the disease lives
there and nowhere else, split across two manifests for one reason:
`server/protected_data_build.ts` lists `COL4A1/2` as a leak canary and every
island bundle embeds the web-facing manifest, so no gene symbol may live in it.
`manifest.json` holds the web-facing keys (prose, institute, contact, about,
hosting, populations, populationField, cell-type glossary, citation standard);
`pipeline.json` holds the pipeline-only keys (search terms, monogenic genes,
gene aliases, run label, gene cap) and is read only by `pipeline/disease.py`.
Both have a JSON Schema beside them (`manifest.schema.json`,
`pipeline.schema.json`). `lib/disease/manifest.ts` imports the raw JSON, so
**every key of `disease/manifest.json` — not only the ones a page renders —
ships inside a public client chunk that is served before login**, which is why
gene symbols live in `disease/pipeline.json` and why each schema's
`additionalProperties: false` is enforced by a test on both sides of the seam
(`tests/pipeline/test_disease.py`'s `Draft202012Validator` pass,
`tests/disease_manifest_test.ts`'s structural checker). Also in `disease/`:
`vocabulary.json`, `prompt.md` (the disease half of the extraction prompt; the
methodology is the v7 template in `pipeline/prompts.py`), `phenogram.json`
(families), `timeline.json` (populations, mechanisms, families) and
`omim_info.csv`. `prompt.md` is excluded from `deno fmt` in `deno.json` because
`deno fmt` rewraps Markdown prose and every inserted newline would reach the
model; `tests/pipeline/csvd/test_csvd_prompt_assembly.py` pins the cSVD
rendering byte-identical to the v6 literals. TypeScript reads the manifest
through the narrow modules under `lib/disease/` — `site.ts`, `populations.ts`,
`cell_types.ts`, `citation.ts` — and Python through `pipeline/disease.py`, which
is stdlib-only so `config.py` and `extraction_models.py` can both import it.
`tests/no_disease_literals_test.ts` and
`tests/pipeline/test_no_disease_literals.py` scan the code for the manifest's
own terms and fail on any hit outside a reasoned allow-list. Every measurement
in this file and in `pipeline/CLAUDE.md` is of the cSVD dataset this repository
was built on.

**The tests that name the disease live in four `csvd/` trees** — `tests/csvd`,
`tests/pipeline/csvd`, `tests/scripts/csvd` and `e2e/tests/csvd` — and nowhere
else. A test goes there when it names cSVD content (a gene, a trait, a
population, a search term) or pins a cSVD number (a row count, a recall figure,
the first marker the radar draws). Everything outside them holds for any
disease: an invariant stays where it is, and a test that used to pin a
committed-data number now derives it at test time —
`e2e/fixtures/expected.deno.ts` under Deno so the counts come from the app's own
`lib/`, the `committed_rows` fixture in `tests/pipeline/conftest.py`,
`tests/fixtures/rows.ts` for synthetic rows — or skips with a reason naming the
empty file. Both modes must stay green: the committed data with the trees
present, and `deno task data:empty` with the four trees removed, which is what
the `empty-data` CI job runs. A fork removes the trees with one `git rm -r` of
the four paths, and the upstream recall cassettes in
`tests/pipeline/cassettes/test_query_recall_gold` with another (they sit beside
the generic replay test, and `--record-mode=once` would replay them instead of
recording the fork's), and starts from the empty export; the `new-disease` skill
walks that. The `empty-data` job keeps this repository's `disease/`, so a third
mode runs a fork's own: `deno run -A scripts/adapt_fork_check.ts` (CI's
`adapt-fork` job) unpacks two wizard bundles over copies of the tree, follows
the adaptation checklist and runs every gate on each. A generic test that reads
a manifest string out of rendered HTML goes through `tests/helpers/html.ts`'s
`escapeHtml`, and one that needs two populations builds them, because a fork's
institute can hold `&` and its radar one population.

**Nothing queries a database at request time, and nothing fetches JSON at
runtime.** The modules under `lib/data/` use `import … with { type: "json" }`,
so each file is bundled into whatever imports it. **Import the narrow module,
not the barrel** — `lib/data.ts` re-exports all of them, and an island that
reaches for it pulls the whole ~400 KB dataset into its client bundle instead of
the one table it renders. The barrel exists for the tests that exercise the
complete boundary. Consequences worth remembering:

- Editing `data/*.json` requires a rebuild/HMR cycle to show up.
- **`data/gene_annotations.json` is the only file whose rows are a pivot rather
  than a table dump**: one row per `(gene, disease)` rather than one per
  cross-reference, because the `gene_annotations` table `--sync-annotations`
  fills (see `pipeline/CLAUDE.md`) held ~7,700 rows on 2026-09-24, and any
  island that imports this file bundles it. None does yet — its readers are
  `scripts/reconcile_omim.py`, the contract tests and the `lib/data.ts` barrel.
  Only ClinVar's attested diseases and Orphanet's enrichment of them are
  published; Open Targets' ranked associations and GO terms stay in PostgreSQL,
  where a query that wants them can reach them. **`relatedXrefs` is not a list
  of more identifiers for the row's disease** — each entry names a _different_
  concept and carries the relation its source reported, verbatim and never
  invented. Merging one into `omimId` would publish a different disease's OMIM
  number as this gene's, which is the error the annotations exist to remove.
  `omimSeries` is separate for the same reason: a phenotypic series names a
  group of phenotypes, so `omimByNumber` can never resolve one. It is also the
  one export file that is **skipped rather than emptied** when its table has no
  rows, because only `--sync-annotations` fills it and `[]` would otherwise
  publish over the committed file atomically. Only a genuinely empty read
  reaches that rule: `read_disease_annotations` tolerates a missing table and
  nothing else, so a renamed column or a revoked `SELECT` fails the export
  rather than freezing the file at its last good read.
- `data/pipeline_run.json` is committed as a bare `null` until a run records a
  report, exactly as `data/pipeline_status.json` was. Both states are covered;
  the About page falls back to the summary card for the null one.
- **`data/pipeline_syncs.json` is the reference-data refreshes**, one entry per
  sync mode, committed as `[]` until one is recorded. It is a separate file
  because a refresh is a separate event from a run — and a separate table, so
  that no query feeding the run widget or the About page's date badge can see
  one. It is what publishes the upstreams only the syncs touch: ClinVar,
  Orphadata, Open Targets, UniProt and ClinicalTrials.gov reached no committed
  file before it, because `build_run_report` is reachable only from inside
  `run_pipeline`. See "The refreshes record their own runs" in
  `pipeline/CLAUDE.md`.
- `tests/filters_test.ts` asserts against the _real committed rows_ (e.g. exact
  row counts for a trait filter). Regenerating data can legitimately break tests
  — check whether the data changed before "fixing" the filter. The trial
  populations are pinned in five more places, all of which move together:
  `tests/timeline_layout_test.ts` and `tests/scripts/test_timeline_figure.py`
  (sector drug counts, empty cells, the shared-cell pair and its ring
  thickness), `e2e/tests/trials-filters.spec.ts` (per-population counts),
  `e2e/tests/timeline.spec.ts` (the first marker drawn, and accessible names
  that use the population _label_ -- "Any SVD (including monogenic)", not "SVD")
  and the "CADASIL" search counts in `e2e/tests/readout.spec.ts` and
  `e2e/tests/trials-table.spec.ts`.
- `tests/data_contract_test.ts` asserts against the raw committed JSON, and is
  the guardrail that proved the R-to-Python export port faithful. Keep it.
- The export itself is covered by `tests/pipeline/export/` and runs in CI,
  against fixtures rather than a live database.
- **JSON output is byte-exact, and every exported file is gated on it.**
  `tests/pipeline/export/test_writer.py` parses each `data/*.json`, re-encodes
  it through the writer and compares the bytes; a companion test fails if a file
  appears in `data/` with no case, so the gate cannot quietly cover less than
  the directory. `data/cytobands_hg38.json` is the one exception, and is listed
  as such: it is not the export's output but `scripts/fetch_cytobands.py`'s, and
  `deno fmt --check .` is what gates its formatting. It needs no database and
  runs in milliseconds. This is what catches a formatting divergence before it
  turns the first regeneration into an unreviewable whole-file diff — the way
  the missing `</` → `<\/` escaping (jsonlite escapes a slash only after `<`;
  `json.dumps` never does) would have rewritten every row of `refs.json`.
  `json.loads()`-equality sees none of this.
- **Every export query needs an `ORDER BY`.** Without one PostgreSQL returns
  rows in whatever physical order it likes, so the byte-exact contract above
  cannot hold across runs. One unordered `SELECT * FROM clinical_trials`
  reordered `gene_info_table2.json` too, because `split_genetic_targets()` feeds
  the lookup request lists and `complete_lookup()` restores _request_ order.
  `_read_table` orders by `id`; the lookups inherit determinism from it.
  `_read_genes_with_lists` needs **two** -- `genes` by `id`, and each join table
  by `(gene_id, ordinal)`, because the within-gene order reaches the JSON array
  as well. `read_disease_annotations` orders by
  `(gene_symbol, group_key, source, object_id)`, and the `source` term is
  load-bearing beyond determinism: it puts ClinVar's rows before Orphadata's,
  which is what lets an exact Orphanet mapping fill only a column ClinVar left
  empty.
- **`data/table2.json` publishes only curated trial rows.** `--clinical-trials`
  writes ClinicalTrials.gov discoveries into the same table, with every curator
  column NULL; `_read_curated_trials` in `pipeline/export/main.py` skips any row
  with no `target_population` and logs the count, so a discovery no one has read
  cannot reach the dashboard as `(unknown)` mechanism, population and evidence —
  values no filter choice offers and the radar draws nowhere. See
  "ClinicalTrials.gov rows must read like the curated ones" in
  `pipeline/CLAUDE.md`.
- Facility coordinates come from ClinicalTrials.gov's `geoPoint`, which is
  computed as `GeoPoint(City, State, Country)` — city-level, not facility-level.
  `jitter_duplicate_coordinates()` fans out co-located sites. No geocoding
  service is involved. A site with no `geoPoint` is dropped rather than
  defaulted — a marker at (0, 0) is worse than no marker — and both that drop
  and a trial that ends up contributing no marker at all are logged by name: the
  trial is still in `nctIds`, and `tests/data_contract_test.ts` compares only
  that set, so nothing else would say the map is missing it.

### Gene lists

Three `genes` columns (references, GWAS traits, monogenic links) have been join
tables since migrations 005 and 006, one value per row, after a spreadsheet
round-trip destroyed a delimited PMID list once. `.claude/rules/gene-lists.md`
loads with `pipeline/export/main.py`, `pipeline/export/tables.py`,
`pipeline/database.py`, `pipeline/data_merger.py` and
`scripts/backfill_gene_lists.py`, and carries the three load-bearing invariants:
`_GENE_COLUMNS` holds the JSON key order, sentinels and untracked traits are
never stored, and `_append_gene_list_sql` carries the merge invariants the
delimited strings used to.

### The JSON contract

`pipeline/export/writer.py`'s `to_camel()` derives JSON keys from display column
names, which come from `clean_column_name()` in `pipeline/export/text.py`, so
the wire keys, the shapes in `lib/types.ts` and the labels in
`islands/*View.tsx` move together. Two invariants the TypeScript side depends
on: list-columns are always arrays, and the sentinel strings `"(none found)"`,
`"(unknown)"` and `"(none)"` are matched literally by the filter choices in
`lib/constants.ts`, so those values must stay byte-identical to what
`pipeline/export/tables.py` emits. `.claude/rules/json-contract.md` loads with
the export, `lib/types.ts`, `lib/data/`, `lib/constants.ts`, the two table
islands and the contract tests, and carries the rest: the omics vocabulary,
`relatedXrefs`, `sourceVersions`, and why each is shaped as it is.

### Pipeline internals

Extraction (the pinned model, the effort and prompt sweeps, provenance), the
Docling PDF fallback, what the measurements do and do not prove, and the known
data limitations live in `pipeline/CLAUDE.md`, which loads when working under
`pipeline/` — and, by import, under `tests/pipeline/`.

### Filtering

`lib/filters.ts` is the only place row selection happens; islands hold the
selection state and call it. It deliberately diverges from the Shiny original by
normalizing case and surrounding whitespace on both sides of every comparison —
the R version compared raw strings and silently hid rows (`"Proteomics"` vs
`"proteomics"`, traits stored as `"PSMD "`). The data boundary also trims
display strings, while the raw-JSON contract test keeps generator regressions
visible.

Two matching rules are non-obvious: a binary group (Yes/No) only constrains when
exactly one option is selected, and a `SHOW_ALL` selection means "no filter".
The same rules are enforced in the UI by `nextSelection()` in
`components/CheckboxFilter.tsx`, which can never leave a group matching nothing.

One group starts constrained: the trials pages' "Study status" seeds
`DEFAULT_TRIAL_STATUSES` (every status but Completed) through the `initial`
field on a `useCheckboxFilters` definition, and `reset()` returns to it rather
than to Show All. The `Active Filters:` line therefore reports it on first
paint, which is intended -- it is what tells a viewer completed trials are
hidden. `visibleTrials()` in `lib/filters.ts` is the same default as a function,
for the SSR tests and any count that has to agree with the page.

`components/FilterPanel.tsx` owns the sidebar-and-table layout both data pages
render, and the collapse state with it -- the `is-collapsed` class has to reach
the grid wrapper as well as the panel, so the panel cannot hold it alone.
Collapsing folds the sidebar to a rail rather than unmounting it: the toggle
stays one button in one place, so the press keeps focus and the pointer. Hiding
the controls hides no information, because `TableShell` prints the
`Active Filters:` line above the table either way -- keep that true if the
readout ever moves.

### Routes and islands

`routes/` is one server-rendered page per tab, listed in `TABS`
(`lib/constants.ts`) — adding a tab means adding both a route file and a `TABS`
entry with an `Icon` glyph. Every route renders into `components/Page.tsx`,
which owns the vertical rhythm through one gap token. The About page opts into a
fixed measure with `contained`; data tables, the map and the two figures use the
full width. Blocks inside the shell carry no outer margin. `Page` also takes an
optional `header`, which replaces the default `.page-header` block — the About
page passes its hero card, which carries the heading, the lead and the four
totals on one surface, so `title`/`description` go unused there.

Interactivity lives in the eight islands under `islands/`; theme state, events
and observers live in `lib/theme.ts` so no other island depends on
`ThemeToggle`. Both figures are drawn in-app from the committed JSON.

`routes/adapt.tsx` is the one page that is not a tab: it mounts
`islands/AdaptWizard.tsx`, which collects the `new-disease` interview and
downloads a generated `disease/`. Its logic is pure in `lib/adapt/` and its
title comes from `UNTABBED_TITLES` in `routes/_app.tsx`.

### Login

The dashboard sits behind one shared passphrase and a 30-day signed cookie, with
no store and no auth library. Three things hold everywhere: the gate in
`routes/_middleware.ts` fails closed and answers 503 on every gated route while
`DASHBOARD_PASSPHRASE` or `DASHBOARD_SESSION_SECRET` is unset, in development
too; `/logout` is a POST guarded by `csrf()`, so do not turn it into a GET link;
and the e2e suite runs pre-authenticated, so a spec about the logged-out state
opts out with an empty `storageState`, as `login.spec.ts` does.
`.claude/rules/login.md` loads with the auth routes, `lib/auth.ts`, `main.ts`
and `e2e/`, and carries the rest.

### Hosting

The app runs at `https://csvd-dashboard.fly.dev`, on Fly.io, as one always-on
machine built from the root `Dockerfile` and configured by `fly.toml`. CI's
`deploy` job ships it on a push to `main` once every other job has passed, then
checks it from outside with `scripts/smoke_deploy.sh`. It moved there on 2
November 2026 from Deno Deploy, which shuts down six months after Deno's 9
October 2026 announcement. The one-machine rule, the `Fly-Client-IP` header the
login limiter keys on, the allowlist `.dockerignore` and the 503 diagnosis are
in the `deploy` skill (`.claude/skills/deploy/SKILL.md`).

Because `data/*.json` is committed and bundled at build time, regenerating data
and pushing it to `main` republishes the site — `tests/filters_test.ts`'s caveat
about regeneration now has a production consequence as well as a test one.

### Scoped notes

Eight rules files load by path from `.claude/rules/`: `tables.md` (`TableShell`
and the two table islands), `timeline.md` (the trials radar), `phenogram.md`
(the karyogram), `tooltips.md`, `compression.md`, `gene-lists.md`,
`json-contract.md` and `login.md`. Four directories carry their own `CLAUDE.md`:
`islands/` (the map, the run widget and the adapt wizard, whose notes cover
`lib/adapt/` too), `assets/` (tokens, depth, typography), `e2e/` (npm isolation,
the production-build harness, the selector traps) and `tests/pipeline/`, which
imports `pipeline/CLAUDE.md`.

### Typography and type graph

The fixed figure-colour tokens and the no-mono-face decision are in
`assets/CLAUDE.md` under "Typography and figure colour"; the font faces are the
`@font-face` blocks in `assets/app.css` and the icons are `components/Icon.tsx`.

`@types/node` sits in `deno.json`'s import map because `deno check` cannot
resolve `@playwright/test` without it under `nodeModulesDir: "manual"`. It is
load-bearing — do not remove it — but it pulls Node's ambient globals into the
whole app's type graph, so `process` and `Buffer` type-check clean inside
browser-only islands and `ReturnType<typeof setTimeout>` resolves to
`NodeJS.Timeout` rather than `number` (see `components/Tooltip.tsx`, which
derives the ref type from `setTimeout` instead of naming either).
