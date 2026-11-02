"""Unit tests for scripts/restore_from_json.py.

`testpaths` is `["tests/pipeline"]`, so nothing collects this file -- in CI
either -- unless it is named: `uv run pytest tests/scripts`. The restore's
contract against a real database -- that the export of what it wrote
reproduces the committed files byte for byte -- is
`tests/pipeline/test_restore_from_json_integration.py`, because the
`database_env` fixture lives in `tests/pipeline/conftest.py`.
"""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from scripts.restore_from_json import (
    GeneRestore,
    RestoreRefused,
    build_plan,
    gene_from_wire,
    ledger_from_logs,
    stored_omics,
    trial_from_wire,
)

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

_POPULATED_GENE: dict[str, Any] = {
    "gene": "ABC1",
    "protein": "ABC protein one",
    "chromosomalLocation": "1p36.3",
    "gwasTrait": ["TRAIT-A", "TRAIT-B"],
    "mendelianRandomization": "Yes",
    "evidenceFromOtherOmicsStudies": ["TWAS;brain"],
    "linkToMonogenicDisease": ["123456"],
    "brainCellTypes": "endothelial cells",
    "affectedPathway": "angiogenesis",
    "references": ["11111111", "22222222"],
    "sourceQuote": "ABC1 was associated with the trait.",
    "confidence": 0.8,
}

_SENTINEL_GENE: dict[str, Any] = {
    "gene": "XYZ9",
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
}

_TRIAL: dict[str, Any] = {
    "drug": "Drug A",
    "mechanismOfAction": "(unknown)",
    "geneticTarget": "(none)",
    "geneticEvidence": "No",
    "trialName": "(unknown)",
    "registryId": "NCT00000001",
    "clinicalTrialPhase": "2",
    "targetPopulation": "Population A",
    "targetPopulationDetails": "(unknown)",
    "targetSampleSize": "120",
    "estimatedCompletionDate": "Completed (unpublished)",
    "primaryOutcome": "(unknown)",
    "sponsorType": "(unknown)",
    "overallStatus": "(unknown)",
}


def _write_export(
    directory: Path,
    *,
    table1: list[dict[str, Any]] | None = None,
    **files: Any,
) -> Path:
    """A data/ directory shaped like `deno task data:empty`'s output."""
    contents: dict[str, Any] = {
        "table1.json": table1 or [],
        "table2.json": [],
        "gene_info.json": [],
        "gene_info_table2.json": [],
        "protein_info.json": [],
        "refs.json": [],
        "pipeline_run.json": None,
        "pipeline_status.json": None,
        "pipeline_syncs.json": [],
    }
    contents.update({f"{name}.json": value for name, value in files.items()})
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in contents.items():
        (directory / name).write_text(json.dumps(value), encoding="utf-8")
    return directory


# ------------------------------------------------------------------ omics


def test_an_omics_detail_goes_back_behind_a_colon() -> None:
    """The export turns ":" into ";" and splits studies on ";" first, so a
    ";" stored back verbatim would come out as two studies."""
    assert stored_omics(["TWAS;brain", "EWAS"]) == "TWAS:brain, EWAS"


def test_a_detail_with_a_comma_goes_back_in_parentheses() -> None:
    """The export splits on commas outside parentheses; the parenthetical is
    the stored form it rebuilds "Proteomics;plasma, CSF" from."""
    assert stored_omics(["Proteomics;plasma, CSF"]) == "Proteomics (plasma, CSF)"


def test_the_omics_sentinel_is_not_stored() -> None:
    assert stored_omics(["(none found)"]) is None


# ------------------------------------------------------------------ genes


def test_a_populated_gene_keeps_its_values() -> None:
    assert gene_from_wire(_POPULATED_GENE) == GeneRestore(
        columns={
            "protein": "ABC protein one",
            "gene": "ABC1",
            "chromosomal_location": "1p36.3",
            "mendelian_randomization": True,
            "evidence_from_other_omics_studies": "TWAS:brain",
            "brain_cell_types": "endothelial cells",
            "affected_pathway": "angiogenesis",
            "source_quote": "ABC1 was associated with the trait.",
            "confidence": 0.8,
        },
        traits=["TRAIT-A", "TRAIT-B"],
        links=["123456"],
        references=["11111111", "22222222"],
    )


def test_gene_sentinels_are_never_stored() -> None:
    """The export manufactures every sentinel from NULL or zero list rows;
    storing one would publish it as a value -- a trait named
    "(none found)", a reference named "(reference needed)"."""
    assert gene_from_wire(_SENTINEL_GENE) == GeneRestore(
        columns={
            "protein": None,
            "gene": "XYZ9",
            "chromosomal_location": None,
            "mendelian_randomization": False,
            "evidence_from_other_omics_studies": None,
            "brain_cell_types": None,
            "affected_pathway": None,
            "source_quote": None,
            "confidence": None,
        },
        traits=[],
        links=[],
        references=[],
    )


# ----------------------------------------------------------------- trials


def test_trial_sentinels_become_null_and_the_sample_size_an_integer() -> None:
    assert trial_from_wire(_TRIAL) == {
        "drug": "Drug A",
        "mechanism_of_action": None,
        "genetic_target": None,
        "genetic_evidence": "No",
        "trial_name": None,
        "registry_id": "NCT00000001",
        "clinical_trial_phase": "2",
        "target_population": "Population A",
        "target_population_details": None,
        "target_sample_size": 120,
        # The export's 23-character label does not fit VARCHAR(20).
        "estimated_completion_date": "Completed, unpublish",
        "primary_outcome": None,
        "sponsor_type": None,
        "overall_status": None,
    }


def test_an_unknown_sample_size_is_null() -> None:
    row = trial_from_wire({**_TRIAL, "targetSampleSize": "(unknown)"})
    assert row["target_sample_size"] is None


# ------------------------------------------------------------ the plan


def test_every_committed_row_round_trips_through_the_export() -> None:
    """The guard build_plan applies, on the data this repository ships: an
    inverse that missed one export step would raise RestoreRefused here."""
    plan = build_plan(_DATA_DIR, Path("/nonexistent-logs"))

    table1 = json.loads((_DATA_DIR / "table1.json").read_text(encoding="utf-8"))
    table2 = json.loads((_DATA_DIR / "table2.json").read_text(encoding="utf-8"))
    assert len(plan.genes) == len(table1)
    assert len(plan.trials) == len(table2)


def test_a_row_the_export_could_not_have_written_is_refused(tmp_path: Path) -> None:
    """The export lowercases the pathway, so a capitalised one cannot be
    restored faithfully -- re-exporting it would change the file."""
    data = _write_export(
        tmp_path / "data",
        table1=[{**_POPULATED_GENE, "affectedPathway": "Angiogenesis"}],
    )

    with pytest.raises(RestoreRefused, match=r"ABC1.*affectedPathway"):
        build_plan(data, tmp_path / "logs")


def test_the_empty_export_plans_nothing(tmp_path: Path) -> None:
    plan = build_plan(_write_export(tmp_path / "data"), tmp_path / "logs")

    assert plan.genes == []
    assert plan.trials == []
    assert plan.ncbi == []
    assert plan.uniprot == []
    assert plan.citations == []
    assert plan.ledger == []
    assert plan.runs == []
    assert plan.syncs == []


_REPORT_STUB: dict[str, Any] = {
    "runMode": "standard",
    "durationSeconds": 12.5,
    "papers": {"processed": 5},
}


def test_one_run_in_both_run_files_is_one_row(tmp_path: Path) -> None:
    """pipeline_status.json carries the counts at second precision and
    pipeline_run.json the report at microseconds; they are the same run."""
    run = {"runTimestamp": "2026-01-02T03:04:05.678000+00:00", "status": "completed",
           **_REPORT_STUB}
    status = {"runTimestamp": "2026-01-02T03:04:05Z", "papersProcessed": 5,
              "fulltextRetrieved": 3, "genesExtracted": 2, "genesValidated": 1}
    data = _write_export(tmp_path / "data", pipeline_run=run, pipeline_status=status)

    plan = build_plan(data, tmp_path / "logs")

    assert plan.runs == [
        (datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC), 5, 3, 2, 1,
         "standard", "completed", 12.5, run)
    ]


def test_a_failed_newest_run_and_the_last_good_one_are_two_rows(
    tmp_path: Path,
) -> None:
    """A failed run publishes its report but does not date the data, so the
    two files then describe different runs; one row would lose one of them
    -- the widget's failure, or the About page's date."""
    run = {"runTimestamp": "2026-01-02T00:00:00+00:00", "status": "failed",
           **_REPORT_STUB}
    status = {"runTimestamp": "2026-01-01T00:00:00Z", "papersProcessed": 5,
              "fulltextRetrieved": 3, "genesExtracted": 2, "genesValidated": 1}
    data = _write_export(tmp_path / "data", pipeline_run=run, pipeline_status=status)

    plan = build_plan(data, tmp_path / "logs")

    assert plan.runs == [
        (datetime(2026, 1, 1, tzinfo=UTC), 5, 3, 2, 1, "standard", None, None, None),
        (datetime(2026, 1, 2, tzinfo=UTC), 0, 0, 0, 0, "standard", "failed", 12.5, run),
    ]


def test_lookup_fallbacks_are_not_restored_as_cache_rows(tmp_path: Path) -> None:
    """A fallback is the export saying "the cache had nothing". Storing it
    would make the cache claim an answer, and the sync would never fetch
    the real one."""
    empty_gene = {
        "name": "NOPE1", "uid": None, "description": None, "otheraliases": None
    }
    known = {
        "name": "ABC1", "uid": "42", "description": "abc one", "otheraliases": "ABC"
    }
    data = _write_export(
        tmp_path / "data",
        gene_info=[known, empty_gene],
        # The same symbol in both NCBI files is one cache row.
        gene_info_table2=[known],
        protein_info=[
            {"gene": "ABC1", "accession": "P12345", "url": "https://u/P12345"},
            {"gene": "NOPE1", "accession": None, "url": None},
        ],
        refs=[
            {
                "pmid": "11111111",
                "authors": "Doe J",
                "title": "A title",
                "journal": "J",
                "publicationDate": "2020",
                "doi": "10.1/x",
                "formattedRef": "Doe J. A title. J. 2020.",
            },
            {
                "pmid": "22222222",
                "authors": None,
                "title": None,
                "journal": None,
                "publicationDate": None,
                "doi": None,
                "formattedRef": "PMID: 22222222 (citation not available)",
            },
        ],
    )

    plan = build_plan(data, tmp_path / "logs")

    assert plan.ncbi == [("ABC1", "42", "abc one", "ABC")]
    assert plan.uniprot == [("ABC1", "P12345", "https://u/P12345")]
    assert plan.citations == [
        (
            "11111111",
            "Doe J",
            "A title",
            "J",
            "2020",
            "10.1/x",
            "Doe J. A title. J. 2020.",
        )
    ]


# ---------------------------------------------------------- the PMID ledger


def _paper(pmid: str, *, source: str = "pmc", fulltext: bool = True,
           genes: int = 0, error: str | None = None) -> dict[str, Any]:
    return {
        "pmid": pmid,
        "fulltext": fulltext,
        "source": source,
        "error": error,
        "gene_count": genes,
        "genes": [],
        "rejected_genes": [],
        "processing_time": 1.0,
    }


def _report(stamp: str, papers: list[dict[str, Any]], *, merged: bool = True,
            **config: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "timestamp": stamp,
        "pipeline_config": config,
        "papers_detail": papers,
    }
    if merged:
        report["database"] = {"inserted": 0, "updated": 0}
    return report


def _write_events(path: Path, *reports: dict[str, Any]) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    con.execute(
        "INSERT INTO events (event_type, payload, created_at) VALUES (?, ?, ?)",
        ("paper_extraction_thinking", json.dumps({"pmid": "99999999"}), "x"),
    )
    for report in reports:
        con.execute(
            "INSERT INTO events (event_type, payload, created_at) VALUES (?, ?, ?)",
            ("pipeline_completed", json.dumps(report), report["timestamp"]),
        )
    con.commit()
    con.close()


def test_the_ledger_replays_the_merged_runs_oldest_first(tmp_path: Path) -> None:
    """record_processed_pmids_batch upserts, so the last run to process a
    paper is what pubmed_refs held. Only the online merge writes it: an
    offline --pmids run, a dry run and a failed paper never did."""
    logs = tmp_path / "logs"
    (logs / "json").mkdir(parents=True)
    first = _report(
        "2026-01-01T00:00:00+00:00",
        [_paper("10000001", genes=2), _paper("10000002", error="timeout")],
        dry_run=False,
    )
    (logs / "json" / "pipeline_report_a.json").write_text(json.dumps(first))
    _write_events(
        logs / "events.db",
        first,  # the same run, also recorded as an event: counted once
        _report(
            "2026-01-02T00:00:00+00:00",
            [_paper("10000001", source="europepmc", fulltext=False, genes=3)],
            dry_run=False,
        ),
        _report(
            "2026-01-03T00:00:00+00:00",
            [_paper("10000003")],
            merged=False,
            mode="pmid_list",
        ),
        _report("2026-01-04T00:00:00+00:00", [_paper("10000004")], dry_run=True),
        _report("2026-01-05T00:00:00+00:00", [_paper("10000005")], test_mode=True),
    )

    assert ledger_from_logs(logs) == [
        ("10000001", datetime(2026, 1, 2, tzinfo=UTC), False, "europepmc", 3)
    ]


def test_no_logs_is_an_empty_ledger(tmp_path: Path) -> None:
    """logs/ is gitignored, so CI and a fresh clone have none."""
    assert ledger_from_logs(tmp_path / "absent") == []
