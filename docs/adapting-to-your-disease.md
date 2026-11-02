# Adapting the dashboard to your disease

A guide for research groups who want this dashboard, its pipeline and its tests
to serve a disease of their own. It assumes you can open a terminal and follow
commands; it does not assume you can read TypeScript or Python.

The short version: everything that names the disease lives in one folder,
`disease/`. You fork the repository, fill that folder from a nine-question
interview, start from empty data, delete the tests that name our disease, prove
the checks pass, then run the pipeline and deploy. The repository ships with a
Claude Code skill, `/new-disease`, that walks the whole process with you; this
guide explains what it does and lets you do any step by hand.

---

## Contents

1. [How the codebase is split](#1-how-the-codebase-is-split)
2. [What you need before starting](#2-what-you-need-before-starting)
3. [Three ways to do this](#3-three-ways-to-do-this)
4. [Step 1: fork and clone](#4-step-1-fork-and-clone)
5. [Step 2: the nine-question interview](#5-step-2-the-nine-question-interview)
6. [Step 3: the files under disease/](#6-step-3-the-files-under-disease)
7. [Step 4: start from empty data](#7-step-4-start-from-empty-data)
8. [Step 5: remove the tests that name our disease](#8-step-5-remove-the-tests-that-name-our-disease)
9. [Step 6: the handful of edits outside disease/](#9-step-6-the-handful-of-edits-outside-disease)
10. [Step 7: run the checks](#10-step-7-run-the-checks)
11. [Step 8: measure cost and recall](#11-step-8-measure-cost-and-recall)
12. [Step 9: secrets, database and first commit](#12-step-9-secrets-database-and-first-commit)
13. [Step 10: the first live run](#13-step-10-the-first-live-run)
14. [Step 11: deploy](#14-step-11-deploy)
15. [Keeping up with upstream](#15-keeping-up-with-upstream)
16. [Troubleshooting](#16-troubleshooting)
17. [Checklist](#17-checklist)

---

## 1. How the codebase is split

The repository has two halves that never share a process:

```text
PubMed / ClinicalTrials.gov ──> pipeline/ (Python) ──> PostgreSQL ──> data/*.json ──> web app (Deno)
```

The pipeline searches PubMed for genetics papers about the disease, asks Claude
to extract putative causal genes from each one, validates the symbols against
NCBI, merges the result into a database and exports it as JSON files. The web
app reads only those JSON files. Nothing about the disease is hard-coded in
either half; both read it from `disease/`.

| Where                            | What it holds                                                          | Do you edit it?            |
| -------------------------------- | ---------------------------------------------------------------------- | -------------------------- |
| `disease/`                       | Every word that names the disease: prose, search terms, traits, prompt | **Yes, all of it**         |
| `data/`                          | The exported JSON the site displays                                    | No; the pipeline writes it |
| `tests/csvd/` and three siblings | Tests that pin our disease's numbers                                   | You delete them            |
| Everything else                  | Code that works for any disease                                        | Almost never               |

Because of this split, the same code can serve your disease from your
`disease/`, and you can keep pulling our improvements later (section 15) without
touching your content.

**One rule that matters for security.** `disease/manifest.json` is shipped to
every browser _before login_, so it must never contain a gene symbol. Gene
symbols and search terms go in `disease/pipeline.json`, which only the pipeline
reads. A test enforces this.

---

## 2. What you need before starting

### Tools

| Tool                     | Needed for                   | Version |
| ------------------------ | ---------------------------- | ------- |
| Git and a GitHub account | forking and pushing          | any     |
| Deno                     | the web app and its checks   | 2.9+    |
| uv                       | the Python pipeline          | 0.12+   |
| Node.js + npm            | the browser end-to-end tests | 26+     |
| PostgreSQL               | the pipeline's database      | 18.6    |
| libwebp (`cwebp`)        | the README's screenshots     | 1.0+    |
| Claude Code              | the guided path (optional)   | current |

Install links: [deno.com](https://deno.com/),
[docs.astral.sh/uv](https://docs.astral.sh/uv/),
[nodejs.org](https://nodejs.org/),
[postgresql.org](https://www.postgresql.org/). On a Mac, PostgreSQL through
Homebrew (`brew install postgresql@18`) is the simplest route; it is keg-only,
so section 12 starts it and puts its commands on your `PATH`. The
`regenerate-data` skill documents a container alternative. libwebp is
`brew install webp` on a Mac, or the `webp` package on Debian and Ubuntu.

### Accounts and keys

| Key                                                | Free? | Used for                                         |
| -------------------------------------------------- | ----- | ------------------------------------------------ |
| `ANTHROPIC_API_KEY`                                | No    | Gene extraction from papers (the only paid step) |
| `ENTREZ_EMAIL`                                     | Yes   | PubMed search, gene validation, ClinVar          |
| `NCBI_API_KEY` (optional)                          | Yes   | Ten NCBI requests a second rather than three     |
| `UNPAYWALL_EMAIL`                                  | Yes   | Finding open-access PDFs                         |
| `DASHBOARD_PASSPHRASE`, `DASHBOARD_SESSION_SECRET` | n/a   | The site's login gate                            |

### Expertise

The interview needs a researcher who knows the disease: which phenotypes matter,
which genes are Mendelian, which papers are landmark. That person does not need
to code, but they need to be in the room.

### Budget

Extraction costs roughly **0.09 USD per paper**. Our query retrieves about 800
papers a year, so a yearly run is about 70 USD. Section 11 shows you how to
measure your own figure before spending anything.

---

## 3. Three ways to do this

**Guided (recommended).** Open the fork in
[Claude Code](https://claude.com/claude-code) and type `/new-disease`. The
assistant asks the nine questions, runs a lookup before each so you decide over
real counts rather than guesses, writes every file, validates it as it lands,
deletes the old tests, runs the checks, measures cost and recall, and ends with
a hand-off message listing everything it did and anything it could not.

**Through the dashboard.** The dashboard has an "Adapt this dashboard to another
disease" page at `/adapt`, linked from its About page. The site this repository
runs belongs to its lab and sits behind their login, so run your own copy: after
step 1, copy `.env.example` to `.env`, set `DASHBOARD_PASSPHRASE` (a sentence,
in single quotes) and `DASHBOARD_SESSION_SECRET` (`openssl rand -base64 32`),
then

```bash
deno task dev    # then open http://localhost:5173/adapt and sign in
```

Without `.env` every page answers 503: the login fails closed rather than open.
The page asks the nine questions below as a form and runs the lookups a browser
can: NCBI (PubMed counts, MeSH, Gene, ClinVar, paper titles), the Ontology
Lookup Service and ClinicalTrials.gov. Q7's Orphadata cross-check is not among
them; do it by hand or with the skill. It checks every answer and downloads
`disease-adaptation.zip`, which holds the nine files of `disease/`, your logos
under `static/institute/`, `ADAPT-CHECKLIST.md` (the steps that follow, written
out for your disease) and `adapt-answers.json`. "Export answers" and "Import
answers" save and restore the answers alone, so a draft can move to another
browser or to a colleague; neither file carries your NCBI key or email. The
checklist's step 2 unpacks the archive: it removes our logos, unzips `disease/`
and `static/` over the clone and deletes our recall baseline. Use the page when
nobody on the team wants to write JSON; steps 1 and 4 onwards are still yours.

**Manual.** Follow this guide and the two files the skill itself reads:
`.claude/skills/new-disease/interview.md` (the questions, with the exact web
lookups) and `.claude/skills/new-disease/checklist.md` (the flat tick list).
Every file you write is validated by a JSON Schema or a test, so mistakes are
caught by name.

Whichever you choose, the steps are the same and this guide describes them.

---

## 4. Step 1: fork and clone

**Purpose:** get your own copy that can receive our future updates.

1. On GitHub, fork `mathieubpoiriericm/csvd-dashboard` under your account or
   organisation. Name it `<key>-dashboard`, where `<key>` is the one-word
   disease key you will choose in the interview (for example `als-dashboard`). A
   key may hold underscores, and a repository or Fly.io app name becomes part of
   a host name, so there each `_` is written `-`: the key `example_itis` makes
   `example-itis-dashboard`. Database names keep the key as it is
   (`example_itis_dashboard`). Wherever this guide writes `<key>-dashboard`,
   read it with that change.
2. Clone it and register the original as `upstream`:

```bash
git clone https://github.com/<you>/<key>-dashboard.git
cd <key>-dashboard
git remote add upstream https://github.com/mathieubpoiriericm/csvd-dashboard.git
```

3. Install the three dependency sets once:

```bash
deno install                          # web app
uv sync --group dev --group figure    # pipeline, plus the print figures
deno task e2e:install                 # browser tests, and the Chromium they drive
```

`npm ci --prefix e2e` alone installs Playwright but no browser, and every
browser test then fails with "Executable doesn't exist". On Linux, Chromium also
needs system libraries, which this adds:

```bash
npx --prefix e2e playwright install --with-deps chromium
```

Before you change anything, read `disease/` as it stands. It is the worked
example for every file you are about to write, and the shapes it shows are the
ones the schemas enforce.

---

## 5. Step 2: the nine-question interview

Each question feeds specific files; the order matters because later answers
depend on earlier ones. For every question, the guided path runs a web lookup
first and shows you the result. Doing it by hand, the lookups are `curl`
commands listed in `interview.md`; a browser works just as well.

### Q1. Name, key, institute, maintainer

**Purpose:** the words the site prints, and the identifier everything else is
keyed on.

You supply: the disease's full name as it reads mid-sentence, a short form, an
abbreviation, an adjective form for headings, and a one-word lowercase key; your
institute's name, short name, URL and copyright line; the maintainer's name and
email.

The key must match `^[a-z][a-z0-9_]*$`. It becomes the database name
(`<key>_dashboard`), the deploy project name (with each `_` written `-`, as in
step 1), and the `disease` column on every row the pipeline extracts, so choose
it once.

Ours, for comparison:

```json
"disease": {
  "key": "csvd",
  "name": "cerebral small vessel disease",
  "short": "SVD",
  "abbreviation": "cSVD",
  "adjective": "Cerebral SVD"
}
```

Yours might read:

```json
"disease": {
  "key": "als",
  "name": "amyotrophic lateral sclerosis",
  "short": "ALS",
  "abbreviation": "ALS",
  "adjective": "ALS"
}
```

The page titles and ledes are then drafted by substituting the name into our
sentences, and you edit the result. Two logo files go under `static/institute/`,
one for a light background and one for a dark one; or set `srcOnDark` to `null`
and use one. A single file is drawn on the dark navigation bar and on the light
login card alike, so it has to read on both.

### Q2. Disease phrases and MeSH headings

**Purpose:** the anchor of the PubMed query. Papers are found either because
they write the disease's name in the title or abstract, or because PubMed
indexed them under a MeSH heading.

You supply: one to three phrases papers actually write. The headings come from
them: each phrase is resolved to the heading PubMed's own translation maps it
to, and a phrase MeSH has no heading for adds none.

Lookup: resolve each phrase to its heading, read the scope note back to confirm
it is your disease, and count what the heading retrieves all-time. A heading in
the hundreds of thousands (`Stroke`, `Dementia`) is a parent, not a disease;
reword the phrase towards a child.

A heading that is not one of your phrases (ours adds `White Matter` beside
`Cerebral Small Vessel Diseases`) is added by hand after the files are written:
append it to `search.pubmed.meshTerms` in `disease/pipeline.json`, record its
count in that block's `$comment`, and measure recall again (step 8). The page
has no field for one.

```text
https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=mesh&term=amyotrophic+lateral+sclerosis&retmode=json
https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term="Amyotrophic+Lateral+Sclerosis"[MeSH]&rettype=count&retmode=json
```

### Q3. Marker terms

**Purpose:** a second way in, for papers that write one of your disease phrases
but none of the genetic terms (Q4). The query keeps a paper that holds a disease
phrase and one of these terms; every phrase anchors them, not only the first,
and a marker never reaches a paper that names no disease phrase. Each term adds
papers, so a long or broad list is what inflates the cost estimate.

You supply: five to ten terms. Ours are `stroke`, `dementia`, `lacunes`,
`lacunar stroke`, `white matter hyperintensities`, `perivascular spaces`,
`cerebral microbleeds`.

Lookup: a count per term, anchored on every disease phrase, so the number means
"papers holding a phrase and this term".

### Q4. Confirm the genetic terms

**Purpose:** the ten terms that identify a paper as _genetics_ (GWAS, Mendelian
randomisation, TWAS, exome, and so on) live in code, not in `disease/`, because
they are about genetics rather than any disease. You confirm they fit your
field. Wanting one added is a code change with a test, and belongs upstream as a
pull request.

### Q5. Trait vocabulary

**Purpose:** the phenotypes the dashboard filters on and the karyogram pins. The
extraction prompt only lets the model label a gene with one of these, so the
list is a contract between your field's vocabulary and the site's filters.

You supply: four to sixteen traits. Each has a key (the value the model writes
and the data stores), a label (the short text on the karyogram's pills and the
filter choices, 20 characters or fewer), a long name, a one-sentence definition,
and a family (two to five traits sharing a colour). If your field has a
phenotype standard (as STRIVE-2 is for us), cite it; definitions quoted from it
are marked `standard: true`.

Lookup: an ontology cross-reference per trait from the EBI Ontology Lookup
Service. A miss is fine; record what was searched.

```json
{
  "key": "PSMD",
  "label": "PSMD",
  "family": "diffusion",
  "name": "Peak width of skeletonized mean diffusivity",
  "standard": true,
  "definition": "…",
  "xref": null,
  "xrefNote": "no EFO or HP term for this measure"
}
```

Keys may not contain a comma, because the prompt lists them comma-separated.

### Q6. Trial populations, ClinicalTrials.gov terms, first mechanism

**Purpose:** the trials pages. Three things:

- **Populations**: the one to six groups a trial is filed under, plus the column
  label the table shows (ours is "SVD Population"). One population is drawn as
  the radar's whole circle.
- **Search terms and condition filters**: what to search ClinicalTrials.gov for,
  and the substrings a trial's stated condition must contain to be kept. The
  registry expands a query through its own concept graph, so a small-vessel
  search returns vasculitis trials; the substrings are what filter them out.
- **At least one mechanism of action** already being tested, with a family for
  it. The trials timeline cannot start with an empty family list.

Lookup: a count per search term, and the conditions and interventions the first
page of results names, so you can choose substrings that keep the right trials.

A mechanism is your wording, such as
`Cholinesterase inhibition (acetylcholinesterase inhibitor)`, never a drug name
copied as-is. A family keyed `uncharacterised` is the one the radar flags: file
there the mechanisms a trial states too thinly to classify.

### Q7. Monogenic genes

**Purpose:** the Mendelian genes get special treatment in the prompt (their
known status must not inflate new common-variant evidence) and their OMIM
entries feed the tooltips.

You supply: zero to ten HGNC symbols, any alias names, and per gene the OMIM
phenotype entries to show. An alias is the name the dashboard files genes under
_instead of_ their HGNC symbols: every extraction of the members is stored, and
published, under the alias. Use one for a group the literature treats as one
(our `COL4A1/2` stands for two genes) or for a symbol NCBI has retired that the
literature still writes (our `C6orf195`, now `LINC01600`), never for a synonym
of a gene you want shown by its own symbol.

Lookup: NCBI Gene for each symbol's official spelling, searched among current
records only (a replaced record answers with its old name, which is not the
official symbol). ClinVar for the diseases each gene carries: pathogenic records
that describe the gene itself (it is their one gene, or one of at most five with
no copy-number change and variants no wider than 1 kb), and only traits carrying
an OMIM, MONDO or Orphanet identifier. Orphadata for the associations, which the
page does not query: do it by hand or with the skill. You then confirm each row
on omim.org and copy the location, inheritance, mapping key and gene MIM number
from the entry page. The pipeline has no OMIM licence; this CSV has always been
hand-copied.

With no monogenic gene, the list is `[]` and the CSV is its header line only.

### Q8. Papers for the prompt's examples

**Purpose:** the extraction prompt ends with worked examples that teach the
model what a finding for _this_ disease reads like. Ours has fifteen.

You supply: two to four PMIDs, and for each the gene, the trait keys from Q5,
the sentence in the paper that states the finding, and how confident you are
that the gene is causal (0 to 1).

**The one rule that never bends:** every example cites a PMID you supplied and
quotes a sentence you supplied or that was copied verbatim from the fetched
abstract. Nobody, human or assistant, composes a paper, a finding or a quote,
and nobody adapts one of our cSVD examples by swapping names. An invented
example teaches the model to invent. With fewer than two papers the examples
section is the single line `<!-- examples: none yet -->`, and the site works
fine until you add them.

Lookup: fetch each abstract to confirm the PMID exists and check the quote.

```text
https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id=<pmid>&rettype=abstract&retmode=text
```

### Q9. Gold PMIDs for recall

**Purpose:** a measurable definition of "the query works". These are the ten to
thirty genetics papers on your disease you would be alarmed to see a run miss.
Section 11 measures how many the query retrieves.

The Q8 papers count. Each row carries a short note saying why it is gold.

```csv
pmid,note
15905468,landmark GWAS
19539236,table1 reference
```

---

## 6. Step 3: the files under disease/

Written from the interview in this order, because each is validated as it lands
and the later ones reference the earlier ones. After each file, two cheap
commands name the key that is wrong:

```bash
deno task check
uv run pytest tests/pipeline/test_disease.py
```

| File              | From           | Purpose                                              | Notes                                                                        |
| ----------------- | -------------- | ---------------------------------------------------- | ---------------------------------------------------------------------------- |
| `manifest.json`   | Q1, Q5, Q6     | Everything the web app prints                        | No gene symbols. `hosting.url` and the four `about.*` texts start `null`.    |
| `pipeline.json`   | Q2, Q3, Q6, Q7 | Search terms, gene lists                             | Rewrite the `$comment` strings; ours quote cSVD measurements.                |
| `vocabulary.json` | Q5             | The traits                                           | `synonyms` and `untracked` start `[]`.                                       |
| `timeline.json`   | Q6             | Trial populations, mechanisms, families with colours | Populations in the manifest's order; at least one family with one mechanism. |
| `phenogram.json`  | Q5             | One colour pair per trait family                     | Reuse our seven pairs in order; a new pair must pass the contrast test.      |
| `omim_info.csv`   | Q7             | OMIM tooltip rows                                    | Nine columns; header line alone if none.                                     |
| `prompt.md`       | Q5, Q7, Q8     | The disease half of the extraction prompt            | Same `## section` ids as ours, same order. Do not wrap lines.                |
| `recall_gold.csv` | Q9             | The gold papers                                      | **Delete `recall_baseline.json`**; you will write your own in section 11.    |
| `README.md`       | all            | The human-readable disease page                      | Then `deno fmt disease/ static/institute/`.                                  |

Three cross-file rules the tests pin:

- `## strategy.monogenic_genes` in `prompt.md` is exactly `monogenicGenes` from
  `pipeline.json` joined by `,`.
- `## traits.canonical` in `prompt.md` names every trait key in
  `vocabulary.json` and nothing else.
- The `populations` in `timeline.json` equal the manifest's, in the same order.

An example of `pipeline.json`, trimmed:

```json
{
  "schemaVersion": 1,
  "search": {
    "pubmed": {
      "diseaseTerms": ["amyotrophic lateral sclerosis"],
      "markerTerms": ["motor neuron", "…"],
      "meshTerms": ["Amyotrophic Lateral Sclerosis"]
    },
    "clinicalTrials": {
      "searchTerms": ["amyotrophic lateral sclerosis", "motor neuron disease"],
      "conditions": ["amyotrophic lateral", "motor neuron", "motor neurone"],
      "conditionPairs": []
    }
  },
  "monogenicGenes": ["SOD1", "C9orf72", "TARDBP", "FUS"],
  "geneAliases": {},
  "pipeline": { "runLabel": "ALS Pipeline", "maxGenesPerPaper": 20 }
}
```

The marker terms above are placeholders; yours come from Q3 and its counts.

---

## 7. Step 4: start from empty data

**Purpose:** the committed `data/*.json` files are our disease's genes and
trials. Your site must build and every check must pass with no data at all,
before you spend anything on a run.

```bash
deno task data:empty
```

This writes every row file as `[]`, both run files as `null`, the map with no
sites, `omim_info.json` from your CSV, and leaves the genome reference file
(cytobands) untouched. It needs no database. Our CI runs the whole test suite in
exactly this state, so it is a supported configuration, not a degraded one.

---

## 8. Step 5: remove the tests that name our disease

**Purpose:** four test folders pin cSVD content: gene names, row counts, recall
figures, the first marker the radar draws. They would fail on your data by
design. Everything outside them holds for any disease.

```bash
git rm -r tests/csvd e2e/tests/csvd tests/pipeline/csvd tests/scripts/csvd
git rm -r tests/pipeline/cassettes/test_query_recall_gold
```

The second line removes our recall measurement's recorded network responses.
They sit beside the generic replay test rather than in a `csvd/` tree, and step
8 records yours: one of ours left in place would be replayed instead of
recorded, and the recall test would fail against it.

---

## 9. Step 6: the handful of edits outside disease/

**Purpose:** a few places carry cSVD content outside `disease/`, each for a
reason. They are judgement calls you make once.

| File                                                                                 | What to do                                                                                                                                                                                                       |
| ------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/pipeline/test_prompt_vocabulary.py`                                           | Set `_ADMITTED_UNASKED` to `{}`. It explains why two of our trait keys are absent from our prompt's canonical sentence; yours names every key, so there is nothing to explain.                                   |
| `tests/no_disease_literals_test.ts` and `tests/pipeline/test_no_disease_literals.py` | Only if they fail. They scan the code for **your** disease's terms; add an allow-list entry only for a hit that is about something else.                                                                         |
| `server/protected_data_build.ts`                                                     | `PROTECTED_DATA_SENTINELS` lists strings that must never appear in a public bundle (leak canaries). Passes on empty data. After your first real export, replace the seven strings with ones unique to your data. |
| `e2e/package.json`                                                                   | The `name` and `description` fields.                                                                                                                                                                             |
| `docs/screenshots/`                                                                  | The README's six screenshots, of our dashboard. Like the sentinels, they wait for your first real export; then `deno task screenshots` recaptures all six from your build (section 13).                          |
| `static/institute/logo-light.svg`, `logo-dark.svg`                                   | Your logos, or one file with `srcOnDark: null`, which must read on the dark navbar and the light login card.                                                                                                     |
| `tests/pipeline/conftest.py`, `.gitignore`                                           | Cosmetic container names and four inert ignore lines. Leave or rename.                                                                                                                                           |

---

## 10. Step 7: run the checks

**Purpose:** prove the repository is consistent before the first run. These are
the same commands our CI runs on the empty-data configuration. All must pass.

```bash
deno task check                 # format, lint, type-check
deno task test:coverage         # web app tests, 100 % coverage under lib/
uv run ruff check .             # Python lint
uv run ty check                 # Python types
uv run pytest --cov=pipeline --cov=pipeline/alembic --cov-branch --cov-report=term-missing:skip-covered
uv run pytest tests/scripts     # the figure scripts, collected separately
npx --prefix e2e playwright test -c e2e/playwright.config.ts   # browser tests
```

A failure names the file and the key. The most common ones:

- A `manifest.json` key the schema does not know: a typo, or a key that belongs
  in `pipeline.json`.
- `## traits.canonical` and `vocabulary.json` disagree.
- `disease/README.md` or an uploaded logo not formatted: run
  `deno fmt disease/ static/institute/`.
- A disease-literal hit (`tests/no_disease_literals_test.ts` or
  `tests/pipeline/test_no_disease_literals.py`): a line holding one of your
  terms as a whole word. See Troubleshooting.

---

## 11. Step 8: measure cost and recall

**Purpose:** two numbers that tell you whether the query is right, before it
costs anything. Neither needs a database.

### Cost

```bash
uv run python -m pipeline.main --test-mode --days-back 365
```

Test mode runs the search and retrieval only, with no extraction and no
database. It prints the number of papers the query retrieves in a year. Multiply
by 0.09 USD.

| Papers a year | Estimate  | Action                   |
| ------------- | --------- | ------------------------ |
| ~800 (ours)   | ~70 USD   | Fine                     |
| above ~2,000  | 180 USD + | Narrow the query (below) |

Each marker term adds every paper that uses it beside a disease phrase, and a
shorter phrase matches more. So narrowing means going back to Q3 and removing
the broadest marker terms or making them more specific, never shortening them. A
MeSH heading from Q2 that is a broad parent inflates the count as well; replace
it with a narrower one.

### Recall

```bash
uv run python -m scripts.measure_recall                    # prints matched and missed, with titles
uv run python -m scripts.measure_recall --write-baseline   # records disease/recall_baseline.json
uv run pytest tests/pipeline/test_query_recall_gold.py --record-mode=once
```

The first prints which gold papers the query retrieves and which it misses. A
missed paper is a question for the researcher (is it really about the disease,
or is the query too narrow?), never a reason to edit the gold list. The second
records the figure; the third records the network responses so CI can replay the
measurement offline. Commit the baseline and the recorded cassette.

The measurement's gold set also takes in every PMID `data/table1.json` cites.
That file is empty until the first live run, whose export adds references and so
moves the denominator recorded here; section 13 measures again after it.

Our baseline, for scale: 106 of 111 gold papers matched.

---

## 12. Step 9: secrets, database and first commit

**Purpose:** the values only your machine knows, and the database the pipeline
writes to.

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable                   | Value                                                       |
| -------------------------- | ----------------------------------------------------------- |
| `DASHBOARD_PASSPHRASE`     | A sentence, in single quotes: `'…'`. It is the shared login |
| `DASHBOARD_SESSION_SECRET` | Output of `openssl rand -base64 32`                         |
| `DB_NAME`                  | `<key>_dashboard`                                           |
| `DB_USER`, `DB_PASSWORD`   | `<key>_user`, and the password you give that role below     |
| `ANTHROPIC_API_KEY`        | From console.anthropic.com                                  |
| `ENTREZ_EMAIL`             | Your email                                                  |
| `NCBI_API_KEY`             | Optional: leave it commented out unless you have one        |
| `UNPAYWALL_EMAIL`          | Your own email; Unpaywall refuses a placeholder address     |

Wrap the passphrase in single quotes and keep apostrophes out of it. The two
serving tasks, `deno task dev` and `deno task start`, read `.env` with
`--env-file`, which ends an unquoted value at a `#` and expands a `$NAME`, and
the login then refuses the sentence you meant. `fly secrets set` takes the same
sentence in the same quotes, because a shell reads it the same way (section 14).

`NCBI_API_KEY` is commented out in `.env.example`. Uncomment it only to set a
real key from your NCBI account: NCBI answers any other value, an empty one
included, with HTTP 400 on every call, which the pipeline can report only as
"Bad Request". Without a key NCBI allows three requests a second, and the
pipeline paces itself to match.

`.env` is git-ignored and must stay so. With either login variable missing the
site answers 503 on every page rather than opening; that is deliberate.

Create the database role and the database. `createuser -P` asks for the role's
password twice: type your `DB_PASSWORD`. Homebrew's `postgresql@18` is keg-only,
so on a Mac start it and put its commands on your `PATH` first:

```bash
brew services start postgresql@18
export PATH="$(brew --prefix postgresql@18)/bin:$PATH"
createuser -s -P <key>_user
createdb -O <key>_user <key>_dashboard
```

On Linux, run the last two as the `postgres` user instead:

```bash
sudo -u postgres createuser -s -P <key>_user
sudo -u postgres createdb -O <key>_user <key>_dashboard
```

A role without a password works on Homebrew's server, which trusts every
connection from your own machine, and nowhere else: the pipeline and alembic log
in over TCP with `DB_PASSWORD`, and a server using `scram-sha-256` (the default
for Linux packages and the official container image) refuses them with "password
authentication failed".

Then apply the schema:

```bash
cd pipeline && uv run alembic upgrade head && cd ..
```

Then the first commit:

```bash
git add -A
git commit -m "Set the repository up for <disease name>"
git push
```

You can already run the site locally, with empty tables, to see your prose:

```bash
deno task dev    # http://localhost:5173
```

---

## 13. Step 10: the first live run

**Purpose:** fill the database and the JSON files with your disease's genes and
trials.

A run in three parts, in this order:

```bash
# 1. Genes: search PubMed over a window, extract, validate, merge, export.
uv run python -m pipeline.main --days-back 365 --batch --export

# 2. Trials: discover registered trials for your condition terms.
uv run python -m pipeline.main --clinical-trials --export
deno task geocode

# 3. Reference data: gene descriptions, UniProt, citations, ClinVar and Orphanet.
uv run python -m pipeline.main --sync-external-data --sync-annotations --export
```

What each does and why:

- `--batch` submits every paper in one request at half price; results arrive
  within about an hour. A crash while waiting loses the submission, so let it
  finish. Without `--batch`, a long run is resumable from a checkpoint file.
- `--export` regenerates `data/*.json` at the end. It changes files but commits
  nothing, because the curated gene table is a scientific judgement and you
  should review the diff.
- `deno task geocode` fetches facility coordinates for the map. It is separate
  because it calls ClinicalTrials.gov, and a test fails on the map's trial set
  if it is skipped.
- Discovered trials are not published until a curator fills their population,
  mechanism and evidence columns in the database. A trial with no population is
  skipped by the export and counted in the log, so an unread discovery never
  reaches the site as "(unknown)". The `sync-clinical-trials` skill describes
  the curation columns.

After the export, review `git diff --stat data/` and measure recall again. The
gold set counts every PMID `data/table1.json` cites, so the references this
export added changed the denominator section 11 recorded, and four recall tests
fail until the baseline and its recording are made afresh:

```bash
uv run python -m scripts.measure_recall --write-baseline
rm -r tests/pipeline/cassettes/test_query_recall_gold
uv run pytest tests/pipeline/test_query_recall_gold.py --record-mode=once
```

Then fill the seven `PROTECTED_DATA_SENTINELS` (section 9), and recapture the
README's screenshots, which still show our disease:

```bash
deno task screenshots
```

Run the checks again, and commit.

A weekly cron of the first command with the default seven-day window is how the
site stays current. Any run whose export adds a reference moves the gold set the
same way, and the same three commands follow it.

---

## 14. Step 11: deploy

**Purpose:** put the site on the web behind its passphrase.

The site runs on [Fly.io](https://fly.io) as one always-on machine, built from
the repository's `Dockerfile` and deployed by CI from your `main` branch once
every check has passed. It costs about $3.69 a month. Four values are needed:

| Setting                    | Value                                                |
| -------------------------- | ---------------------------------------------------- |
| `app` in `fly.toml`        | `<key>-dashboard`, each `_` in the key as `-`        |
| `primary_region`           | The Fly region nearest your readers                  |
| `DASHBOARD_PASSPHRASE`     | The same sentence as your `.env`, in the same quotes |
| `DASHBOARD_SESSION_SECRET` | The same value as your `.env`                        |

The app name becomes the address `<key>-dashboard.fly.dev` and has to be free on
Fly. Set the first two in `fly.toml` and commit, then:

```bash
brew install flyctl && fly auth login
fly launch --no-deploy --copy-config
fly secrets set DASHBOARD_PASSPHRASE='…' DASHBOARD_SESSION_SECRET='…'
fly tokens create deploy
```

Store the token as the repository secret `FLY_API_TOKEN` on GitHub; from then on
every push to `main` that passes CI deploys, and is checked from outside by
`scripts/smoke_deploy.sh`. Without the token, CI skips the deploy and says so.
The setup, the one-machine rule and the diagnosis for a site that answers 503 on
every page are in `.claude/skills/deploy/SKILL.md` and in the README's
"Deployment" section. Once the site answers, write its URL into `hosting.url` in
`disease/manifest.json` and into `disease/README.md`, and commit.

Because the JSON is bundled at build time, every push of regenerated data to
`main` republishes the site.

---

## 15. Keeping up with upstream

**Purpose:** we keep improving the code, the tests and the pipeline. Your fork
can take those changes without touching your content, because the disease seam
means the two never overlap.

```bash
git fetch upstream
git merge upstream/main
```

Then one resolution per location, and only these:

| Location                    | Resolution                                                    |
| --------------------------- | ------------------------------------------------------------- |
| `data/`                     | Keep yours                                                    |
| `disease/`                  | Keep yours; then diff the two schemas for any new key         |
| The four `csvd/` test trees | Keep them deleted                                             |
| `README.md` first line      | Keep yours                                                    |
| `fly.toml`                  | Keep your `app` and `primary_region`; take the rest           |
| `docs/screenshots/`         | Recapture with `deno task screenshots`, once `data/` is yours |

A conflict anywhere else is a bug in the seam; please report it upstream rather
than resolving it locally. Run the checks from section 10 afterwards. The Claude
Code skill `/update-from-upstream` performs exactly this sequence.

If you fix or improve the shared code, a pull request upstream benefits every
fork.

---

## 16. Troubleshooting

**Every page answers 503.** One of the two login variables is unset. Check
`.env` locally, or the Deploy project's secrets.

**Login succeeds and the form reappears.** You opened the site on
`http://0.0.0.0:8000` rather than `http://127.0.0.1:8000`. The session cookie is
only sent over plain HTTP on localhost.

**A check says a manifest key is not allowed.** Every key in both manifests is
listed in `manifest.schema.json` and `pipeline.schema.json`. Compare your file
against ours.

**A disease-literal test fails.** Both scans report every line outside
`disease/` holding one of your terms as a whole word: the name and the
institute's name in any case, the abbreviation, the short form, the institute's
short name and the gene symbols as written. A line that is about your disease is
reworded. Often it is not: a unit, an identifier or an unrelated code (OMIM
writes autosomal dominant as `AD`). Then add an entry with the reason, to
`ALLOWED` in the TypeScript test:

```ts
[
  /^components\/adapt\/steps\/MonogenicStep\.tsx$/,
  /Inheritance \(AD, AR, XL…\)/,
  "OMIM's inheritance codes, not a disease",
],
```

or to `_ALLOWED` in the Python one, keyed by the file and the term as written:

```python
("pipeline/export/omim.py", "AD"): "OMIM's inheritance code, not a disease",
```

A one- or two-letter short form matches ordinary words and codes, so expect a
few.

**The cost estimate is huge.** Each marker term adds papers: fewer, more
specific marker terms cut the cost, and so does a child MeSH heading rather than
a parent.

**A gold paper is missed.** Read its title in the output. Either it is not
really a genetics paper on your disease, or the query needs a phrase it uses.
Never delete it from the gold list to make the number go up.

**A paper fails to retrieve.** The `debug-paper-retrieval` skill lists the
Europe PMC and PDF traps and what each looks like in the run log.

---

## 17. Checklist

```text
[ ] Forked as <key>-dashboard, cloned, upstream remote added
[ ] deno install, uv sync, deno task e2e:install
[ ] Interview answered (Q1 to Q9)
[ ] disease/manifest.json          (no gene symbols)
[ ] disease/pipeline.json          ($comments rewritten)
[ ] disease/vocabulary.json
[ ] disease/timeline.json          (>= 1 family, populations in manifest order)
[ ] disease/phenogram.json
[ ] disease/omim_info.csv
[ ] disease/prompt.md              (examples cite your PMIDs, or "none yet")
[ ] disease/recall_gold.csv        (recall_baseline.json deleted)
[ ] disease/README.md              (deno fmt)
[ ] deno task data:empty
[ ] git rm -r tests/csvd e2e/tests/csvd tests/pipeline/csvd tests/scripts/csvd
[ ] git rm -r tests/pipeline/cassettes/test_query_recall_gold
[ ] _ADMITTED_UNASKED = {}, e2e/package.json, logos
[ ] All seven checks green
[ ] Cost estimate printed and acceptable
[ ] Recall measured, baseline and cassette committed
[ ] .env filled (passphrase single-quoted, NCBI_API_KEY only if real)
[ ] Role created with createuser -P, database created, alembic upgrade head
[ ] First commit pushed
[ ] First live run, recall measured again, sentinels replaced, data committed
[ ] Screenshots recaptured with deno task screenshots, committed
[ ] Deployed; hosting.url written and committed
```
