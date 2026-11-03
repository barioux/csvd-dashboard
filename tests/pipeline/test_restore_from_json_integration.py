"""scripts/restore_from_json.py against a real PostgreSQL.

The restore exists for the day the database is gone and `data/*.json` is the
only copy left, so its contract is the export's: what it writes must export
back to the committed files byte for byte. A mocked connection cannot say
that -- the first live restore failed on a `VARCHAR(20)` the export's own
label does not fit -- so these run against the `database_env` fixture, which
is why they live here rather than beside the script's unit tests in
`tests/scripts/`.

The throwaway database is shared by the whole session and the other
integration tests leave `pipeline_runs` rows behind, so every test here
starts from empty tables. Never point `CSVD_TEST_DB_URL` at a database you
want to keep; that is true of every test that uses it.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from scripts.restore_from_json import RestoreRefused, build_plan, write_plan

from pipeline.cache_utils import DB_CACHE_TTL_DAYS
from pipeline.database import Database, get_cached_uniprot_info
from pipeline.export.lookups import read_uniprot_info
from pipeline.export.main import read_published_genes, run_export

pytestmark = pytest.mark.usefixtures("database_env")

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

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


async def _count(table: str) -> int:
    async with Database.connection() as conn:
        return await conn.fetchval(f"SELECT count(*) FROM {table}")


@pytest.fixture(autouse=True)
async def _empty_tables(database_env):
    # Requests database_env itself: autouse fixtures are set up before the
    # module's usefixtures mark, so without it this connects with the
    # credentials _isolate_credentials has just cleared.
    await _truncate()
    yield
    await _truncate()
    await Database.close()


def _one_gene_export(directory: Path, **trial: Any) -> Path:
    gene = {
        "gene": "ABC1",
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
    }
    trials = []
    if trial:
        trials = [
            {
                "drug": "Drug A",
                "mechanismOfAction": "(unknown)",
                "geneticTarget": "(none)",
                "geneticEvidence": "No",
                "trialName": "(unknown)",
                "registryId": "NCT00000001",
                "clinicalTrialPhase": "2",
                "targetPopulation": "Population A",
                "targetPopulationDetails": "(unknown)",
                "targetSampleSize": "(unknown)",
                "estimatedCompletionDate": "(unknown)",
                "primaryOutcome": "(unknown)",
                "sponsorType": "(unknown)",
                "overallStatus": "(unknown)",
                **trial,
            }
        ]
    files: dict[str, Any] = {
        "table1.json": [gene],
        "table2.json": trials,
        "gene_info.json": [],
        "gene_info_table2.json": [],
        "protein_info.json": [],
        "refs.json": [],
        "pipeline_run.json": None,
        "pipeline_status.json": None,
        "pipeline_syncs.json": [],
    }
    directory.mkdir(parents=True)
    for name, value in files.items():
        (directory / name).write_text(json.dumps(value), encoding="utf-8")
    return directory


async def test_a_restored_database_exports_the_committed_files_byte_for_byte(
    tmp_path: Path,
) -> None:
    if not json.loads((_DATA_DIR / "table1.json").read_text(encoding="utf-8")):
        pytest.skip("data/table1.json is empty -- nothing committed to restore")

    await write_plan(build_plan(_DATA_DIR, tmp_path / "no-logs"))
    out = tmp_path / "export"
    await run_export(out)

    produced = sorted(path.name for path in out.glob("*.json"))
    assert {"table1.json", "table2.json", "refs.json", "pipeline_run.json"} <= set(
        produced
    )
    differing = [
        name
        for name in produced
        if (out / name).read_bytes() != (_DATA_DIR / name).read_bytes()
    ]
    assert differing == []


async def test_read_published_genes_is_what_the_export_writes(tmp_path: Path) -> None:
    """The reset compares the live table with a baseline in the published
    shape, so this reader has to be exactly what table1.json receives."""
    await write_plan(build_plan(_one_gene_export(tmp_path / "data"), tmp_path / "x"))
    out = tmp_path / "export"
    await run_export(out)

    published = json.loads((out / "table1.json").read_text(encoding="utf-8"))
    assert await read_published_genes() == published


async def test_a_database_holding_rows_is_refused_and_left_as_it_was(
    tmp_path: Path,
) -> None:
    """Restoring over live rows would collide on the explicit ids or, worse,
    succeed beside them; either way the result is no longer the export."""
    async with Database.connection() as conn:
        await conn.execute("INSERT INTO pubmed_refs (pmid) VALUES ('33333333')")
    plan = build_plan(_one_gene_export(tmp_path / "data"), tmp_path / "no-logs")

    with pytest.raises(RestoreRefused, match="pubmed_refs"):
        await write_plan(plan)

    assert await _count("genes") == 0
    assert await _count("pubmed_refs") == 1


async def test_the_next_insert_after_a_restore_does_not_collide(
    tmp_path: Path,
) -> None:
    """The restore writes explicit ids so the export's ORDER BY id keeps the
    committed row order; the sequences have to be moved past them, or the
    next --clinical-trials discovery fails on id 1."""
    await write_plan(
        build_plan(_one_gene_export(tmp_path / "data", drug="Drug A"), tmp_path / "x")
    )

    async with Database.connection() as conn:
        gene_id = await conn.fetchval(
            "INSERT INTO genes (gene) VALUES ('NEW1') RETURNING id"
        )
        trial_id = await conn.fetchval(
            "INSERT INTO clinical_trials (drug, registry_id) "
            "VALUES ('Drug B', 'NCT00000002') RETURNING id"
        )

    assert (gene_id, trial_id) == (2, 2)


async def test_a_restored_uniprot_row_is_refetched_by_the_next_sync(
    tmp_path: Path,
) -> None:
    """protein_info.json publishes a UniProt row's accession and url, not its
    name or GO terms, so the restore stores those NULL. The sync reads its
    cache with a TTL, and a row stamped with the restore's own time read as
    fresh for 30 days -- so --sync-external-data left those columns NULL
    until it aged out. The export reads with no TTL and must still see it."""
    url = "https://www.uniprot.org/uniprotkb/P12345"
    data = _one_gene_export(tmp_path / "data")
    (data / "protein_info.json").write_text(
        json.dumps([{"gene": "ABC1", "accession": "P12345", "url": url}])
    )
    await write_plan(build_plan(data, tmp_path / "no-logs"))

    assert await get_cached_uniprot_info(["ABC1"], max_age_days=DB_CACHE_TTL_DAYS) == {}
    assert await read_uniprot_info(["ABC1"]) == [
        {"gene": "ABC1", "accession": "P12345", "url": url}
    ]


async def test_a_write_that_fails_part_way_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    """genes is written before clinical_trials; a trial value too wide for
    its column must take the already-inserted gene down with it, or a
    second attempt is refused for the half-restore the first one left."""
    plan = build_plan(
        # 31 characters into clinical_trial_phase VARCHAR(20).
        _one_gene_export(
            tmp_path / "data", clinicalTrialPhase="Phase 2/3 randomised open-label"
        ),
        tmp_path / "no-logs",
    )

    with pytest.raises(Exception, match="value too long"):
        await write_plan(plan)

    assert await _count("genes") == 0
    assert await _count("gene_references") == 0
