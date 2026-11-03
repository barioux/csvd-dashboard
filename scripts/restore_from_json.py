"""Rebuild the database from the committed data/*.json.

For the day PostgreSQL is gone and no dump survives -- 2026-11-02, when a
Homebrew reinstall deleted /opt/homebrew/var/postgresql@18 and the formula's
post-install initdb'd an empty cluster in its place. A `pg_dump` is still the
backstop (the regenerate-data skill); this is the second one, and it only
holds while data/ is current: anything merged since the last export is not
in the files and does not come back.

    uv run python -m scripts.restore_from_json            # build and check only
    uv run python -m scripts.restore_from_json --write    # into an empty database

The database must exist and be at `alembic upgrade head`; `--write` refuses
if any table it writes already holds a row.

**Every row is checked before anything is written.** Each genes and
clinical_trials row is pushed back through the export's own
`clean_gene_row` / `clean_trial_row`, and the restore refuses unless the
result is the committed row exactly -- so an export step this module failed
to invert is a refusal, not a silently different dataset. Re-running
`run_export` against the result reproduces every file it writes byte for
byte (tests/pipeline/test_restore_from_json_integration.py).

What comes back, and from where:

- genes and its three join tables, from table1.json -- source_quote and
  confidence included, since both are published.
- clinical_trials, from table2.json: the curated rows only. The export never
  publishes an uncurated ClinicalTrials.gov discovery (`--clinical-trials`
  fetches those again) or a TERMINATED / WITHDRAWN trial, whose curator
  columns survive only in a table2.json committed before 1299cd7 -- in the
  pre-squash history the regenerate-data skill describes.
- The NCBI, UniProt and PubMed caches, from gene_info*.json, protein_info.json
  and refs.json. `ncbi_gene_info.map_location` and UniProt's GO columns are
  not published, so they stay NULL until `--sync-external-data`.
- pipeline_runs, from pipeline_run.json and pipeline_status.json -- only the
  runs those two files describe -- and sync_runs from pipeline_syncs.json.
- pubmed_refs, the processed-PMID ledger, from the run reports under the
  gitignored logs/ rather than from data/: logs/json/pipeline_report_*.json
  and the pipeline_completed events in logs/events.db. A paper processed by
  a run older than every surviving report is missing, and the next window
  that covers it re-extracts it -- unless a later run's skipped count says
  how many there were and its query names them (the regenerate-data skill).

What does not: ids and timestamps (rows are renumbered in file order, which
is the order the export reads them in), gene_annotations (empty until
`--sync-annotations`; the export keeps the committed gene_annotations.json
meanwhile), and every spelling the export normalises -- trait synonyms come
back folded, pathways lowercased, the omics prose in the shape the export
splits it from.

Run it as a module, not as a path: `uv run scripts/...` puts scripts/ on
sys.path rather than the repo root, and the pipeline imports below would not
resolve.
"""

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from dotenv import load_dotenv

from pipeline.database import Database
from pipeline.export.tables import clean_gene_row, clean_trial_row
from pipeline.export.text import clean_column_name
from pipeline.export.writer import to_camel

_ROOT: Final = Path(__file__).resolve().parent.parent


class RestoreRefused(Exception):
    """The files cannot be restored faithfully, or the database is not empty."""


@dataclass(frozen=True)
class GeneRestore:
    """One genes row and its three join-table lists, in stored form."""

    columns: dict[str, Any]
    traits: list[str]
    links: list[str]
    references: list[str]


# (pmid, processed_at, fulltext_available, source, genes_extracted)
LedgerRow = tuple[str, datetime, bool, str, int]
# (run_timestamp, papers_processed, fulltext_retrieved, genes_extracted,
#  genes_validated, run_mode, status, duration_seconds, report)
RunRow = tuple[
    datetime, int, int, int, int, str, str | None, float | None, dict[str, Any] | None
]


@dataclass(frozen=True)
class RestorePlan:
    genes: list[GeneRestore]
    trials: list[dict[str, Any]]
    ncbi: list[tuple[str, str | None, str | None, str | None]]
    uniprot: list[tuple[str, str | None, str | None]]
    citations: list[tuple[str, Any, Any, Any, Any, Any, str]]
    ledger: list[LedgerRow]
    runs: list[RunRow]
    syncs: list[dict[str, Any]]


def _unless(value: Any, *sentinels: str) -> Any:
    return None if value in sentinels else value


# --------------------------------------------------------------------- genes

_COMMA_OUTSIDE_PARENS: Final = re.compile(r",(?![^(]*\))")


def stored_omics(entries: list[str]) -> str | None:
    """The stored prose `clean_omics_value` turns back into *entries*.

    The export rewrites the stored ":" to ";" after splitting studies on
    ";", and splits on commas outside parentheses, so a detail goes back
    behind a colon -- or, when it holds such a comma, inside parentheses,
    the form `_recover_omics_type` rebuilds "Type;detail" from.
    """
    if entries == ["(none found)"]:
        return None
    stored = []
    for entry in entries:
        candidate = entry.replace(";", ":")
        if _COMMA_OUTSIDE_PARENS.search(candidate):
            head, _, detail = entry.partition(";")
            candidate = f"{head} ({detail})"
        stored.append(candidate)
    return ", ".join(stored)


def gene_from_wire(row: Mapping[str, Any]) -> GeneRestore:
    """Invert `clean_gene_row` for one data/table1.json row.

    Every sentinel goes back as NULL or as zero list rows, which is all the
    export ever made one from.
    """
    return GeneRestore(
        columns={
            "protein": _unless(row["protein"], "(unknown)"),
            "gene": row["gene"],
            "chromosomal_location": _unless(row["chromosomalLocation"], "(unknown)"),
            "mendelian_randomization": row["mendelianRandomization"] == "Yes",
            "evidence_from_other_omics_studies": stored_omics(
                row["evidenceFromOtherOmicsStudies"]
            ),
            "brain_cell_types": _unless(row["brainCellTypes"], "(unknown)"),
            "affected_pathway": _unless(row["affectedPathway"], "(unknown)"),
            "source_quote": _unless(row["sourceQuote"], "(not yet extracted)"),
            "confidence": row["confidence"],
        },
        traits=[t for t in row["gwasTrait"] if t != "(none found)"],
        links=[o for o in row["linkToMonogenicDisease"] if o != "(none found)"],
        references=[r for r in row["references"] if r != "(reference needed)"],
    )


def _export_gene(gene: GeneRestore) -> dict[str, Any]:
    """What `_read_genes_with_lists` and `clean_gene_row` publish for it."""
    row = {
        **gene.columns,
        "gwas_trait": gene.traits or None,
        "link_to_monogenetic_disease": gene.links or None,
        "references": gene.references or None,
    }
    return {to_camel(key): value for key, value in clean_gene_row(row).items()}


# ------------------------------------------------------------ clinical trials

# Every clinical_trials column the export publishes, by its stored name.
_TRIAL_COLUMNS: Final = (
    "drug",
    "mechanism_of_action",
    "genetic_target",
    "genetic_evidence",
    "trial_name",
    "registry_id",
    "clinical_trial_phase",
    "target_population",
    "target_population_details",
    "target_sample_size",
    "estimated_completion_date",
    "primary_outcome",
    "sponsor_type",
    "overall_status",
)

# clean_trial_row's label for every `_COMPLETED_UNPUBLISHED` spelling is 23
# characters and estimated_completion_date is VARCHAR(20). The stored value
# was "Completed, unpublished" cut to that width, which is why the export's
# pattern accepts "unpublish".
_COMPLETED_UNPUBLISHED: Final = ("Completed (unpublished)", "Completed, unpublish")


def trial_from_wire(row: Mapping[str, Any]) -> dict[str, Any]:
    """Invert `clean_trial_row` for one data/table2.json row."""
    stored: dict[str, Any] = {}
    for column in _TRIAL_COLUMNS:
        value = row[to_camel(clean_column_name(column))]
        sentinel = "(none)" if column == "genetic_target" else "(unknown)"
        value = _unless(value, sentinel)
        if column == "target_sample_size" and value is not None:
            value = int(value)
        if column == "estimated_completion_date" and value == _COMPLETED_UNPUBLISHED[0]:
            value = _COMPLETED_UNPUBLISHED[1]
        stored[column] = value
    return stored


def _export_trial(trial: Mapping[str, Any]) -> dict[str, Any]:
    return {to_camel(key): value for key, value in clean_trial_row(dict(trial)).items()}


# ------------------------------------------------------------ the PMID ledger


def _utc(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _reports(logs_dir: Path) -> list[dict[str, Any]]:
    reports = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((logs_dir / "json").glob("pipeline_report_*.json"))
    ]
    events = logs_dir / "events.db"
    if events.is_file():
        con = sqlite3.connect(f"{events.resolve().as_uri()}?mode=ro", uri=True)
        try:
            reports.extend(
                json.loads(payload)
                for (payload,) in con.execute(
                    "SELECT payload FROM events "
                    "WHERE event_type = 'pipeline_completed' ORDER BY id"
                )
            )
        finally:
            con.close()
    return reports


def ledger_from_logs(logs_dir: Path) -> list[LedgerRow]:
    """pubmed_refs as the surviving run reports leave it.

    Only a run that reached the merge wrote it -- its report has a
    `database` block; an offline --pmids / --local-pdfs run's has none --
    and a dry run or a test-mode preview wrote nothing. A failed paper was
    never recorded, so the next run retried it.
    `record_processed_pmids_batch` upserts, so runs are replayed oldest
    first and the last one to process a paper wins. The same run recorded
    both as a report file and as an event is counted once.
    """
    runs: dict[datetime, list[dict[str, Any]]] = {}
    for report in _reports(logs_dir):
        config = report.get("pipeline_config") or {}
        if "database" not in report or config.get("dry_run") or config.get("test_mode"):
            continue
        runs.setdefault(_utc(report["timestamp"]), report.get("papers_detail") or [])
    ledger: dict[str, LedgerRow] = {}
    for when in sorted(runs):
        for paper in runs[when]:
            if paper.get("error"):
                continue
            ledger[paper["pmid"]] = (
                paper["pmid"],
                when,
                bool(paper["fulltext"]),
                paper["source"],
                int(paper["gene_count"]),
            )
    return list(ledger.values())


# ---------------------------------------------------------------- run rows


def _run_rows(
    run: dict[str, Any] | None, status: dict[str, Any] | None
) -> list[RunRow]:
    """The pipeline_runs rows the two run files were exported from.

    pipeline_status.json is the newest run that did not fail, and
    pipeline_run.json the newest report of any status -- the same run at
    two precisions, or, when the newest run failed, two different runs.
    """
    rows: dict[str, RunRow] = {}
    if status is not None:
        stamp = _utc(status["runTimestamp"].replace("Z", "+00:00"))
        rows[stamp.strftime("%Y-%m-%dT%H:%M:%S")] = (
            stamp,
            status["papersProcessed"],
            status["fulltextRetrieved"],
            status["genesExtracted"],
            status["genesValidated"],
            "standard",
            None,
            None,
            None,
        )
    if run is not None:
        stamp = _utc(run["runTimestamp"])
        key = stamp.strftime("%Y-%m-%dT%H:%M:%S")
        counts = rows[key][1:5] if key in rows else (0, 0, 0, 0)
        rows[key] = (
            stamp,
            *counts,
            run["runMode"],
            run["status"],
            run["durationSeconds"],
            run,
        )
    return [rows[key] for key in sorted(rows)]


# --------------------------------------------------------------------- plan


def _differences(committed: Mapping[str, Any], again: Mapping[str, Any]) -> list[str]:
    keys = list(dict.fromkeys([*committed, *again]))
    return [k for k in keys if committed.get(k) != again.get(k)]


def _is_fallback_citation(row: Mapping[str, Any]) -> bool:
    return row["formattedRef"] == f"PMID: {row['pmid']} (citation not available)"


def build_plan(data_dir: Path, logs_dir: Path) -> RestorePlan:
    """Every row to write, or RestoreRefused naming those that would not
    export back to their committed JSON."""

    def read(name: str) -> Any:
        return json.loads((data_dir / name).read_text(encoding="utf-8"))

    failures: list[str] = []
    genes = []
    for wire in read("table1.json"):
        gene = gene_from_wire(wire)
        if keys := _differences(wire, _export_gene(gene)):
            failures.append(f"gene {wire['gene']}: {', '.join(keys)}")
        genes.append(gene)
    trials = []
    for wire in read("table2.json"):
        trial = trial_from_wire(wire)
        if keys := _differences(wire, _export_trial(trial)):
            failures.append(
                f"trial {wire['registryId']} / {wire['drug']}: {', '.join(keys)}"
            )
        trials.append(trial)
    if failures:
        raise RestoreRefused(
            "These rows would not export back to the committed JSON, so "
            "restoring them would change the data:\n  " + "\n  ".join(failures)
        )

    # A complete_lookup fallback is the export saying the cache had nothing;
    # storing one would make the cache claim an answer it never fetched.
    ncbi: dict[str, tuple[str, str | None, str | None, str | None]] = {}
    for name in ("gene_info.json", "gene_info_table2.json"):
        for row in read(name):
            if (row["uid"], row["description"], row["otheraliases"]) == (None,) * 3:
                continue
            ncbi.setdefault(
                row["name"],
                (row["name"], row["uid"], row["description"], row["otheraliases"]),
            )

    return RestorePlan(
        genes=genes,
        trials=trials,
        ncbi=list(ncbi.values()),
        uniprot=[
            (row["gene"], row["accession"], row["url"])
            for row in read("protein_info.json")
            if (row["accession"], row["url"]) != (None, None)
        ],
        citations=[
            (
                row["pmid"],
                row["authors"],
                row["title"],
                row["journal"],
                row["publicationDate"],
                row["doi"],
                row["formattedRef"],
            )
            for row in read("refs.json")
            if not _is_fallback_citation(row)
        ],
        ledger=ledger_from_logs(logs_dir),
        runs=_run_rows(read("pipeline_run.json"), read("pipeline_status.json")),
        syncs=read("pipeline_syncs.json"),
    )


# -------------------------------------------------------------------- write

# Every table write_plan inserts into, each of which must start empty.
_WRITTEN_TABLES: Final = (
    "genes",
    "gene_references",
    "gene_gwas_traits",
    "gene_monogenic_links",
    "clinical_trials",
    "ncbi_gene_info",
    "uniprot_info",
    "pubmed_citations",
    "pubmed_refs",
    "pipeline_runs",
    "sync_runs",
)

# A cache timestamp older than any TTL: the row was not fetched by this
# database, and when it was fetched before the loss is not published.
_NEVER_FETCHED: Final = datetime(1970, 1, 1)

_GENE_LISTS: Final = (
    ("gene_gwas_traits", "trait", "traits"),
    ("gene_monogenic_links", "omim_id", "links"),
    ("gene_references", "pmid", "references"),
)


def _insert(table: str, columns: list[str]) -> str:
    placeholders = ", ".join(f"${n}" for n in range(1, len(columns) + 1))
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"


async def write_plan(plan: RestorePlan) -> dict[str, int]:
    """Write the plan in one transaction, so a failure leaves nothing."""
    counts: dict[str, int] = {}
    async with Database.connection() as conn, conn.transaction():
        occupied = [
            table
            for table in _WRITTEN_TABLES
            # Interpolating is safe: every name is a module constant.
            if await conn.fetchval(f"SELECT EXISTS (SELECT 1 FROM {table})")
        ]
        if occupied:
            raise RestoreRefused(
                f"{', '.join(occupied)} already hold rows. The restore writes "
                "explicit ids and expects to be the only writer; restore into "
                "a freshly migrated database instead."
            )

        # Explicit ids in file order: the export reads `ORDER BY id`, so this
        # is what keeps the committed row order.
        for gene_id, gene in enumerate(plan.genes, start=1):
            await conn.execute(
                _insert("genes", ["id", *gene.columns]),
                gene_id,
                *gene.columns.values(),
            )
            for table, column, attribute in _GENE_LISTS:
                await conn.executemany(
                    _insert(table, ["gene_id", "ordinal", column]),
                    [
                        (gene_id, ordinal, value)
                        for ordinal, value in enumerate(getattr(gene, attribute))
                    ],
                )
        await conn.executemany(
            _insert("clinical_trials", ["id", *_TRIAL_COLUMNS]),
            [
                (trial_id, *(trial[c] for c in _TRIAL_COLUMNS))
                for trial_id, trial in enumerate(plan.trials, start=1)
            ],
        )
        for table in ("genes", "clinical_trials"):
            # The next insert takes max(id) + 1; an empty table keeps 1.
            await conn.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT max(id) FROM {table}), 0) + 1, false)"
            )

        await conn.executemany(
            _insert(
                "ncbi_gene_info", ["gene_symbol", "ncbi_uid", "description", "aliases"]
            ),
            plan.ncbi,
        )
        # protein_info.json carries no protein name or GO terms, so these rows
        # are incomplete -- and get_cached_uniprot_info treats any row younger
        # than DB_CACHE_TTL_DAYS as a hit, NULL columns or not. Stamped with
        # the epoch, they are stale to the sync's first read and refetched,
        # while the export, which reads with no TTL, still publishes them.
        # (ncbi_gene_info needs no stamp: a NULL map_location already makes
        # get_cached_ncbi_genes withhold the row.) The trigger fires on
        # UPDATE only, so the INSERT keeps these values.
        await conn.executemany(
            _insert(
                "uniprot_info",
                ["gene_symbol", "accession", "url", "fetched_at", "updated_at"],
            ),
            [(*row, _NEVER_FETCHED, _NEVER_FETCHED) for row in plan.uniprot],
        )
        await conn.executemany(
            _insert(
                "pubmed_citations",
                ["pmid", "authors", "title", "journal", "publication_date", "doi",
                 "formatted_ref"],
            ),
            plan.citations,
        )
        # processed_at is TIMESTAMP, written by CURRENT_TIMESTAMP in local time.
        await conn.executemany(
            _insert(
                "pubmed_refs",
                ["pmid", "processed_at", "fulltext_available", "source",
                 "genes_extracted"],
            ),
            [
                (pmid, when.astimezone().replace(tzinfo=None), *rest)
                for pmid, when, *rest in plan.ledger
            ],
        )
        await conn.executemany(
            _insert(
                "pipeline_runs",
                ["run_timestamp", "papers_processed", "fulltext_retrieved",
                 "genes_extracted", "genes_validated", "run_mode", "status",
                 "duration_seconds", "report"],
            ),
            [
                (*row[:8], None if row[8] is None else json.dumps(row[8]))
                for row in plan.runs
            ],
        )
        await conn.executemany(
            _insert(
                "sync_runs",
                ["mode", "run_timestamp", "status", "duration_seconds", "report"],
            ),
            [
                (
                    sync["mode"],
                    _utc(sync["runTimestamp"]),
                    sync["status"],
                    sync["durationSeconds"],
                    json.dumps(sync),
                )
                for sync in plan.syncs
            ],
        )
        for table in _WRITTEN_TABLES:
            counts[table] = await conn.fetchval(f"SELECT count(*) FROM {table}")
    return counts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild the database from the committed data/*.json."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write into the database .env names (default: check only)",
    )
    args = parser.parse_args(argv)

    try:
        plan = build_plan(_ROOT / "data", _ROOT / "logs")
    except RestoreRefused as exc:
        sys.exit(str(exc))
    print(
        f"{len(plan.genes)} genes, {len(plan.trials)} trials, "
        f"{len(plan.ledger)} processed PMIDs, {len(plan.runs)} run rows, "
        f"{len(plan.syncs)} refreshes; every row round-trips through the export."
    )
    if not args.write:
        print("Nothing written; pass --write to restore.")
        return

    # Loaded here rather than at import, so importing this module -- as the
    # tests do -- never puts the real .env's credentials in the environment.
    load_dotenv(_ROOT / ".env")

    async def _write() -> dict[str, int]:
        try:
            return await write_plan(plan)
        finally:
            await Database.close()

    try:
        counts = asyncio.run(_write())
    except RestoreRefused as exc:
        sys.exit(str(exc))
    for table, count in counts.items():
        print(f"{table}: {count} rows")


if __name__ == "__main__":
    main()
