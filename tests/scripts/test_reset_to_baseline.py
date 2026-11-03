"""scripts/reset_to_baseline.py's checks, without a database.

The reset throws away every extracted value and keeps a curated baseline, so
what has to hold before it writes is that the baseline reproduces itself
through the export and that the live table differs from it only by what
extraction adds. These tests pin both, on synthetic genes.
"""

from typing import Any

import pytest
from scripts.reset_to_baseline import (
    ResetRefused,
    additions_only,
    build_reset,
    current_format,
)


def _today(gene: str = "ABC1", **changes: Any) -> dict[str, Any]:
    """A table1.json row in today's wire format, every field at its sentinel."""
    return {
        "gene": gene,
        "protein": "(unknown)",
        "chromosomalLocation": "(unknown)",
        "gwasTrait": ["(none found)"],
        "mendelianRandomization": "No",
        "evidenceFromOtherOmicsStudies": ["(none found)"],
        "linkToMonogenicDisease": ["(none found)"],
        "brainCellTypes": "(unknown)",
        "affectedPathway": "(unknown)",
        "references": ["11111111"],
        "sourceQuote": "(not yet extracted)",
        "confidence": None,
        **changes,
    }


def _august(gene: str = "ABC1", **changes: Any) -> dict[str, Any]:
    """The same row as the export wrote it on 31 August 2026: no provenance
    columns yet, and a missing protein published as the gene symbol."""
    row = _today(gene, protein=gene)
    del row["sourceQuote"], row["confidence"]
    return {**row, **changes}


def test_current_format_appends_the_provenance_sentinels_last() -> None:
    row = current_format(_august())
    assert list(row)[-2:] == ["sourceQuote", "confidence"]
    assert (row["sourceQuote"], row["confidence"]) == ("(not yet extracted)", None)


def test_current_format_maps_only_an_exact_symbol_placeholder() -> None:
    assert current_format(_august())["protein"] == "(unknown)"
    assert current_format(_august(protein="Abc1"))["protein"] == "Abc1"


def test_current_format_is_idempotent() -> None:
    once = current_format(_august())
    assert current_format(once) == once


def test_additions_only_accepts_pipeline_style_growth() -> None:
    grown = _today(
        references=["11111111", "22222222"],
        gwasTrait=["T1"],
        evidenceFromOtherOmicsStudies=["TWAS"],
        mendelianRandomization="Yes",
        protein="A protein",
        sourceQuote="A sentence.",
        confidence=0.7,
    )
    assert additions_only(_today(), grown) == []


def test_additions_only_accepts_a_sentinel_replaced_by_values() -> None:
    assert (
        additions_only(
            _today(references=["(reference needed)"]),
            _today(references=["22222222"]),
        )
        == []
    )


def test_additions_only_names_each_curated_value_that_moved() -> None:
    base = _today(
        mendelianRandomization="Yes",
        protein="A protein",
        affectedPathway="angiogenesis",
    )
    # In the row's own key order.
    assert additions_only(base, _today(references=[])) == [
        "protein",
        "mendelianRandomization",
        "affectedPathway",
        "references",
    ]


def test_build_reset_returns_rows_in_baseline_order() -> None:
    genes = build_reset(
        [_august("DEF2"), _august("ABC1")], [_today("ABC1"), _today("DEF2")]
    )
    assert [g.columns["gene"] for g in genes] == ["DEF2", "ABC1"]


def test_build_reset_lists_every_failure_at_once() -> None:
    with pytest.raises(ResetRefused) as refused:
        build_reset(
            [_august("ABC1"), _august("DEF2", affectedPathway="x")],
            [_today("ABC1", references=[]), _today("DEF2")],
        )
    assert "ABC1: changed: references" in str(refused.value)
    assert "DEF2: changed: affectedPathway" in str(refused.value)


def test_build_reset_refuses_a_gene_missing_from_the_table() -> None:
    with pytest.raises(ResetRefused, match="DEF2: not in the current table"):
        build_reset([_august("DEF2")], [_today("ABC1")])


def test_build_reset_refuses_a_baseline_row_carrying_an_extraction() -> None:
    with pytest.raises(ResetRefused, match="ABC1: carries an extraction"):
        build_reset(
            [_august(sourceQuote="A sentence.", confidence=0.9)],
            [_today(sourceQuote="A sentence.", confidence=0.9)],
        )


def test_build_reset_refuses_duplicate_baseline_symbols() -> None:
    with pytest.raises(ResetRefused, match="ABC1: duplicate in the baseline"):
        build_reset([_august(), _august()], [_today()])


def test_build_reset_refuses_a_row_that_does_not_round_trip() -> None:
    # An empty pathway exports as "(unknown)", not "" (checked 2026-11-02).
    with pytest.raises(
        ResetRefused, match="ABC1: does not round-trip: affectedPathway"
    ):
        build_reset([_august(affectedPathway="")], [_today(affectedPathway="")])
