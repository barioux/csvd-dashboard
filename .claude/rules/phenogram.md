---
paths:
  - "islands/Phenogram.tsx"
  - "lib/phenogram.ts"
  - "lib/phenogram_encoding.json"
  - "disease/phenogram.json"
  - "lib/disease/citation.ts"
  - "lib/cytobands.ts"
  - "disease/vocabulary.json"
  - "scripts/phenogram_figure.py"
  - "scripts/fetch_cytobands.py"
  - "tests/phenogram_*.ts"
  - "tests/csvd/phenogram_*.ts"
  - "tests/cytobands_test.ts"
  - "tests/scripts/test_phenogram_figure.py"
  - "tests/scripts/csvd/test_csvd_phenogram_figure.py"
  - "e2e/tests/phenogram.spec.ts"
  - "e2e/tests/csvd/phenogram.spec.ts"
---

# Phenogram

`islands/Phenogram.tsx` draws the karyogram from `data/table1.json` and
`data/cytobands_hg38.json` (UCSC hg38, 862 bands, written by
`scripts/fetch_cytobands.py`). A gene is placed at the midpoint of the band its
`chromosomalLocation` names — exact bands only, in `lib/cytobands.ts`, where
NCBI's range for a gene across a band edge (`10q24.33-q25.1`) spans its two
exact ends;
`tests/cytobands_test.ts` fails if any committed location stops resolving, and
the island lists such genes instead of guessing.

Two renderers, one contract, as for the timeline:

- **`disease/vocabulary.json` is the single source of truth for the GWAS trait
  vocabulary**, and the only place a trait is defined: key, display label,
  family, long name, the STRIVE-2 definition where one exists, and an internal
  ontology `xref` (or an explicit `null` with the reason — seven of the sixteen
  have no usable term in any of the ~250 ontologies OLS4 indexes, because this
  axis is finer-grained than anything published). It also carries `synonyms`, an
  **ordered** array `pipeline/export/tables.py` reads as `_TRAIT_REWRITES`
  (substring folds, applied in sequence — so a short code like `OD` must never
  be folded: it occurs inside `NODDI`), and `untracked`, the prompt vocabulary
  the dashboard deliberately does not carry, each with a reason.
  `GWAS_TRAIT_CHOICES` in `lib/constants.ts` and both phenogram renderers derive
  from this file; nothing restates it. That restating is what let `PVWMH` — the
  second most extracted trait in `logs/json/pipeline_report_*.json`, 29 times —
  have no filter choice and no phenogram entry, and let `lacunar stroke` be
  labelled `"Lacunar Stroke"` in one file and `"Lacunar stroke"` in another.
- **The prompt is reconciled, not generated.** `pipeline/prompts.py`'s
  canonical-abbreviation sentence stays a frozen literal — it is recorded in
  cassettes and treated as part of the method, so generating it would let an
  edit silently change what the model is asked.
  `tests/pipeline/test_prompt_vocabulary.py` fails in both directions instead: a
  term the prompt asks for that the vocabulary does not declare, and a
  prompt-sourced vocabulary entry the prompt no longer asks for. A synonym
  records `source: "prompt"` or `"curated"`; only the first is reconciled,
  because a curated spelling comes from the source spreadsheet and the prompt
  has never asked for it.
- `lib/phenogram_encoding.json` holds appearance only: the evidence glyphs, the
  band-stain greys and the geometry constants. The seven phenotype families and
  their hues and tints are the disease's and live in `disease/phenogram.json`;
  the citation standard the STRIVE-2 definitions are quoted from is
  `citationStandard` in `disease/manifest.json`, read on the TypeScript side
  through `lib/disease/citation.ts`. `tests/phenogram_encoding_test.ts` fails
  when the data carries a GWAS trait the vocabulary does not, when the filter
  choices stop being derived from it, when the two renderers stop composing the
  same identity, or when a family hue leaves the lightness band or sits within
  OKLab ΔE 15 of its legend neighbour. Add the entry; do not widen the test.

  **The `viewBox` is not derived, and both dimensions have overflowed.** A
  chromosome's label stack needs `blocks * (height + blockGap)` and a row needs
  `margin * 2 + columns * (chromosomeWidth + leaderGap + labelColumn)`. The
  365-day run broke the first (chromosome 17 reached ten genes, 498 units
  against a 400 row) and then the second (X made row 2 eleven columns, 1,732
  against 1,600). The 2026-11-03 re-extraction broke the first again:
  chromosome 17 reached thirteen genes and 736 units, chromosome 2 666, against
  a 520 row, because genes now carry more trait pills (2.2 a gene against 1.8).
  `rowHeight` is 760 and the viewBox 1732x1640 (`2 * margin + 2 * rowHeight +
  rowGap`). A band range such as `10q24.33-q25.1`, which NCBI writes for a gene
  across a band edge, is placed at the middle of its span in both renderers;
  any other inexact band is still unplaced. The layout test
  catches only the vertical case -- it asserts blocks stay inside their _row_ --
  so the horizontal one was found by measuring every `li` in
  `ul.phenogram-genes` against the list's own bounding box in a browser. A
  figure can be internally consistent and still not fit its plate.
- `lib/phenogram.ts` (island) and `scripts/phenogram_figure.py` (print)
  implement the same rule: chromosomes that carry a gene, to scale, in two rows
  split after chromosome 10; a label block per gene — symbol and evidence glyphs
  on the first line, then one pill per GWAS trait — stacked by
  `resolveCollisions` (sweep down to enforce the gap, sweep back up at the row's
  bottom). `tests/phenogram_layout_test.ts` and
  `tests/scripts/test_phenogram_figure.py` pin the same numbers.

Its key sits above the plate too, in the same collapsed `<details>` and for the
same reasons — supporting evidence in one cell, the phenotype key spanning two,
both on `auto-fit` grids guarded with `min(…, 100%)`. The skip link targets
`#phenogram-end` after the plate.

Trait identity is text, not colour: each trait is a pill with its label on the
family tint, because no seven-hue palette is pairwise distinguishable under
colour-blindness simulation and the printed figure has no tooltip. The hues are
the dataviz reference palette minus yellow, in the one legend order (of 720)
that clears both adjacent gates; the gates and the OKLab distances are recorded
in `tests/phenogram_encoding_test.ts`.

The SVG is `aria-hidden` decoration. Over it, `ul.phenogram-genes` holds one
absolutely positioned `<li>` per block, in percent of the viewBox, each wrapping
`components/Tooltip.tsx` around a visually-hidden name — so hover, Enter,
Tab-to-link and Escape are the tables' behaviour with no new tooltip code, and
screen readers get the 87 genes in chromosome order. Pill backgrounds and the
glyph positions after each symbol are the only measured geometry (`getBBox()` on
mount and on `document.fonts.ready`); block positions never depend on text
metrics.
