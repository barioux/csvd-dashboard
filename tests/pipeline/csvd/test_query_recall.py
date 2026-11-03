"""Retrieval-recall regression suite for ``SVD_QUERY``.

Measures what fraction of the papers this dashboard actually cites the
production PubMed query can retrieve. The measurement is exact rather than
sampled: PubMed is asked for ``(SVD_QUERY) AND (pmid[uid] OR ...)``, which
intersects the query with the gold set in one request. No ``retmax`` paging,
no random sample, and no exposure to the 9,999-record ``esearch`` cap that
makes a naive recall denominator silently wrong.
"""

from pathlib import Path
from typing import Final, cast

import pytest

from pipeline.pubmed_search import (
    DISEASE_TERMS,
    GENETIC_TERMS,
    MESH_TERMS,
    SVD_QUERY,
)
from pipeline.query_eval import (
    esearch_pmids,
    gold_pmids,
    measure_recall,
    uid_clause,
)


def test_gold_pmids_include_every_pmid_in_the_gold_standard_csv() -> None:
    """The curated extraction fixture is the floor of the retrieval gold set."""
    pmids = set(gold_pmids())
    assert "37063705" in pmids
    assert "33773637" in pmids
    assert len(pmids) >= 22


def test_gold_pmids_include_references_the_gold_csv_does_not_carry() -> None:
    """The published table cites papers the extraction fixture omits.

    ``data/table1.json`` is the dataset the dashboard actually ships, so its
    ``references`` are part of what a retrieval query has to be able to find.
    Six of them appear in no gold-standard CSV row -- among them the two PMIDs
    recovered from the mangled reference lists (``39114924``, ``39805841``).
    """
    pmids = set(gold_pmids())
    assert {"19539236", "26063658", "33773636", "39114924", "39805841"} <= pmids


def test_vcr_redacts_the_entrez_credentials_from_query_strings(
    vcr_config: dict[str, object],
) -> None:
    """Entrez sends its key as a query *parameter*, not a header.

    The suite's other cassettes record Anthropic traffic, where the credential
    is a header and ``filter_headers`` covers it. E-utilities puts ``api_key``
    and ``email`` in the URL instead, which header filtering does not reach.

    ``_isolate_credentials`` in conftest.py is the first line of defence and
    normally keeps a real key out of any request at all. This is the second:
    that fixture's own docstring invites a test to set a value it needs, and
    such a test would otherwise write a live NCBI key into a committed
    cassette. The module-scoped fixture below is exactly that case.
    """
    filtered = cast("list[tuple[str, str]]", vcr_config["filter_query_parameters"])
    assert ("api_key", "REDACTED") in filtered
    assert ("email", "REDACTED") in filtered


_CASSETTES = Path(__file__).parent / "cassettes"
# The credential scan over every committed cassette, this tree's included, is
# tests/pipeline/test_query_recall_gold.py, which a fork keeps.


@pytest.fixture(autouse=True)
def _entrez_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supply the contact address NCBI policy requires.

    ``_isolate_credentials`` clears ENTREZ_EMAIL for every test, and its
    docstring says a test needing a value sets it itself. Without this the
    live calls below would go out anonymously and each would raise
    pubmed_search's UserWarning, so the suite's output would not be clean.
    A placeholder rather than a real address: the value reaches NCBI when
    recording, and ``filter_query_parameters`` keeps it out of the cassette.
    """
    monkeypatch.setenv("ENTREZ_EMAIL", "pipeline-tests@example.org")
    monkeypatch.setattr("pipeline.pubmed_search._entrez_configured", False)


# --- the measurement -------------------------------------------------------
#
# Every assertion below is a recorded number, not a threshold. Each carries
# the reason it holds, in the style of `_RECALL_BASELINE` in
# test_extraction_golden.py: re-measure and update when the query changes on
# purpose; do not edit a figure to turn a red run green.

# SVD_QUERY retrieves 173 of the 178 papers this dashboard cites. It was 18
# of 29 before the MeSH branch: the two Title/Abstract branches both AND on
# the phrase "cerebral small vessel disease", so a paper that never writes it
# out was unreachable however many genetic or marker terms were added.
#
# The gold set was 28 until run 6 added PMID 42650130 to NOTCH3's references
# -- a CADASIL case report that names neither the phrase nor a genetic term
# in its title or abstract, so the MeSH branch is what reaches it. Re-measured
# against live PubMed, not adjusted to pass: the anchor-only figure was
# unchanged at 18 and the branch recovered six papers rather than five.
#
# It was 29 until the first 30-day run added 42607872 to COL4A1/2 and
# 42621038 / 42626038 to APOE, taking it to 32. The 365-day run then took it
# to 111: 795 papers, sixteen new genes and ~79 new references. Re-measured
# against live PubMed rather than adjusted -- and the striking part is that
# **the missed set did not change at all**. Every one of the new references
# is reachable, so recall rose from 27/32 to 106/111 while the same five
# papers stayed out; the anchor-only figure moved 21 -> 74 with the
# denominator. A query that held its five known blind spots across a
# 3.5x larger gold set is the strongest evidence this suite has produced
# that those five are properties of the papers rather than of the query.
#
# On 2026-11-03 the gene table was reset to its curated 31 August baseline and
# two years of papers were re-extracted under Claude Opus 5.5 with prompt v7
# (1,533 of 1,570 extracted; 37 refused by the bio classifier), taking the gold
# set to 178. Re-measured against live PubMed: 173/178, and again the same five
# missed. The anchor-only figure moved 74 -> 113, so the MeSH branch now
# recovers 60 papers.
#
# Re-recorded against live PubMed each time: the committed cassettes are
# POSTed bodies, which VCR's default matchers do not compare, so a stale one
# answers a changed gold set with the old response and the new PMIDs read as
# missed. `test_the_recorded_recall_request_carries_the_current_query` is
# what catches that.
#
# Of the five still missed:
#   23649698  CADASIL, no genetic term in title or abstract. Reachable only
#             by dropping the genetics gate on the MeSH branch, which costs
#             ~1,476 extra papers a year for this one -- measured, rejected.
#   36180795  GIGASTROKE. Recorded in CLAUDE.md as out of the prompt's scope.
#   39805841  one of the digit-recovered references.
#   30651383, 37063705  carry neither the phrase nor a cSVD MeSH heading.
_SVD_QUERY_RECALL: Final[int] = 173
_MISSED: Final[frozenset[str]] = frozenset(
    {
        "23649698",
        "30651383",
        "36180795",
        "37063705",
        "39805841",
    }
)


@pytest.mark.vcr
async def test_pubmed_returns_every_gold_pmid_when_asked_by_uid() -> None:
    """The control. Prove the instrument before trusting the measurement.

    A `[uid]` disjunction should return exactly the PMIDs it names. If any go
    missing -- withdrawn, merged into another record, or simply absent -- then
    a low recall figure is an artefact of the denominator rather than a
    property of the query, and every number below it is meaningless.
    """
    gold = gold_pmids()
    returned = await esearch_pmids(uid_clause(gold), retmax=len(gold))
    assert returned == frozenset(gold), (
        "PubMed did not return every gold PMID asked for by uid; the recall "
        "denominator is wrong, so the figures below are not measurements."
    )


@pytest.mark.vcr
async def test_svd_query_recall_matches_the_recorded_baseline() -> None:
    """Pin what the production query actually retrieves."""
    gold = gold_pmids()
    result = await measure_recall(SVD_QUERY, gold)

    assert result.total == 178, f"gold set changed: {result.total} PMIDs, was 178"
    assert len(result.matched) == _SVD_QUERY_RECALL, (
        f"SVD_QUERY recall moved to {len(result.matched)}/{result.total}, "
        f"was {_SVD_QUERY_RECALL}/178. If the query changed on purpose, "
        "re-measure and update _SVD_QUERY_RECALL and _MISSED together."
    )
    assert result.missed == _MISSED


def _recorded_terms(cassette: str) -> list[str]:
    """Every ``term`` a committed cassette recorded, in request order.

    Biopython posts once a query grows past a length threshold and gets
    below it, so the term is a body parameter for the long requests and a
    URL parameter for the short ones. Both are read here, because which
    form a query takes is a property of its length and not of the test.
    """
    import urllib.parse

    import yaml

    path = _CASSETTES / "test_query_recall" / cassette
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    terms: list[str] = []
    for interaction in loaded["interactions"]:
        request = interaction["request"]
        body = request.get("body")
        raw = body if body else urllib.parse.urlsplit(request["uri"]).query
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        terms.append(urllib.parse.parse_qs(text)["term"][0])
    return terms


# The two recall tests above and below replay a cassette, and VCR's default
# matchers -- method, scheme, host, port, path, query -- never read a request
# *body*. `vcr_config` sets no `match_on`, so the long queries these tests
# send, which Biopython posts, are matched on nothing but "a POST to
# esearch.fcgi": any query at all replays the recorded 24-of-29 answer.
# Widening MESH_TERMS, GENETIC_TERMS or MARKER_TERMS therefore moves nothing
# in a replayed run, and the recorded figures would go on being reported for
# a query that no longer produced them.
#
# The two tests below close that offline, by asserting that the query the
# code builds today is byte-for-byte the query the recording was made with.
# They need no network and no re-recording: if the query changes, they fail
# and the cassettes have to be re-recorded, which is the same instruction
# `_SVD_QUERY_RECALL` already carries.


def test_the_recorded_recall_request_carries_the_current_query() -> None:
    """The baseline cassette answers the query the code builds now."""
    (term,) = _recorded_terms(
        "test_svd_query_recall_matches_the_recorded_baseline.yaml"
    )
    assert term == f"({SVD_QUERY}) AND ({uid_clause(gold_pmids())})", (
        "SVD_QUERY or the gold set changed since the recall cassette was "
        "recorded, so the replayed 24/29 is a figure for a query the "
        "pipeline no longer sends. Re-record: rm "
        "tests/pipeline/cassettes/test_query_recall/"
        "test_svd_query_recall_matches_the_recorded_baseline.yaml and run "
        "this module with --record-mode=once."
    )


@pytest.mark.vcr
async def test_the_anchor_still_caps_the_title_abstract_branches() -> None:
    """Widening the term lists cannot move recall; only the MeSH branch can.

    Both Title/Abstract branches are `AND`-ed with the disease anchor, so no
    paper lacking that exact phrase is reachable through either of them
    however many genetic or marker terms are added. Those lists are precision
    filters. This pins the ceiling, so widening one of them in the belief that
    it improves recall fails loudly instead of passing quietly.

    The papers the MeSH branch recovers are exactly the gap between this
    number and the full query's. It was six against a 32-paper gold set and
    was 32 against a 111-paper one and is 60 against a 178-paper one -- the
    branch is worth far more than the small set suggested, because a gold set
    of that size was mostly papers that name the phrase outright.
    """
    gold = gold_pmids()
    anchor = " OR ".join(f'"{term}"[Title/Abstract]' for term in DISEASE_TERMS)

    anchor_only = await measure_recall(anchor, gold)
    full = await measure_recall(SVD_QUERY, gold)

    assert len(anchor_only.matched) == 113
    assert anchor_only.matched < full.matched
    assert len(full.matched - anchor_only.matched) == 60


def test_the_recorded_anchor_requests_carry_the_current_queries() -> None:
    """The anchor cassette answers both queries the code builds now.

    The anchor-only request is short enough to go out as a GET, so VCR
    matches it on its query string and DISEASE_TERMS is guarded either way.
    The full-query request is the posted one, and the gap of six papers is
    the difference between the two -- so a widened term list would move the
    recorded difference and nothing would say so.
    """
    anchor = " OR ".join(f'"{term}"[Title/Abstract]' for term in DISEASE_TERMS)
    uids = uid_clause(gold_pmids())
    recorded = _recorded_terms(
        "test_the_anchor_still_caps_the_title_abstract_branches.yaml"
    )
    assert recorded == [f"({anchor}) AND ({uids})", f"({SVD_QUERY}) AND ({uids})"], (
        "the anchor comparison's cassette was recorded with different "
        "queries; re-record it before trusting the 18/24 gap."
    )


def test_the_mesh_branch_is_gated_on_the_genetic_terms() -> None:
    """A bare ``OR (MeSH)`` would quadruple ingestion to gain one paper.

    ``"White Matter"[MeSH]`` is an anatomical heading. Ungated it pulls in the
    multiple-sclerosis and neuroimaging literature: 2,354 papers a year
    against 878 for the gated form, and the difference buys exactly one more
    gold paper. The gate is the whole reason this branch is affordable, so it
    is asserted on the rendered query rather than left to a comment.

    Structural rather than measured -- it needs no network and fails the
    moment someone edits ``_build_query`` to drop the ``AND``.
    """
    mesh_clause = " OR ".join(f'"{term}"[MeSH Terms]' for term in MESH_TERMS)
    research_clause = " OR ".join(f'"{term}"[Title/Abstract]' for term in GENETIC_TERMS)

    assert mesh_clause in SVD_QUERY
    assert f"(({mesh_clause}) AND ({research_clause}))" in SVD_QUERY
