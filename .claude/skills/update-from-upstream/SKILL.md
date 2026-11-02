---
name: update-from-upstream
description: Use when pulling upstream changes into a disease fork - the merge, the one resolution per location (data/, disease/, the csvd/ trees, the README's first line, fly.toml's app and region, docs/screenshots/), the schema diff for new manifest keys, and the gates afterwards.
---

# Updating a disease fork from upstream

The fork was set up by the `new-disease` skill, with the upstream repository
as the `upstream` remote. Upstream keeps shipping code, tests and its own
cSVD data and screenshots; the fork keeps its own `disease/`, its own `data/`,
its own `docs/screenshots/` and none of the `csvd/` test trees. Everything
outside those locations merges clean by construction — that is what the
disease seam is for — so a conflict anywhere else is a bug to report upstream,
not something to resolve locally.

## The merge

```bash
git fetch upstream
OLD_UPSTREAM=$(git merge-base HEAD upstream/main)   # keep this for the schema diff below
git merge upstream/main
```

Then one resolution per location, in this order.

## One resolution per location

| Location                                                                   | Resolution                                                                                                                                                                                                                                   |
| -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `data/*.json`                                                              | **Ours.** `git checkout --ours -- data && git add data`. Then regenerate: `deno task data && deno task geocode` against the fork's database, or `deno task data:empty` if the fork has not run the pipeline yet. Never keep upstream's rows: they are cSVD. |
| `disease/*`                                                                | **Ours**, then the schema diff below for keys upstream added.                                                                                                                                                                                |
| `tests/csvd/`, `e2e/tests/csvd/`, `tests/pipeline/csvd/`, `tests/scripts/csvd/` | Upstream's new files there arrive as "deleted by us" (or as plain additions when the merge is clean). Remove them again: `git rm -r -q -f --ignore-unmatch tests/csvd e2e/tests/csvd tests/pipeline/csvd tests/scripts/csvd` -- `-f` because a file the merge staged is one `git rm` otherwise refuses to drop. |
| `README.md`                                                                | Generic upstream, so it normally merges clean. If the fork retitled it, the H1 is **ours**; everything below it is upstream's.                                                                                                                 |
| `disease/README.md`                                                        | **Ours**, entirely; upstream's is the cSVD page.                                                                                                                                                                                             |
| `fly.toml`                                                                 | **Ours** for `app` and `primary_region`, which name the fork's own Fly app; **upstream's** for every other line.                                                                                                                             |
| `docs/screenshots/`                                                        | **Recaptured**, whatever the merge left. Upstream's images are of the cSVD dashboard: they conflict where the fork recaptured its own, and arrive silently where it never did. After `data/` is regenerated, `deno task screenshots && git add docs/screenshots`; the task builds the merged code, so the images follow any page upstream changed too. |
| Anything else                                                              | Should not conflict. If it does, resolve nothing yet: note the file, finish the merge with the rest, and open an upstream issue naming the file and the two hunks — the seam has a leak there.                                                 |

## The schema diff

A new key in either schema is a key the fork's manifest now has to carry,
because `additionalProperties: false` is enforced on both sides and a
required key that is missing fails `tests/disease_manifest_test.ts` and
`tests/pipeline/test_disease.py`.

```bash
git diff "$OLD_UPSTREAM"..upstream/main -- disease/manifest.schema.json disease/pipeline.schema.json
```

For each added property: read what upstream's own `disease/manifest.json` or
`disease/pipeline.json` put there (`git show upstream/main:disease/manifest.json`)
to learn the shape, then write the fork's value — the researcher's, not a copy
of the cSVD one. A key added as optional (`type: ["string","null"]`, or not
in `required`) may be `null` until the researcher has a value. A removed key
is deleted from the fork's file, or the validators report it as unknown.

Two more upstream files can carry a change the fork has to mirror:

- `disease/prompt.md`'s **section ids**. If upstream's template in
  `pipeline/prompts.py` gained a `{{ slot }}`, the fork's `prompt.md` needs a
  `## <slot>` section or `render_prompt` refuses to start. `git diff
  "$OLD_UPSTREAM"..upstream/main -- disease/prompt.md | grep '^[+-]## '`
  lists them.
- `lib/*_encoding.json` and `disease/{timeline,phenogram}.json` are checked
  against each other by the encoding tests; a new registry fact (a phase, an
  evidence state) lives upstream in `lib/`, a new colour or family lives in
  the fork's `disease/` file, and the test names which is missing.

## Afterwards

Every gate, in the fork, before the merge commit is pushed:

```bash
deno task check && deno task test:coverage
uv run ruff check . && uv run ty check
uv run pytest --cov=pipeline --cov=pipeline/alembic --cov-branch --cov-report=term-missing:skip-covered
uv run pytest tests/scripts
npx --prefix e2e playwright test -c e2e/playwright.config.ts
```

If upstream added an Alembic migration, run
`cd pipeline && uv run alembic upgrade head` against the fork's database
before the next `deno task data`; the export reads the new column names. If
upstream changed the extraction prompt template, the fork's
`recall_baseline.json` is unaffected (it hashes the query, not the prompt),
but any extraction cassette the fork recorded is stale; `pipeline/CLAUDE.md`
says how the golden suite is re-recorded.

Commit the merge with the gates green, and say in the message which of the
locations above needed a hand.
