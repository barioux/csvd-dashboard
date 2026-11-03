"""scripts/reset_to_baseline.py against a real PostgreSQL.

The reset deletes and rewrites the curated table, so what it promises is
stated in the export's terms: afterwards the export publishes the baseline and
nothing else, the ledger is empty, the removed genes' annotations are gone,
and a failure leaves the database exactly as it was. These run against the
`database_env` fixture, like the restore's integration tests, and start from
empty tables for the same reason (see that module's docstring).
"""

import json
import os
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from scripts.reset_to_baseline import (
    build_reset,
    current_format,
    pending_changes,
    reset_database,
    run,
)
from scripts.restore_from_json import build_plan, write_plan

from pipeline.database import Database
from pipeline.export.main import read_published_genes, run_export

pytestmark = pytest.mark.usefixtures("database_env")

_TABLES = (
    "genes",
    "clinical_trials",
    "pubmed_refs",
    "ncbi_gene_info",
    "uniprot_info",
    "pubmed_citations",
    "pipeline_runs",
    "sync_runs",
    "gene_annotations",
    "gene_annotation_status",
    "trial_drug_annotations",
)


async def _truncate() -> None:
    async with Database.connection() as conn:
        await conn.execute(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")


@pytest.fixture(autouse=True)
async def _empty_tables(database_env):
    # Requests database_env itself; see the restore integration tests.
    await _truncate()
    yield
    await _truncate()
    await Database.close()


def _today(gene: str, **changes: Any) -> dict[str, Any]:
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
        "references": ["(reference needed)"],
        "sourceQuote": "(not yet extracted)",
        "confidence": None,
        **changes,
    }


def _august(gene: str, **changes: Any) -> dict[str, Any]:
    row = _today(gene, protein=gene)
    del row["sourceQuote"], row["confidence"]
    return {**row, **changes}


# The curated table before any extraction, in the 31-August export's shape.
BASELINE = [
    _august("ABC1", references=["11111111"]),
    _august("DEF2", protein="DEF protein two", affectedPathway="angiogenesis"),
]

# What extraction made of it: both grown, and one gene it inserted.
_CURRENT = [
    _today(
        "ABC1",
        protein="ABC protein one",
        mendelianRandomization="Yes",
        evidenceFromOtherOmicsStudies=["TWAS;brain"],
        references=["11111111", "22222222"],
        sourceQuote="ABC1 was associated with the trait.",
        confidence=0.8,
    ),
    _today(
        "DEF2",
        protein="DEF protein two",
        affectedPathway="angiogenesis",
        references=["33333333"],
        sourceQuote="DEF2 was associated with the trait.",
        confidence=0.6,
    ),
    _today(
        "XYZ9",
        references=["44444444"],
        sourceQuote="XYZ9 was associated with the trait.",
        confidence=0.7,
    ),
]


async def _seed_current(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    files: dict[str, Any] = {
        "table1.json": _CURRENT,
        "table2.json": [],
        "gene_info.json": [],
        "gene_info_table2.json": [],
        "protein_info.json": [],
        "refs.json": [],
        "pipeline_run.json": None,
        "pipeline_status.json": None,
        "pipeline_syncs.json": [],
    }
    for name, value in files.items():
        (data / name).write_text(json.dumps(value), encoding="utf-8")
    await write_plan(build_plan(data, tmp_path / "no-logs"))
    async with Database.connection() as conn:
        await conn.executemany(
            "INSERT INTO pubmed_refs (pmid) VALUES ($1)", [("22222222",), ("33333333",)]
        )
        for symbol in ("ABC1", "XYZ9"):
            await conn.execute(
                "INSERT INTO gene_annotations (gene_symbol, source, relation, "
                "object_id) VALUES ($1, 'clinvar', 'disease', 'OMIM:100000')",
                symbol,
            )
            await conn.execute(
                "INSERT INTO gene_annotation_status (gene_symbol, source, row_count) "
                "VALUES ($1, 'clinvar', 1)",
                symbol,
            )


async def _symbols(table: str) -> list[str]:
    async with Database.connection() as conn:
        rows = await conn.fetch(
            f"SELECT DISTINCT gene_symbol FROM {table} ORDER BY gene_symbol"
        )
    return [row["gene_symbol"] for row in rows]


async def _reset() -> dict[str, int]:
    return await reset_database(build_reset(BASELINE, await read_published_genes()))


async def test_reset_exports_exactly_the_baseline(tmp_path: Path) -> None:
    await _seed_current(tmp_path)
    await _reset()
    out = tmp_path / "export"
    await run_export(out)

    exported = json.loads((out / "table1.json").read_text(encoding="utf-8"))
    assert exported == [current_format(row) for row in BASELINE]


async def test_reset_empties_the_ledger_and_the_removed_genes_annotations(
    tmp_path: Path,
) -> None:
    await _seed_current(tmp_path)
    counts = await _reset()

    assert counts["pubmed_refs"] == 0
    assert counts["genes"] == len(BASELINE)
    assert await _symbols("gene_annotations") == ["ABC1"]
    assert await _symbols("gene_annotation_status") == ["ABC1"]


async def test_the_next_gene_after_a_reset_takes_the_next_id(tmp_path: Path) -> None:
    await _seed_current(tmp_path)
    await _reset()
    async with Database.connection() as conn:
        new_id = await conn.fetchval(
            "INSERT INTO genes (gene) VALUES ('NEW1') RETURNING id"
        )
    assert new_id == len(BASELINE) + 1


async def test_a_second_reset_changes_nothing(tmp_path: Path) -> None:
    await _seed_current(tmp_path)
    for out in ("first", "second"):
        await _reset()
        await run_export(tmp_path / out)

    for name in ("table1.json", "refs.json", "gene_annotations.json"):
        first, second = tmp_path / "first" / name, tmp_path / "second" / name
        assert first.exists() == second.exists()
        if first.exists():
            assert first.read_bytes() == second.read_bytes()


async def test_a_failed_reset_leaves_everything_as_it_was(tmp_path: Path) -> None:
    await _seed_current(tmp_path)
    before = await pending_changes({"ABC1"})
    genes = build_reset(BASELINE, await read_published_genes())
    genes[-1].columns["gene"] = "X" * 101  # genes.gene is VARCHAR(100)

    with pytest.raises(asyncpg.StringDataRightTruncationError):
        await reset_database(genes)

    assert await pending_changes({"ABC1"}) == before
    assert await _symbols("gene_annotations") == ["ABC1", "XYZ9"]


async def test_without_write_it_names_the_environment_target_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await _seed_current(tmp_path)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(BASELINE), encoding="utf-8")
    before = await pending_changes({"ABC1"})

    await run(baseline, write=False)

    out = capsys.readouterr().out
    assert f"Target: {os.environ['DB_USER']}@{os.environ['DB_HOST']}" in out
    assert "Nothing written" in out
    assert await pending_changes({"ABC1"}) == before
