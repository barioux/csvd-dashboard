"""Reset the genes to a curated baseline before a re-extraction.

For the day the extraction method changes enough that nothing the old one
merged should stand -- 2026-11-02, when extraction moved from Claude Opus 5 with
prompt v6 to Opus 5.5 with v7 and every value the pipeline had ever merged
came from the old pair. The merge only adds (references append, a placeholder
protein is filled, MR flips No to Yes, a quote and its confidence fill an empty
pair), so a new run over the same papers cannot take the old method's values
back out. This puts the table back where it stood before the old method's first
merge, so the next run's window is extracted by one method alone.

    uv run python -m scripts.reset_to_baseline --baseline PATH            # check
    uv run python -m scripts.reset_to_baseline --baseline PATH --write    # reset

PATH is a data/table1.json committed before the first merge -- for this
dataset, `a5915b4:data/table1.json` in the pre-squash history the
regenerate-data skill describes. It is converted to today's wire format first:
the provenance columns (published since) are appended at their sentinels, and a
protein the export of the day published as the gene symbol becomes
"(unknown)".

**Nothing is written unless two checks hold for every baseline row.**

- It round-trips: through `gene_from_wire` and back through `clean_gene_row`
  it reproduces itself exactly -- the refusal `scripts.restore_from_json`
  uses, so an export step this cannot invert is a refusal and not a silently
  different dataset.
- The live table holds it plus additions only: every list element it has is
  still there, MR is unchanged or No became Yes, a "(unknown)" protein may now
  be named, an absent quote or confidence may now be filled, and every other
  field is identical. Anything else is a curator's change made after the
  baseline, and losing it is the one outcome a reset must never have.

A baseline row that already carries a quote or a confidence is refused too:
that is an extraction, not curation.

**With --write, one transaction:** every genes row is deleted (the three list
tables cascade) and the baseline inserted with the restore's own
`insert_genes`; pubmed_refs is emptied, so the next run treats every paper in
its window as new; and the annotation rows and fetch statuses of every gene
symbol outside the baseline are deleted, because `--sync-annotations` replaces
only the genes it fetches and the export publishes the table whole. Trials, the
lookup caches, the annotations of the kept genes and the run and refresh
history are left alone: none of them is extracted.

Before anything else the target is printed from the environment -- `.env` is
loaded without override, so an exported DB_* variable wins and any one left
unset comes from `.env`. Read it before passing --write. Take a `pg_dump` first
and rehearse on a copy (the regenerate-data skill).

Run it as a module, not as a path: `uv run scripts/...` puts scripts/ on
sys.path rather than the repo root, and the imports below would not resolve.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from dotenv import load_dotenv
from scripts.restore_from_json import (
    GeneRestore,
    export_gene,
    gene_from_wire,
    insert_genes,
    row_differences,
)

from pipeline.database import Database
from pipeline.export.main import read_published_genes

_ROOT: Final = Path(__file__).resolve().parent.parent

_NOT_EXTRACTED: Final = "(not yet extracted)"
# Rows keyed by gene symbol rather than by genes.id, so the cascade misses them.
_ANNOTATION_TABLES: Final = ("gene_annotations", "gene_annotation_status")
# Every table the reset rewrites or empties, counted once it has.
_RESET_TABLES: Final = (
    "genes",
    "gene_references",
    "gene_gwas_traits",
    "gene_monogenic_links",
    "pubmed_refs",
    *_ANNOTATION_TABLES,
)
_LIST_KEYS: Final = (
    "gwasTrait",
    "evidenceFromOtherOmicsStudies",
    "linkToMonogenicDisease",
    "references",
)
# What clean_gene_row publishes for an empty list; never an element.
_LIST_SENTINELS: Final = frozenset({"(none found)", "(reference needed)"})


class ResetRefused(Exception):
    """The baseline cannot replace the live genes without losing curation."""


def current_format(row: Mapping[str, Any]) -> dict[str, Any]:
    """A 31-August table1.json row in today's wire format.

    Exact equality for the placeholder, as `clean_gene_row` uses: a curated
    name that differs from the symbol only in case is a real name.
    """
    converted = dict(row)
    if converted.get("protein") == converted["gene"]:
        converted["protein"] = "(unknown)"
    converted.setdefault("sourceQuote", _NOT_EXTRACTED)
    converted.setdefault("confidence", None)
    return converted


def _grew_from(key: str, before: Any, after: Any) -> bool:
    if key in _LIST_KEYS:
        return {v for v in before if v not in _LIST_SENTINELS} <= set(after)
    if key == "mendelianRandomization":
        return after == before or (before, after) == ("No", "Yes")
    if key == "protein":
        return after == before or before == "(unknown)"
    if key == "sourceQuote":
        return after == before or before == _NOT_EXTRACTED
    if key == "confidence":
        return after == before or before is None
    return after == before


def additions_only(
    baseline: Mapping[str, Any], current: Mapping[str, Any]
) -> list[str]:
    """The wire keys where *current* is not *baseline* plus what extraction adds.

    Both rows are in today's wire format. A key only one of them has is a
    format mismatch, and counts as a change.
    """
    keys = list(dict.fromkeys([*baseline, *current]))
    return [
        key
        for key in keys
        if key not in baseline
        or key not in current
        or not _grew_from(key, baseline[key], current[key])
    ]


def build_reset(
    baseline: Sequence[Mapping[str, Any]], current: Sequence[Mapping[str, Any]]
) -> list[GeneRestore]:
    """The baseline as rows to insert, in its own order, or ResetRefused
    listing every row that fails a check -- all of them, not the first."""
    live = {row["gene"]: row for row in current}
    seen: set[str] = set()
    failures: list[str] = []
    genes: list[GeneRestore] = []
    for raw in baseline:
        row = current_format(raw)
        symbol = row["gene"]
        if symbol in seen:
            failures.append(f"{symbol}: duplicate in the baseline")
            continue
        seen.add(symbol)
        if row["sourceQuote"] != _NOT_EXTRACTED or row["confidence"] is not None:
            failures.append(f"{symbol}: carries an extraction")
            continue
        gene = gene_from_wire(row)
        if keys := row_differences(row, export_gene(gene)):
            failures.append(f"{symbol}: does not round-trip: {', '.join(keys)}")
        if symbol not in live:
            failures.append(f"{symbol}: not in the current table")
        elif keys := additions_only(row, live[symbol]):
            failures.append(f"{symbol}: changed: {', '.join(keys)}")
        genes.append(gene)
    if failures:
        raise ResetRefused(
            "The baseline cannot replace the current genes without losing "
            "curation or changing the data:\n  " + "\n  ".join(failures)
        )
    return genes


# -------------------------------------------------------------------- write


async def pending_changes(keep: Collection[str]) -> dict[str, int]:
    """Rows in genes and pubmed_refs now, and the annotation rows a reset
    keeping *keep* would delete. Writes nothing."""
    async with Database.connection() as conn:
        counts = {
            "genes": await conn.fetchval("SELECT count(*) FROM genes"),
            "pubmed_refs": await conn.fetchval("SELECT count(*) FROM pubmed_refs"),
        }
        for table in _ANNOTATION_TABLES:
            # Interpolating is safe: every name is a module constant.
            counts[table] = await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE gene_symbol <> ALL($1::text[])",
                list(keep),
            )
    return counts


async def reset_database(genes: Sequence[GeneRestore]) -> dict[str, int]:
    """Replace the genes with *genes*, empty the ledger and drop the
    annotations of every other symbol, in one transaction; the row counts
    afterwards."""
    keep = [gene.columns["gene"] for gene in genes]
    async with Database.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM genes")  # the list tables cascade
        await insert_genes(conn, genes)
        await conn.execute("DELETE FROM pubmed_refs")
        for table in _ANNOTATION_TABLES:
            await conn.execute(
                f"DELETE FROM {table} WHERE gene_symbol <> ALL($1::text[])", keep
            )
        return {
            table: await conn.fetchval(f"SELECT count(*) FROM {table}")
            for table in _RESET_TABLES
        }


async def run(baseline: Path, *, write: bool) -> None:
    """Check the baseline against the database the environment names, print
    what a reset would change, and reset it when *write* is set."""
    env = os.environ
    print(
        f"Target: {env.get('DB_USER')}@{env.get('DB_HOST')}:"
        f"{env.get('DB_PORT') or 5432}/{env.get('DB_NAME')}"
    )
    current = await read_published_genes()
    genes = build_reset(json.loads(baseline.read_text(encoding="utf-8")), current)
    keep = {gene.columns["gene"] for gene in genes}
    removed = [row["gene"] for row in current if row["gene"] not in keep]
    pending = await pending_changes(keep)
    print(
        f"genes: {pending['genes']} now, {len(genes)} after; "
        f"removed: {', '.join(map(str, removed)) or 'none'}"
    )
    print(f"pubmed_refs: {pending['pubmed_refs']} now, 0 after")
    for table in _ANNOTATION_TABLES:
        print(f"{table}: {pending[table]} rows of removed genes to delete")
    if not write:
        print("Nothing written; pass --write to reset.")
        return
    for table, count in (await reset_database(genes)).items():
        print(f"{table}: {count} rows after the reset")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Reset the genes to a curated baseline before a re-extraction."
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        required=True,
        help="a data/table1.json committed before the first extraction merged",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="reset the database the environment names (default: check only)",
    )
    args = parser.parse_args(argv)

    # Without override, so an exported DB_* variable -- a rehearsal copy's --
    # wins over .env, which names production.
    load_dotenv(_ROOT / ".env")

    async def _run() -> None:
        try:
            await run(args.baseline, write=args.write)
        finally:
            await Database.close()

    try:
        asyncio.run(_run())
    except ResetRefused as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
