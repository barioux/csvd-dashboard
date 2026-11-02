"""UniProt protein information fetching module.

Fetches protein data (accession, GO annotations, protein name) from UniProt
and stores results in PostgreSQL for dashboard consumption.
"""

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import httpx

from pipeline.api_telemetry import record_transport_failure
from pipeline.cache_utils import (
    DB_CACHE_TTL_DAYS,
    SyncResult,
    make_log_progress,
    run_batched_fetch,
    single_flight_get,
    sync_cache_misses,
)
from pipeline.config import PipelineConfig
from pipeline.http_client import AsyncHttpClientManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

UNIPROT_BASE_URL: Final[str] = "https://rest.uniprot.org/uniprotkb/search"
# UniProt separates the names inside one TSV cell with spaces; semicolons and
# commas appear in hand-edited exports of the same field.
_SYNONYM_SPLIT: Final[re.Pattern[str]] = re.compile(r"[\s,;]+")
_GO_FIELDS: Final[tuple[str, str, str]] = (
    "biological_process",
    "molecular_function",
    "cellular_component",
)


@dataclass(slots=True)
class UniProtInfo:
    """UniProt protein information for a single gene."""

    gene_symbol: str
    accession: str | None = None
    protein_name: str | None = None
    biological_process: str | None = None
    molecular_function: str | None = None
    cellular_component: str | None = None
    url: str | None = None
    cacheable_miss: bool = True


# ---------------------------------------------------------------------------
# HTTP CLIENT AND CACHE
# ---------------------------------------------------------------------------

# Module-level shared HTTP client (30s timeout for UniProt's slower API)
_client_manager = AsyncHttpClientManager(timeout=30.0)
_uniprot_cache: OrderedDict[str, UniProtInfo | None] = OrderedDict()
_cache_lock: asyncio.Lock | None = None
_uniprot_semaphore: asyncio.Semaphore | None = None
# Single-flight registry keyed by uppercase symbol; see ``single_flight_get``.
_in_flight: dict[str, asyncio.Task[UniProtInfo | None]] = {}


def _get_cache_lock() -> asyncio.Lock:
    """Lazy-init cache lock (avoids creating Lock before event loop exists)."""
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


def _get_uniprot_semaphore(config: PipelineConfig | None = None) -> asyncio.Semaphore:
    """Get or create the UniProt rate-limit semaphore."""
    global _uniprot_semaphore
    if _uniprot_semaphore is None:
        limit = (config or PipelineConfig()).uniprot_rate_limit
        _uniprot_semaphore = asyncio.Semaphore(limit)
    return _uniprot_semaphore


async def close_uniprot_client() -> None:
    """Close shared HTTP client (call at shutdown)."""
    await _client_manager.close()


def clear_uniprot_cache() -> None:
    """Clear the UniProt info cache and any in-flight task references."""
    global _uniprot_cache
    _uniprot_cache = OrderedDict()
    _in_flight.clear()


def _clean_go_term(text: str | None) -> str | None:
    """Clean GO annotation text by removing GO IDs in brackets.

    Example: "apoptotic process [GO:0006915]" -> "apoptotic process"
    """
    if not text:
        return None

    # Remove GO IDs like [GO:0006915]
    cleaned = re.sub(r"\s*\[GO:\d+\]", "", text)
    # Clean up multiple semicolons and whitespace
    cleaned = re.sub(r";\s*;", ";", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip() or None


def _parse_search_rows(
    lines: list[str], gene_symbol: str
) -> tuple[str | None, str | None]:
    """The entry that names this symbol, or ``(None, None)``.

    A row whose *primary* gene is the symbol wins outright. Failing that, the
    symbol must appear in the row's ``Gene Names (synonym)`` column -- which
    the query already asks for. Taking the first returned row instead was the
    defect: UniProt token-matches the gene-names field even under
    ``gene_exact:``, so an entry that merely contains the symbol as a word in
    another gene's name was published as this gene's protein, accession, name
    and URL alike, and ``sync_uniprot_info`` files anything with an accession
    as a success and holds it for DB_CACHE_TTL_DAYS.

    The synonym fallback is load-bearing rather than lenient: it is how an
    obsolete symbol resolves, ``C6orf195`` being published from
    ``LINC01600``'s entry, which lists it as a synonym. Among synonym matches
    a reviewed (Swiss-Prot) entry beats an unreviewed one; otherwise the first
    stands.
    """
    header = lines[0].split("\t")

    def _column_index(name: str, fallback: int) -> int:
        return header.index(name) if name in header else fallback

    accession_index = _column_index("Entry", 0)
    gene_index = _column_index("Gene Names (primary)", 2)
    synonym_index = _column_index("Gene Names (synonym)", 3)
    protein_index = _column_index("Protein names", 4)
    # Optional rather than defaulted: a wrong guess here would read a gene
    # name as a review status and quietly rank the rows on nothing.
    reviewed_index = header.index("Reviewed") if "Reviewed" in header else None
    required_columns = max(
        accession_index,
        gene_index,
        synonym_index,
        protein_index,
        reviewed_index if reviewed_index is not None else 0,
    )
    # Synonym matches are only recorded here and chosen after the loop: a
    # primary match wins outright wherever UniProt puts it, and UniProt can
    # put another gene's reviewed entry, which keeps the symbol as an old
    # synonym, first.
    reviewed_synonym: tuple[str, str] | None = None
    fallback: tuple[str, str] | None = None
    wanted = gene_symbol.upper()

    for line in lines[1:]:
        columns = line.split("\t")
        if len(columns) <= required_columns:
            continue
        accession = columns[accession_index]
        protein_name = columns[protein_index]
        if columns[gene_index].upper() == wanted:
            return accession, protein_name
        synonyms = {
            token.upper() for token in _SYNONYM_SPLIT.split(columns[synonym_index])
        }
        if wanted not in synonyms:
            continue
        if (
            reviewed_synonym is None
            and reviewed_index is not None
            and columns[reviewed_index].strip().casefold() == "reviewed"
        ):
            reviewed_synonym = accession, protein_name
        if fallback is None:
            fallback = accession, protein_name

    chosen = reviewed_synonym or fallback
    if chosen is None:
        logger.debug(f"No UniProt row names {gene_symbol}")
    return chosen or (None, None)


# ---------------------------------------------------------------------------
# SEARCH FUNCTIONS
# ---------------------------------------------------------------------------


async def _fetch_uniprot_accession_status(
    gene_symbol: str,
) -> tuple[str | None, str | None, bool]:
    """Fetch UniProt accession for a gene symbol.

    Args:
        gene_symbol: Gene symbol to look up.

    Returns:
        Tuple of (accession, protein_name, cacheable_miss). ``cacheable_miss``
        is False for transient API/client failures that should not be stored
        as durable negative cache rows. Rows that name no matching gene are a
        *cacheable* miss: the answer arrived and does not carry this symbol.
    """
    params = {
        "format": "tsv",
        # ``reviewed`` and ``gene_synonym`` are what _parse_search_rows reads
        # to tell this gene's entry from an entry that merely mentions it.
        "fields": "accession,reviewed,gene_primary,gene_synonym,protein_name",
        "size": "5",
    }

    try:
        client = await _client_manager.get()
        queries = (
            f'gene_exact:"{gene_symbol}" AND organism_id:9606',
            f'gene:"{gene_symbol}" AND organism_id:9606',
        )
        for query in queries:
            params["query"] = query
            resp = await client.get(UNIPROT_BASE_URL, params=params)
            if resp.status_code != 200:
                logger.warning(
                    f"UniProt search failed for {gene_symbol}: {resp.status_code}"
                )
                return None, None, False
            lines = resp.text.strip().split("\n")
            if len(lines) >= 2:
                accession, protein_name = _parse_search_rows(lines, gene_symbol)
                # Rows that name some other gene are not an answer for this
                # one, so the broader ``gene:`` query still gets its turn.
                if accession is not None:
                    return accession, protein_name, True

        logger.debug(f"No UniProt entry found for {gene_symbol}")
        return None, None, True

    except httpx.TimeoutException:
        record_transport_failure(UNIPROT_BASE_URL)
        logger.warning(f"Timeout querying UniProt for {gene_symbol}")
    except httpx.RequestError as e:
        record_transport_failure(UNIPROT_BASE_URL)
        logger.warning(f"Request error querying UniProt for {gene_symbol}: {e}")
    except (ValueError, IndexError) as e:
        logger.warning(f"Failed to parse UniProt response for {gene_symbol}: {e}")

    return None, None, False


async def fetch_uniprot_go_info(accession: str) -> dict[str, str | None] | None:
    """Fetch GO annotations for a UniProt accession.

    Args:
        accession: UniProt accession ID.

    Returns:
        Dict with biological_process, molecular_function, cellular_component
        -- every value None when the entry carries no GO terms -- or None
        when UniProt did not answer: a timeout, a transport error, a non-200
        or a body that could not be read. The two are different facts, and
        the caller stores only the first.
    """
    url = f"https://rest.uniprot.org/uniprotkb/{accession}"
    params = {
        "format": "tsv",
        "fields": "go_p,go_f,go_c",
    }

    try:
        client = await _client_manager.get()
        resp = await client.get(url, params=params)

        if resp.status_code != 200:
            logger.warning(
                f"UniProt GO fetch failed for {accession}: {resp.status_code}"
            )
            return None

        lines = resp.text.strip().split("\n")
        if len(lines) < 2:
            return dict.fromkeys(_GO_FIELDS)

        # Parse TSV - first line is header, second is data
        cols = lines[1].split("\t")

        return {
            field: _clean_go_term(cols[index] if index < len(cols) else None)
            for index, field in enumerate(_GO_FIELDS)
        }

    except httpx.TimeoutException:
        record_transport_failure(url)
        logger.warning(f"Timeout fetching GO info for {accession}")
    except httpx.RequestError as e:
        record_transport_failure(url)
        logger.warning(f"Request error fetching GO info for {accession}: {e}")
    except (ValueError, IndexError) as e:
        logger.warning(f"Failed to parse GO response for {accession}: {e}")

    return None


async def fetch_uniprot_info(
    gene_symbol: str,
    config: PipelineConfig | None = None,
) -> UniProtInfo | None:
    """Fetch complete UniProt information for a gene symbol.

    Results are cached; concurrent callers for the same symbol share one
    in-flight fetch via ``single_flight_get``.
    """
    return await single_flight_get(
        gene_symbol.upper(),
        cache=_uniprot_cache,
        cache_lock=_get_cache_lock(),
        in_flight=_in_flight,
        semaphore=_get_uniprot_semaphore(config),
        fetch_fn=lambda: _fetch_uniprot_uncached(gene_symbol),
        label="UniProt cache",
    )


async def _fetch_uniprot_uncached(gene_symbol: str) -> UniProtInfo:
    """Internal: fetch UniProt data without caching."""
    accession, protein_name, cacheable_miss = await _fetch_uniprot_accession_status(
        gene_symbol
    )

    if not accession:
        return UniProtInfo(
            gene_symbol=gene_symbol,
            cacheable_miss=cacheable_miss,
        )

    # Fetch GO annotations. No answer is a transient miss for the whole
    # gene, not an entry with empty GO columns: sync_uniprot_info files
    # anything carrying an accession as a success, and a row written here
    # would hold the gene out of the next sync for DB_CACHE_TTL_DAYS.
    go_info = await fetch_uniprot_go_info(accession)
    if go_info is None:
        return UniProtInfo(
            gene_symbol=gene_symbol,
            cacheable_miss=False,
        )

    return UniProtInfo(
        gene_symbol=gene_symbol,
        accession=accession,
        protein_name=protein_name,
        biological_process=go_info["biological_process"],
        molecular_function=go_info["molecular_function"],
        cellular_component=go_info["cellular_component"],
        url=f"https://www.uniprot.org/uniprotkb/{accession}/entry",
    )


async def fetch_uniprot_batch(
    gene_symbols: list[str],
    progress_callback: Callable[[int, int], None] | None = None,
    config: PipelineConfig | None = None,
) -> list[UniProtInfo]:
    """Fetch UniProt info for multiple genes concurrently.

    Uses the module-level semaphore (via fetch_uniprot_info) to rate-limit
    concurrent requests.
    """

    async def _fetch_one(symbol: str) -> UniProtInfo:
        info = await fetch_uniprot_info(symbol, config=config)
        # No answer at all is not a confirmed "not in UniProt", so the
        # placeholder is a transient miss the sync will not store.
        return info or UniProtInfo(gene_symbol=symbol, cacheable_miss=False)

    return await run_batched_fetch(
        gene_symbols, _fetch_one, progress_callback=progress_callback
    )


# ---------------------------------------------------------------------------
# DATABASE SYNC
# ---------------------------------------------------------------------------


async def sync_uniprot_info(
    gene_symbols: list[str],
    config: PipelineConfig | None = None,
) -> SyncResult:
    """Sync UniProt info to database for given gene symbols.

    Args:
        gene_symbols: List of gene symbols to sync.
        config: Pipeline config for UniProt semaphore sizing.

    Returns:
        SyncResult with counts of fetched, cached, and failed genes.
    """
    from pipeline.database import get_cached_uniprot_info, upsert_uniprot_batch

    # Fresh rows only — stale rows fall through to a re-fetch.
    cached_genes = await get_cached_uniprot_info(
        gene_symbols, max_age_days=DB_CACHE_TTL_DAYS
    )
    symbols_to_fetch = [s for s in gene_symbols if s not in cached_genes]

    logger.info(
        f"UniProt sync: {len(cached_genes)} cached, {len(symbols_to_fetch)} to fetch"
    )

    return await sync_cache_misses(
        symbols_to_fetch,
        cached_count=len(cached_genes),
        fetch_batch=lambda symbols: fetch_uniprot_batch(
            symbols,
            make_log_progress("UniProt fetch"),
            config=config,
        ),
        upsert_batch=upsert_uniprot_batch,
        is_success=lambda gene: gene.accession is not None,
        is_cacheable_miss=lambda gene: gene.cacheable_miss,
        error_for=lambda gene: f"UniProt not found: {gene.gene_symbol}",
    )
