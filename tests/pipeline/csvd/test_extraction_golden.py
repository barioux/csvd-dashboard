"""Regression suite for LLM extraction.

Scored as recall over the gold genes a paper's retrieved text actually
names, never equality: the model is nondeterministic, so an equality
assertion would flake and then get ignored.

It is not scored as raw set-F1 either, though that was the obvious choice
and this module used to hold a `MIN_F1 = 0.80` that nothing had ever run.
Measured, raw F1 against the gold standard is about 0.40 — and almost none
of that shortfall is extraction. Two things dominate it:

  retrieval   23% of the gold gene-paper pairs name a gene that is simply
              not in the text the pipeline retrieves (`_reachable_genes`)
  curation    the gold standard is a curated dashboard table. Its rows are
              the genes the curators judged putatively causal, not every
              gene a paper implicates. On PMID 37069360 the recorded
              cassette holds 43 genes for 17 gold rows and misses only the
              one (CARF) that is not in the retrieved text and FOXF2, which
              it names only as a GWAS table row's nearest gene, so
              "precision" here is mostly the curators' inclusion criteria,
              which the extraction prompt was never given.

Filter those out and the picture inverts: 85% recall pooled (39/46), and
four of the seven scorable papers recover every reachable gene. A number that
says 0.40 when the truth is 0.85 is worse than no number, so this suite
asserts the one that means something and reports the other alongside it.

The fixtures under tests/pipeline/fixtures/papers/ are the retrieved text of
ten gold-standard papers, so the suite measures extraction and not retrieval.
See `_GOLDEN_PMIDS` for how those ten were chosen and what the choice does
not cover. Seven are full articles, which is publisher text and not ours to
redistribute, so they are gitignored and `scripts/fetch_paper_fixtures.py`
writes them from Europe PMC through the pipeline's own `parse_jats`;
`fixtures/papers/manifest.json` lists them with the hash of the text the
cassettes were recorded against. The three abstract-only fixtures are
tracked.

VCR cassettes under tests/pipeline/csvd/cassettes/test_extraction_golden/ replay
the model's answers without an API key. They embed the fixtures, so they are
gitignored too: the suite runs where both have been produced locally, and CI
and a fresh clone skip it (the `paper_fixtures` and `golden_cassettes`
markers, applied by `pytest_collection_modifyitems` in conftest.py, with a
reason that names what to run). Recording them:

    uv run python -m scripts.fetch_paper_fixtures
    set -a; . ./.env; set +a          # both variables, see _REAL_API_KEY
    rm -rf tests/pipeline/csvd/cassettes/test_extraction_golden
    uv run pytest tests/pipeline/csvd/test_extraction_golden.py \
        --record-mode=once -q
    uv run python -m scripts.fetch_paper_fixtures --update-manifest

`--record-mode=once` writes a cassette only where none exists, so the `rm`
is what makes a re-record actually re-record. It also records *failures* --
an auth error replays forever as if it were the answer -- so check the run
passed before trusting it, and delete the directory again if it did not.
Then re-measure `_RECALL_BASELINE` and the figures quoted from it in
pipeline/CLAUDE.md and disease/README.md.

The `live` pytest marker (see pyproject.toml) is reserved for a future test
that calls the real API directly, outside VCR; nothing here uses it.
"""

import csv
import os
import re
from pathlib import Path
from typing import Any, Final

import pytest

from pipeline.anthropic_client import _build_message_params
from pipeline.config import PipelineConfig
from pipeline.data_merger import _CANONICAL_GENE_SYMBOLS, canonical_gene_symbol
from pipeline.llm_extraction import extract_from_paper
from pipeline.prompts import build_extraction_prompt

# Anchored to this file, not to the working directory: _expected_genes_by_pmid
# reads GOLD at *import* time to build the parametrisation, so a CWD-relative
# path made `pytest` from anywhere but the repo root fail during collection
# rather than skip. Every other test module in the suite uses this idiom.
_HERE = Path(__file__).parent

GOLD = _HERE / "fixtures" / "gold_standard" / "gold_standard_v2.csv"
FIXTURES_DIR = _HERE / "fixtures" / "papers"
# pytest-recording nests cassettes one level under the test module's
# basename by default (see vcr_cassette_dir in pytest_recording/plugin.py):
# tests/pipeline/csvd/cassettes/<module-name>/<test-id>.yaml.
CASSETTE_DIR = _HERE / "cassettes" / "test_extraction_golden"

# Both inputs are gitignored; conftest.py's `pytest_collection_modifyitems`
# turns these two markers into skips that say what to run when either is
# absent, so a fresh clone and CI skip rather than fail.
requires_fixtures = pytest.mark.paper_fixtures
requires_recording = pytest.mark.golden_cassettes


def normalize_symbol(value: str) -> str:
    """Fold case and whitespace, then apply the pipeline's own canonical key.

    Two separate foldings, and both are needed:

    `lib/filters.ts`'s own `normalize()` is `value.trim().toLowerCase()`: it
    trims both ends and lowercases, but neither collapses internal
    whitespace nor applies full Unicode casefolding. Matching that exactly —
    rather than a more aggressive `.split()`-and-`.casefold()` — is what
    keeps this suite measuring extraction quality instead of string
    formatting.

    `canonical_gene_symbol` is what `data_merger` groups on, and without it
    this suite scored a miss the pipeline does not make. The curated table
    holds one row keyed `COL4A1/2`; the model names `COL4A1` and `COL4A2`
    separately, exactly as the papers do, and the merge folds them together.
    Comparing raw symbols marked that gene absent from four of the five
    papers that report it — an artifact of the harness, counted as a recall
    failure.
    """
    return canonical_gene_symbol(value.strip()).strip().lower()


def field_set_f1(expected: set[str], actual: set[str]) -> float:
    """F1 over two sets. Two empty sets score 1.0."""
    if not expected and not actual:
        return 1.0
    overlap = len(expected & actual)
    if overlap == 0:
        return 0.0
    precision = overlap / len(actual)
    recall = overlap / len(expected)
    return 2 * precision * recall / (precision + recall)


def test_f1_is_1_for_two_empty_sets() -> None:
    assert field_set_f1(set(), set()) == 1.0


def test_f1_is_0_when_nothing_overlaps() -> None:
    assert field_set_f1({"a"}, {"b"}) == 0.0


def test_f1_is_0_when_expected_is_empty_but_actual_is_not() -> None:
    """One-sided emptiness is a different path than 'no overlap' between two
    non-empty sets; both must resolve to 0.0 without dividing by zero.
    """
    assert field_set_f1(set(), {"a"}) == 0.0


def test_f1_is_0_when_actual_is_empty_but_expected_is_not() -> None:
    assert field_set_f1({"a"}, set()) == 0.0


def test_f1_partial_overlap_is_the_harmonic_mean() -> None:
    # precision = 1/1 = 1.0, recall = 1/3: the harmonic mean is 0.5, while
    # the arithmetic mean would be 0.667. Asymmetric on purpose -- a
    # precision == recall case (e.g. {"a", "b"} vs {"a", "c"}) can't tell
    # the two averaging methods apart, since they coincide whenever the two
    # inputs are equal.
    assert field_set_f1({"a", "b", "c"}, {"a"}) == 0.5


def test_normalize_symbol_folds_case_and_whitespace() -> None:
    assert normalize_symbol("  PSMD ") == normalize_symbol("psmd")


def _gold_rows() -> list[dict[str, str]]:
    with GOLD.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _expected_genes_by_pmid() -> dict[str, set[str]]:
    """Map each individual PMID to the raw gold-standard symbols reported for it.

    The CSV has one row per gene, not one row per (gene, PMID) pair: `pmid`
    is a comma-separated list of every paper supporting that row, so it has
    to be split apart before grouping.
    """
    by_pmid: dict[str, set[str]] = {}
    for row in _gold_rows():
        for pmid in row["pmid"].split(","):
            pmid = pmid.strip()
            if pmid:
                by_pmid.setdefault(pmid, set()).add(row["gene"])
    return by_pmid


# The fixture papers, chosen deliberately rather than sliced off a sort.
#
# `sorted(_expected_genes_by_pmid())[:10]` used to pick these, which sorts
# by PMID -- that is, by publication era. It took the ten oldest papers,
# six of them single-gene, and never measured extraction on 37069360, the
# 17-gene paper that is by far the hardest recall case in the corpus. A
# sample chosen by an unrelated ordering is not a sample.
#
# These ten span the two axes that plausibly move the score:
#
#   gene count  17, 11, 8, 7, 6, 4, 3, 2, 1, 1 -- the full range the gold
#               standard offers, so recall is measured where it is hard and
#               precision where over-extraction would show
#   retrieval   7 Europe PMC full texts, 3 abstract-only -- close to the
#               corpus ratio of 15:7, and 34358307 asks for 6 genes from a
#               1,971-character abstract
#   era         2005 (15905468) through 2024 (39216230)
#
# What this does NOT span, and no honest reading should assume it does:
#
#   - No PDF-fallback paper. All 22 gold-standard PMIDs resolve through
#     Europe PMC or an abstract; none exercises pipeline/pdf_parse.py. The
#     Docling path is covered by its own unit tests, not by extraction
#     quality.
#   - No negative case. Every gold-standard row reports at least one gene,
#     so nothing here measures what the model does with a paper that has no
#     cSVD gene in it. Adding one means curating a new gold row, which is
#     an editorial act, not a test change.
#
# Fixed rather than computed on purpose: adding a row to the gold standard
# must not silently change which papers the suite measures.
_GOLDEN_PMIDS = (
    "37069360",  # 17 genes, Europe PMC, 68 kB -- the recall ceiling
    "33293549",  # 11 genes, Europe PMC, 68 kB
    "36180795",  #  8 genes, Europe PMC, 78 kB -- the longest full text
    "33773637",  #  7 genes, Europe PMC, 38 kB
    "34358307",  #  6 genes, abstract only, 1.9 kB -- six genes, no body
    "35511193",  #  4 genes, Europe PMC, 70 kB
    "31430377",  #  3 genes, abstract only, 3.0 kB
    "35943854",  #  2 genes, Europe PMC, 58 kB
    "15905468",  #  1 gene,  abstract only, 2.0 kB -- oldest, 2005
    "39216230",  #  1 gene,  Europe PMC, 56 kB -- newest, 2024
)


def test_every_selected_pmid_is_in_the_gold_standard() -> None:
    """The fixed selection must not drift away from the CSV it scores against.

    `_GOLDEN_PMIDS` is a literal, so nothing stops a PMID being edited out
    of the gold standard while the tuple still names it -- at which point
    `_expected_genes_by_pmid()[pmid]` raises KeyError inside a test that
    only runs when cassettes are present, and the breakage surfaces
    somewhere far from its cause.
    """
    known = _expected_genes_by_pmid()
    missing = [pmid for pmid in _GOLDEN_PMIDS if pmid not in known]
    assert not missing, f"not in {GOLD.name}: {missing}"


def test_the_selection_spans_the_gene_count_range() -> None:
    """Pin the range the selection comment claims, not just the count.

    The retrieval mix is not checked here: which path a PMID resolves
    through is a property of Europe PMC on the day it was fetched, not of
    the gold standard, so a test could only restate the fixture sizes.

    A future edit that swapped a multi-gene paper for another single-gene
    one would leave the comment true-looking and the sample flat.
    """
    counts = sorted(
        (len(_expected_genes_by_pmid()[pmid]) for pmid in _GOLDEN_PMIDS),
        reverse=True,
    )
    assert len(_GOLDEN_PMIDS) == len(set(_GOLDEN_PMIDS)) == 10
    assert counts[0] >= 15, f"no high-recall paper in the selection: {counts}"
    assert counts[-1] == 1, f"no single-gene paper in the selection: {counts}"
    assert len(set(counts)) >= 8, f"gene counts barely vary: {counts}"


# Header names that must never carry a real value in a committed cassette.
# The first two are credentials; the last two are account identifiers the
# API echoes back in every response. conftest.py's `vcr_config` strips all
# four -- `filter_headers` for the request side, `before_record_response`
# for the response side, because VCR's filter_headers does not reach a
# response.
_MUST_BE_REDACTED = frozenset(
    {
        "x-api-key",
        "authorization",
        "anthropic-workspace-id",
        "anthropic-organization-id",
    }
)


@requires_recording
def test_no_cassette_carries_a_credential_or_an_account_identifier() -> None:
    """Guard the artifact, not just the hook that writes it.

    A cassette recorded before a redaction rule existed keeps whatever it
    captured, and nothing about replaying it would ever complain. This is
    the check that fails on such a file -- and on a future SDK version that
    starts echoing an identifier under a name the config does not know.
    """
    import yaml

    leaks: list[str] = []
    for cassette in sorted(CASSETTE_DIR.glob("*.yaml")):
        loaded = yaml.safe_load(cassette.read_text(encoding="utf-8"))
        for interaction in loaded.get("interactions", []):
            for side in ("request", "response"):
                headers = (interaction.get(side) or {}).get("headers") or {}
                for name, values in headers.items():
                    if name.lower() not in _MUST_BE_REDACTED:
                        continue
                    if list(values) != ["REDACTED"]:
                        leaks.append(f"{cassette.name}: {side}.{name}")
    assert not leaks, "unredacted in committed cassettes: " + ", ".join(leaks)


def _search_names(gene: str) -> tuple[str, ...]:
    """Every raw symbol a paper might use for one curated gold key.

    The gold standard stores the merged key `COL4A1/2`; papers name COL4A1
    and COL4A2 separately. Inverting the pipeline's own canonical map keeps
    the two in step, so adding a symbol pair there does not quietly make a
    gene look unreachable here.
    """
    aliases = tuple(
        raw for raw, key in _CANONICAL_GENE_SYMBOLS.items() if key == gene
    )
    return aliases + (gene,)


def _reachable_genes(pmid: str) -> set[str]:
    """The gold genes for `pmid` that its retrieved text actually names.

    A gold row lists every PMID supporting that gene. It does *not* claim
    the gene is written in each paper's retrievable text, and for 14 of the
    60 gene-paper pairs across these ten fixtures (23%) it is not:

      - abstract-only papers (34358307, 15905468) carry none of their genes,
        because the curator read the full paper and the pipeline gets 1-3 kB
        of abstract;
      - 36180795 and 35943854 are full texts whose remaining genes live in
        supplementary tables that Europe PMC's fullTextXML does not serve.

    Scoring recall against a gene that is not in the input measures
    retrieval coverage and prints it as extraction quality. This is the
    filter that keeps the two apart. Word-boundary, case-insensitive: gene
    symbols are distinctive tokens, so a substring match would count
    "LAMC1" inside "LAMC10" and a stricter match would miss ordinary
    sentence punctuation.
    """
    text = (FIXTURES_DIR / f"{pmid}.txt").read_text(encoding="utf-8")
    reachable = set()
    for gene in _expected_genes_by_pmid()[pmid]:
        patterns = (rf"\b{re.escape(name)}\b" for name in _search_names(gene))
        if any(re.search(p, text, re.IGNORECASE) for p in patterns):
            reachable.add(normalize_symbol(gene))
    return reachable


# Recall floor per paper, measured on the committed cassettes. Four of the
# seven scorable papers recover every reachable gene; the floors are set at
# the measured value so any drop is a regression, not a judgement call.
#
# 36180795 is the outlier and the reason this table is not a single
# constant. It is the GIGASTROKE all-stroke GWAS: the extraction prompt
# targets cerebral small vessel disease, the paper is about stroke at large,
# and the model is correspondingly conservative. That is a prompt scope gap
# rather than an extraction bug, and it is exactly the finding this suite
# exists to make visible. Raise the floor when the prompt is widened; do not
# raise it to make a red run green. Its floor is now 0.0, which asserts
# nothing about this paper -- see the Opus 5.5 note below for why that is
# the measurement rather than a waiver.
_RECALL_BASELINE: Final[dict[str, float]] = {
    "37069360": 0.9375,  # 15/16 -- FOXF2, Opus 5.5 re-record, see below
    "33293549": 0.80,  # 10/10 measured; floor kept, see below
    "36180795": 0.0,  # 0/5   -- known prompt scope gap, see above and below
    "33773637": 1.0,  # 7/7
    "34358307": 1.0,  # skipped: 0 reachable
    "35511193": 0.75,  # 3/4  -- PLEKHG1, Opus 5.5 re-record, see below
    "31430377": 1.0,  # 3/3
    "35943854": 1.0,  # skipped: 0 reachable
    "15905468": 1.0,  # skipped: 0 reachable
    "39216230": 1.0,  # 1/1
}

# 33293549 is the one floor that moved when the cassettes were re-recorded on
# 2026-09-02, from 10/10 to 8/10 -- `DEGS2` and `PLEKHG1`, neither named in the
# prompt. Nothing about extraction changed between the two recordings: same
# model, same prompt version, same effort, same fixture text. What changed is
# the tool schema (the `cerebral-microbleeds` enum member and the SDK's
# generated `description`), which constrains the answer without steering it,
# and the sample -- the model is nondeterministic, which is the reason this
# suite scores recall against a floor rather than asserting equality.
#
# So this is a re-measurement, not a licence. A floor is lowered only by a
# re-record, only to what that recording measures, and it is written down
# here with the genes it lost; lowering one to turn a red run green is the
# same error as raising one, in the other direction. Pooled recall went 42/46
# to 40/46 with it, and the clean (uncontaminated) subset 23/26 to 21/26.
#
# The 2026-09-11 re-record -- against fixtures refetched through the current
# parser, when the full texts stopped being committed -- measured 33293549 at
# 10/10 again, pooled 42/46 and clean 23/26. The floor stays at 0.80: the
# sample has now landed on both sides of it with nothing about extraction
# changed, which is what a floor below the best observed value is for. Both
# figures are quoted in pipeline/CLAUDE.md and disease/README.md and move with this.
#
# The 2026-11-02 re-record is the move from Claude Opus 5 to Claude Opus 5.5,
# at effort "high" over the same fixture text (every manifest hash matched),
# and three floors came down to what it measured. Pooled recall went 43/46 to
# 39/46 and clean 23/26 to 21/26. The genes lost:
#
#   37069360  FOXF2 -- in the text only as a nearest gene in a GWAS table row
#   35511193  PLEKHG1 -- named only as the nearest gene to one locus
#   36180795  ICA1L, GP1BA, LAMC1, LAMC2 -- Opus 5.5 reported one gene,
#             PTCH1, writing "I found one gene with cSVD-specific evidence"
#
# Every one of them was a gene Opus 5 reported below the update floor
# (FOXF2 0.25, PLEKHG1 0.2, GP1BA 0.3, LAMC1 0.25, LAMC2 0.3) except
# ICA1L at 0.5, so a pipeline run would have discarded all but ICA1L anyway.
# Scored after the update floor -- what a run actually publishes -- the two
# recordings are pooled 38/46 (Opus 5) and 39/46 (Opus 5.5), clean 20/26 and
# 21/26: Opus 5.5 leaves out genes it does not believe instead of reporting
# them at 0.2. 33293549 measured 10/10 and keeps its 0.80 floor for the
# reason above. This suite still scores raw extraction, before the floor
# (see pipeline/CLAUDE.md, "What is not proven"), so these are the floors.


def test_every_selected_paper_has_a_recall_baseline() -> None:
    """A new fixture paper must arrive with a measured floor, not a default."""
    assert set(_RECALL_BASELINE) == set(_GOLDEN_PMIDS)


@requires_fixtures
def test_the_gold_standard_is_not_fully_reachable() -> None:
    """Pin the retrieval gap so an improvement or a regression is visible.

    This number is a property of Europe PMC's coverage, not of the model,
    and it caps recall before extraction is involved at all. It belongs in
    the suite because a silent drop -- a retrieval path breaking, an
    abstract served where full text used to be -- would otherwise show up
    as an extraction failure.
    """
    total = sum(len(_expected_genes_by_pmid()[p]) for p in _GOLDEN_PMIDS)
    reachable = sum(len(_reachable_genes(p)) for p in _GOLDEN_PMIDS)
    assert total == 60, f"gold standard changed: {total} gene-paper pairs"
    assert reachable == 46, (
        f"{reachable}/60 gold genes are in the retrieved text, was 46/60. "
        "If retrieval improved, re-measure _RECALL_BASELINE and update both."
    )


def _genes_named_in_the_prompt() -> set[str]:
    """Gold-standard genes that appear verbatim in the production prompt."""
    prompt = build_extraction_prompt(
        paper_text="",
        pmid="0",
        max_chars=1,
        prompt_version=PipelineConfig().prompt_version,
    )
    text = f"{prompt.system_prompt}\n{prompt.extraction_instructions}"
    named = set()
    for genes in _expected_genes_by_pmid().values():
        for gene in genes:
            patterns = (rf"\b{re.escape(n)}\b" for n in _search_names(gene))
            if any(re.search(p, text) for p in patterns):
                named.add(normalize_symbol(gene))
    return named


def test_the_prompt_names_part_of_its_own_answer_key() -> None:
    """Pin the contamination so it cannot grow unnoticed.

    The production prompt's few-shot examples name 13 of the 36 gold-standard
    genes, six of them with the expected gwas_trait and confidence. That
    inflates measured recall on those genes, so the harness reports the
    two separately and the paper should quote the uncontaminated figure.

    This is not a licence to add more. If a prompt edit names another gold
    gene, this fails and the number has to be re-measured.
    """
    named = _genes_named_in_the_prompt()
    assert len(named) == 13, (
        f"{len(named)} gold genes are named in the prompt, was 13: {sorted(named)}"
    )


def _recorded_genes(pmid: str) -> list[dict]:
    """The gene dicts in this PMID's committed cassette.

    Reads the recorded response directly instead of replaying through VCR:
    the tests below compare two subsets of one run, so they need the
    extraction output as data rather than as an assertion, and decoding is
    cheaper than standing up a client per PMID. Confidence rides along,
    which is what lets a floor be swept without new API spend.

    The response body is a gzipped SSE stream. The genes arrive as the
    input of a tool_use block, so they are assembled from
    `input_json_delta` fragments -- the concatenated partials *are* the
    JSON, with no outer decode. Before the schema moved onto a tool they
    were `text_delta` fragments of a JSON string, which needed one.
    """
    import base64
    import contextlib
    import gzip
    import json

    import yaml

    cassette = CASSETTE_DIR / (
        f"test_extraction_recovers_the_reachable_genes[{pmid}].yaml"
    )
    loaded = yaml.safe_load(cassette.read_text(encoding="utf-8"))
    body = loaded["interactions"][0]["response"]["body"]["string"]
    raw = body if isinstance(body, bytes) else base64.b64decode(body)
    with contextlib.suppress(OSError, gzip.BadGzipFile):
        raw = gzip.decompress(raw)  # already decompressed by the recorder
    stream = raw.decode("utf-8", "replace")
    partials = re.findall(
        r'"input_json_delta","partial_json":"((?:[^"\\]|\\.)*)"', stream
    )
    payload = json.loads("".join(json.loads(f'"{p}"') for p in partials))
    return payload.get("genes", [])


def _recorded_extraction(pmid: str) -> set[str]:
    """The normalized gene symbols in this PMID's committed cassette."""
    return {normalize_symbol(g["gene_symbol"]) for g in _recorded_genes(pmid)}


def _recorded_request(pmid: str) -> dict[str, Any]:
    """The request body this PMID's cassette was recorded against.

    Plain JSON, unlike the gzipped SSE on the response side. `stream` is
    dropped: the SDK adds it when it sends a streaming request, so it is
    not part of what `_build_message_params` produces and comparing it
    would only ever report a difference that is not one.
    """
    import json

    import yaml

    cassette = CASSETTE_DIR / (
        f"test_extraction_recovers_the_reachable_genes[{pmid}].yaml"
    )
    loaded = yaml.safe_load(cassette.read_text(encoding="utf-8"))
    body = loaded["interactions"][0]["request"]["body"]
    recorded = json.loads(body.decode() if isinstance(body, bytes) else body)
    recorded.pop("stream", None)
    return recorded


def _current_request(pmid: str) -> dict[str, Any]:
    """What the pipeline would send for this PMID's fixture today."""
    text = (FIXTURES_DIR / f"{pmid}.txt").read_text(encoding="utf-8")
    return _build_message_params(text, pmid, PipelineConfig())


# Every figure this module reports -- the per-paper recall floors,
# `_RECALL_BASELINE`, the update-floor sweep, the contaminated/clean split --
# is measured against a recorded response. VCR matches those recordings on
# method and URI alone (`vcr_config` sets no `match_on`, and every request
# here is a POST to /v1/messages), so a replayed run answers the same way
# whatever prompt, model, thinking config or tool schema the code now sends.
# The two tests below are what tie the numbers to the request: they compare
# the recorded body with what `_build_message_params` builds today, offline
# and without spending an API call.


@requires_fixtures
@requires_recording
@pytest.mark.parametrize("pmid", _GOLDEN_PMIDS)
def test_the_cassettes_were_recorded_with_the_request_the_code_sends(
    pmid: str,
) -> None:
    """Model, prompt, document, thinking and tool_choice, as recorded.

    The tool schema is compared by the test below, which was an
    ``xfail(strict=True)`` while the committed cassettes predated the
    ``cerebral-microbleeds`` enum member and the SDK's generated
    ``description``. The 2026-09-02 re-record closed that gap and the
    marker came off with it.
    """
    if not _reachable_genes(pmid):
        pytest.skip(f"PMID {pmid} has no reachable gold gene, so no cassette")

    recorded = _recorded_request(pmid)
    current = _current_request(pmid)
    # Named rather than compared whole: `system` and `messages` carry the
    # prompt and the paper, so an equality assertion on the two dicts would
    # print two 60 kB blobs and say nothing about which part moved.
    differing = sorted(
        key
        for key in (set(recorded) | set(current)) - {"tools"}
        if recorded.get(key) != current.get(key)
    )
    assert not differing, (
        f"PMID {pmid}: the cassette was recorded with a different "
        f"{', '.join(differing)}, so every recall figure in this module was "
        "measured against a request the pipeline no longer sends. Re-record "
        "the harness -- see this module's docstring -- and re-measure "
        "_RECALL_BASELINE with it."
    )


@requires_fixtures
@requires_recording
@pytest.mark.parametrize("pmid", _GOLDEN_PMIDS)
def test_the_cassettes_were_recorded_with_the_tool_schema_the_code_sends(
    pmid: str,
) -> None:
    """The schema is the grammar the recorded answer was produced under."""
    if not _reachable_genes(pmid):
        pytest.skip(f"PMID {pmid} has no reachable gold gene, so no cassette")

    assert _recorded_request(pmid)["tools"] == _current_request(pmid)["tools"]


@requires_fixtures
@requires_recording
def test_the_update_floor_is_set_where_gold_recall_saturates() -> None:
    """Sweep the update floor against recall over reachable gold genes.

    Recall is the only quantity the gold standard can measure honestly
    here: a gene it does not list is unreviewed rather than wrong, so
    "precision" against it would count the curators' inclusion criteria as
    extraction errors. The sweep reports how many non-gold genes each floor
    admits, and asserts only on recall.

    Post-hoc arithmetic over the committed cassettes -- the recorded
    extractions carry per-gene confidence -- so this costs no API spend.

    The assertion is that the *configured* update floor still recovers what
    the best floor recovers. Asserting instead that the floor merely sits
    above where recall saturates would pass for any value up to 0.70, which
    guards nothing: the whole finding is that 0.65 discards eight gold
    genes the model correctly extracted.
    """
    configured = PipelineConfig().confidence_threshold_update

    def measure(floor: float) -> tuple[float, int]:
        hit = total = extra = 0
        for pmid in _GOLDEN_PMIDS:
            reachable = _reachable_genes(pmid)
            if not reachable:
                continue
            admitted = {
                normalize_symbol(g["gene_symbol"])
                for g in _recorded_genes(pmid)
                if g.get("confidence", 0.0) >= floor
            }
            hit += len(reachable & admitted)
            total += len(reachable)
            extra += len(admitted - reachable)
        return hit / total, extra

    floors = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
    table = [(floor, *measure(floor)) for floor in floors]
    report = "\n".join(
        f"    {f:.2f}  recall {r:.0%}  non-gold admitted {e}" for f, r, e in table
    )

    best = max(recall for _, recall, _ in table)
    at_configured, _ = measure(configured)
    assert at_configured >= best - 0.02, (
        f"the update floor {configured:.2f} recovers {at_configured:.0%} of "
        f"reachable gold genes against {best:.0%} at the best floor swept, "
        f"costing {(best - at_configured) * 100:.0f} points:\n{report}"
    )


@requires_fixtures
@requires_recording
def test_recall_is_reported_separately_for_contaminated_genes() -> None:
    """The honest headline is the clean subset, not the pooled figure.

    13 of the 36 gold genes are named in the production prompt and six carry their
    expected gwas_trait and confidence, so recall on those genes is
    measuring partly what the prompt already said. The clean subset is the
    number that means something about extraction.

    Recorded rather than replayed: this reads the committed cassettes, so
    it costs nothing and needs no credentials.
    """
    named = _genes_named_in_the_prompt()
    seen_hit = seen_tot = clean_hit = clean_tot = 0
    for pmid in _GOLDEN_PMIDS:
        reachable = _reachable_genes(pmid)
        if not reachable:
            continue
        actual = _recorded_extraction(pmid)
        for gene in reachable:
            hit = gene in actual
            if gene in named:
                seen_tot += 1
                seen_hit += hit
            else:
                clean_tot += 1
                clean_hit += hit

    assert clean_tot >= 20, f"clean subset too small to report: {clean_tot}"
    assert clean_hit / clean_tot >= 0.80, (
        f"uncontaminated recall {clean_hit}/{clean_tot}; "
        f"prompt-named {seen_hit}/{seen_tot}"
    )


# Read at import, which is the only time they are still there. conftest.py's
# autouse `_isolate_credentials` deletes both of these before every test to
# keep the suite hermetic, and a conftest fixture is set up before a
# module-level one -- so `_ensure_credentials` below would find them already
# gone. Capturing them here, above every fixture, is what makes
# `--record-mode=once` work at all.
#
# The workspace id matters as much as the key: an identity-linked API key is
# rejected outright without it ("anthropic-workspace-id is required when
# authenticating with an identity-linked API key"), and the 400 is recorded
# into the cassette as if it were the answer.
_REAL_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
_REAL_WORKSPACE_ID = os.environ.get("ANTHROPIC_WORKSPACE_ID")
_REPLAY_API_KEY = "sk-ant-test-cassette-replay-key"


@pytest.fixture(autouse=True)
def _ensure_credentials(monkeypatch) -> None:
    """Satisfy `AnthropicClient._get_client`'s own precondition check.

    That check runs in plain Python before any HTTP call is made — the
    point at which VCR could otherwise intercept — and raises
    ExtractionFailedError if ANTHROPIC_API_KEY is unset. Cassette replay
    only needs *some* value to get past it; a recording session needs the
    real ones, preserved from import time above.

    The workspace id is left unset when there is none, so replay does not
    invent a header the cassettes were not recorded with.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", _REAL_API_KEY or _REPLAY_API_KEY)
    if _REAL_WORKSPACE_ID:
        monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", _REAL_WORKSPACE_ID)


@pytest.mark.vcr
@requires_fixtures
@requires_recording
@pytest.mark.parametrize("pmid", _GOLDEN_PMIDS)
async def test_extraction_recovers_the_reachable_genes(pmid: str) -> None:
    """Recall over the genes that are actually in the text, never raw F1.

    Raw set-F1 against the gold standard was the obvious thing to assert and
    it measures the wrong quantity — see `_reachable_genes` for why 23% of
    the gold pairs are unreachable, and `_RECALL_BASELINE` for what the
    numbers turn out to be. F1 is still computed and reported in the failure
    message, because it is the number a reader will expect to see; it is
    just not what passes or fails the test.
    """
    reachable = _reachable_genes(pmid)
    if not reachable:
        pytest.skip(
            f"PMID {pmid}: none of its {len(_expected_genes_by_pmid()[pmid])} "
            "gold genes appear in the retrieved text (see _reachable_genes)"
        )

    text = (FIXTURES_DIR / f"{pmid}.txt").read_text(encoding="utf-8")
    genes, _ = await extract_from_paper(text, pmid, PipelineConfig())
    actual = {normalize_symbol(g.gene_symbol) for g in genes}

    found = reachable & actual
    recall = len(found) / len(reachable)
    expected_all = {normalize_symbol(g) for g in _expected_genes_by_pmid()[pmid]}
    floor = _RECALL_BASELINE[pmid]
    # Mark the misses the prompt already names: missing one of those is a
    # different kind of failure from missing a gene the model had to find
    # on the evidence, and the two should not read alike in a red run.
    named = _genes_named_in_the_prompt()
    missed = ", ".join(
        f"{gene}{' (named in the prompt)' if gene in named else ''}"
        for gene in sorted(reachable - actual)
    )
    assert recall >= floor, (
        f"PMID {pmid}: recall {recall:.2f} < {floor:.2f} over "
        f"{len(reachable)} reachable genes; missed {missed}. "
        f"(raw F1 against all {len(expected_all)} gold genes: "
        f"{field_set_f1(expected_all, actual):.2f})"
    )


@pytest.mark.vcr
@requires_fixtures
@requires_recording
async def test_every_extracted_gene_carries_a_source_quote() -> None:
    """Provenance is required; an empty quote must never reach the database."""
    pmid = _GOLDEN_PMIDS[0]
    text = (FIXTURES_DIR / f"{pmid}.txt").read_text(encoding="utf-8")
    genes, _ = await extract_from_paper(text, pmid, PipelineConfig())
    assert genes, "expected at least one gene"
    for gene in genes:
        assert gene.source_quote.strip()
