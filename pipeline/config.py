"""Centralized configuration for the pipeline.

All tunable constants live here. Every setting can be overridden via
environment variable (prefixed with ``PIPELINE_``).  Modules accept a
``PipelineConfig`` instance instead of defining their own constants.
"""

# On Python 3.15 (PEP 810) this import is lazy. Only `extraction_tool` reads
# `transform_schema`, and the SDK is half a second of import, so it now loads
# with the first extraction rather than with every config -- a refresh or an
# export never pays for it. 3.14 ignores the name and imports it eagerly.
__lazy_modules__ = frozenset({"anthropic"})

import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache, cached_property
from math import isfinite
from pathlib import Path
from typing import Any, Final

from anthropic import transform_schema
from lxml import etree  # type: ignore[import-untyped]

from pipeline.disease import load_disease
from pipeline.extraction_models import ExtractionResult
from pipeline.prompts import (
    PROMPT_VERSION,
    PROMPT_VERSIONS,
    PROMPT_VERSIONS_WITHOUT_PROVENANCE,
)


def _env_number[T](
    name: str,
    default: T,
    convert: Callable[[str], T],
    type_name: str,
) -> T:
    """Read and convert an environment variable, falling back to *default*."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return convert(raw)
    except ValueError:
        raise ValueError(
            f"Environment variable {name} must be {type_name}, got {raw!r}"
        ) from None


def _env_int(name: str, default: int) -> int:
    """Read an integer from an environment variable, falling back to *default*."""
    return _env_number(name, default, int, "an integer")


def _finite_float(raw: str) -> float:
    """float(), refusing the spellings that parse but cannot be compared.

    `float("nan")` and `float("inf")` are legal, and a NaN confidence floor
    makes every `confidence < floor` False -- nothing rejected, nothing
    said. A value that is not a finite number is not a setting.
    """
    value = float(raw)
    if not isfinite(value):
        raise ValueError(raw)
    return value


def _env_float(name: str, default: float) -> float:
    """Read a finite float from an environment variable, or *default*."""
    return _env_number(name, default, _finite_float, "a finite float")


def _env_str(name: str, default: str) -> str:
    """Read a string from an environment variable, falling back to *default*."""
    return os.getenv(name, default)


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean from an environment variable, falling back to *default*.

    Truthy values (case-insensitive): "1", "true", "yes", "on".
    Anything else is falsy.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Read a comma-separated list from an environment variable.

    Empty entries are dropped; surrounding whitespace is stripped. Returns
    *default* if the variable is unset.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _env_path(name: str, default: Path) -> str:
    """Read a path-like string, treating an empty override as unset."""
    default_str = str(default)
    return _env_str(name, default_str) or default_str


def _require_at_least(minimum: int, **settings: int) -> None:
    """Reject integer settings below a shared inclusive lower bound."""
    for name, value in settings.items():
        if value < minimum:
            raise ValueError(f"{name} must be >= {minimum}, got {value}")


PMID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{1,9}$")

# Accelerator devices docling accepts, mirroring the validator in
# docling/datamodel/accelerator_options.py. Mirrored rather than imported:
# config.py must stay importable without dragging in docling's torch import
# graph, which the argcomplete fast path in main.py exists to avoid.
_PDF_DEVICE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(auto|cpu|mps|xpu|cuda(:\d+)?)$"
)


@cache
def default_pdf_threads() -> int:
    """Performance cores, which is not the same as every core.

    Measured on an M1 Pro (8 performance + 2 efficiency cores), converting a
    rasterised paper plus an 18-page GWAS paper:

        4 threads  69.3 s   (the previous default)
        8 threads  59.1 s   -15%
        10 threads 81.5 s   +18% -- worse than doing nothing

    os.cpu_count() reports 10 here, so the obvious "use every core" default
    would have been slower than the number it replaced: spilling a
    compute-bound stage onto the efficiency cores costs more than the extra
    parallelism returns. hw.perflevel0 is macOS's count of the fast cores.

    Cached because PipelineConfig is constructed constantly in tests and this
    shells out; the answer cannot change within a process.
    """
    if sys.platform == "darwin":
        try:
            completed = subprocess.run(
                ["sysctl", "-n", "hw.perflevel0.logicalcpu"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            pass  # Not fatal: fall through to the portable answer below.
        else:
            value = completed.stdout.strip()
            if value.isdigit() and int(value) >= 1:
                return int(value)
    # Homogeneous CPUs, or a macOS that would not answer: every core is
    # equally fast, so there is no slow tier to avoid.
    return os.cpu_count() or 4

# NCBI E-utilities base URLs
NCBI_ESEARCH_URL: Final[str] = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
)
NCBI_ESUMMARY_URL: Final[str] = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
)
NCBI_EFETCH_URL: Final[str] = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
)

ORPHADATA_BASE_URL: Final[str] = "https://api.orphadata.com"
# Orphadata asserts its own licence in every payload. The client compares
# against this and logs loudly on a change, because the attribution block on
# the About page is tied to this exact value.
ORPHADATA_LICENCE: Final[str] = "CC-BY-4.0"

OPENTARGETS_GRAPHQL_URL: Final[str] = (
    "https://api.platform.opentargets.org/api/v4/graphql"
)

# The one hardened parser for every externally supplied XML document the
# pipeline reads — NCBI E-utilities responses and the publisher-authored
# JATS Europe PMC relays (EBI is the transport there, not the author, so
# that content is no more trustworthy than any other).
#
# resolve_entities=False stops internal-entity amplification (confirmed
# live: the default parser expands a small nested entity to 1,000
# characters; this one leaves it as the unresolved literal reference).
# no_network=True and load_dtd=False stop an external DTD or entity from
# being fetched at all — load_dtd already defaults to False and is stated
# because this is the constant a reader checks for it.
#
# There was briefly a second copy of this in europepmc.py. Two constants
# both called "the hardened parser" are the pair that drifts the day
# someone tightens one, so there is deliberately only one.
SAFE_XML_PARSER: Final[etree.XMLParser] = etree.XMLParser(
    resolve_entities=False, no_network=True, load_dtd=False
)

# Default ClinicalTrials.gov condition/keyword terms for disease relevance.
# Override via PIPELINE_CT_SEARCH_TERMS (comma-separated).
DEFAULT_CT_SEARCH_TERMS: Final[tuple[str, ...]] = load_disease().ct_search_terms


def get_ncbi_params(base_params: dict[str, str]) -> dict[str, str]:
    """Add NCBI API key to params if available."""
    api_key = os.getenv("NCBI_API_KEY")
    if api_key:
        return {**base_params, "api_key": api_key}
    return base_params


def validate_pmid(pmid: str) -> str:
    """Validate and normalize a PubMed ID.

    Args:
        pmid: The PubMed identifier to validate.

    Returns:
        The validated PMID string.

    Raises:
        ValueError: If the PMID format is invalid.
    """
    pmid = pmid.strip()
    if not PMID_PATTERN.fullmatch(pmid):
        raise ValueError(f"Invalid PMID format: {pmid!r}")
    return pmid


PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# One extraction model, pinned. The pipeline's output is compared across
# runs, against recorded cassettes, and against a gold standard, so the
# model is part of the method rather than a deployment knob. Changing it is
# a code change with a re-recorded harness, not an environment variable.
# Effort stays overridable -- see PipelineConfig.llm_effort.
EFFORT_LEVELS: Final[tuple[str, ...]] = ("low", "medium", "high", "xhigh", "max")
EXTRACTION_MODEL: Final[str] = "claude-opus-5-5"

# Name of the strict tool the extraction schema rides on. The response
# parser matches tool_use blocks against it by name.
EXTRACTION_TOOL_NAME: Final[str] = "report_genes"

# Reported in the run metadata beside EXTRACTION_MODEL. A constant rather
# than a regex over the model name, now that there is one name to parse.
EXTRACTION_MODEL_VERSION: Final[str] = "5.5"

# Maximum output tokens for EXTRACTION_MODEL — from the Anthropic model
# table.
MODEL_MAX_OUTPUT_TOKENS: Final[int] = 128_000


@dataclass
class PipelineConfig:
    """Centralised, immutable-ish configuration for the entire pipeline.

    Every field can be overridden via an environment variable. The naming
    convention is ``PIPELINE_<FIELD_UPPER>`` (e.g. ``PIPELINE_LLM_EFFORT``).
    The extraction model is the one exception: it is pinned in code, not
    a field — see EXTRACTION_MODEL and the llm_model property.
    """

    # --- LLM settings ---
    # 0 = auto-resolve to the model's maximum (see __post_init__).
    llm_max_tokens: int = field(
        default_factory=lambda: _env_int("PIPELINE_LLM_MAX_TOKENS", 0)
    )
    # Effort level: "high" (default), "low", "medium", "xhigh", or "max".
    # Always transmitted -- see output_config for why the default cannot
    # be left to the API.
    llm_effort: str = field(
        default_factory=lambda: _env_str("PIPELINE_LLM_EFFORT", "high")
    )
    # Extraction prompt version. prompts.PROMPT_VERSION is production;
    # __post_init__ refuses the pre-provenance versions and any name
    # prompts.py does not carry — this string is published as the method
    # behind every extracted row.
    prompt_version: str = field(
        default_factory=lambda: _env_str("PIPELINE_PROMPT_VERSION", PROMPT_VERSION)
    )

    # Maximum paper text chars sent to the LLM. ~100K tokens of a 1M-token
    # context window, leaving ample room for the system prompt, the tool
    # schema, thinking and the response. The previous 100,000 was sized for
    # a 200K context and was a leftover rather than a decision. Truncation
    # is still wanted: an unbounded paper is an unbounded bill.
    max_paper_text_chars: int = field(
        default_factory=lambda: _env_int("PIPELINE_MAX_PAPER_TEXT_CHARS", 400_000)
    )

    # --- Retry settings (parse / API errors) ---
    max_retries: int = field(
        default_factory=lambda: _env_int("PIPELINE_MAX_RETRIES", 1)
    )

    # --- Rate-limit retry settings (429 errors) ---
    max_rate_limit_retries: int = field(
        default_factory=lambda: _env_int("PIPELINE_MAX_RATE_LIMIT_RETRIES", 6)
    )
    rate_limit_retry_delay: float = field(
        default_factory=lambda: _env_float("PIPELINE_RATE_LIMIT_RETRY_DELAY", 1.0)
    )

    # --- Connection/network error retry settings ---
    max_connection_retries: int = field(
        default_factory=lambda: _env_int("PIPELINE_MAX_CONNECTION_RETRIES", 3)
    )
    connection_retry_delay: float = field(
        default_factory=lambda: _env_float("PIPELINE_CONNECTION_RETRY_DELAY", 2.0)
    )

    # --- Concurrency ---
    max_concurrent_papers: int = field(
        default_factory=lambda: _env_int("PIPELINE_MAX_CONCURRENT_PAPERS", 5)
    )

    # Pre-call token reservation, for the rate limiter's TPM window.
    #
    # This is a *reservation*, not a measurement: acquire() adds it to the
    # window before the call and record_actual_usage() replaces it with the
    # real figure only when the call returns, ~50 s later. So an oversized
    # value throttles admission for the whole of that window, and the
    # binding constraint stops being max_concurrent_papers.
    #
    # 40_000 against a 100_000 TPM limit admitted two papers where five were
    # configured. Measured totals per call across the three runs in
    # logs/json/ are 9.6K, 11.7K and 14.5K, so 20_000 sits above every
    # observed call while letting the configured concurrency through:
    # 5 x 20_000 is exactly the TPM limit, and real usage lands well under
    # it. Raise both together, or neither.
    estimated_tokens_per_call: int = field(
        default_factory=lambda: _env_int("PIPELINE_ESTIMATED_TOKENS_PER_CALL", 20_000)
    )

    # --- Rate limiter (RPM / TPM) ---
    rpm_limit: int = field(default_factory=lambda: _env_int("PIPELINE_RPM_LIMIT", 50))
    tpm_limit: int = field(
        default_factory=lambda: _env_int("PIPELINE_TPM_LIMIT", 100_000)
    )

    # --- Validation ---
    # The floor is two numbers because it decides two different things with
    # opposite risk profiles. Adding a reference to a gene already in the
    # table is cheap, reversible and visible in `git diff data/`; creating a
    # new row is a scientific claim in a published dataset.
    #
    # Floor for adding a reference to a gene already in the table, and for
    # nothing else: `_citation_only` in pipeline/data_merger.py withholds
    # the causal claims from an update below the insert floor, because
    # `mendelian_randomization` is OR-accumulated and the trait and omics
    # lists are append-only, so none of the three is the reversible write
    # this floor is justified by. Set where
    # gold recall saturates -- see the sweep in
    # tests/pipeline/test_extraction_golden.py. 0.45 is the highest floor
    # that still recovers every reachable gold gene the model finds (41/46);
    # below it nothing is gained and noise is admitted, above it gold genes
    # start being discarded.
    confidence_threshold_update: float = field(
        default_factory=lambda: _env_float(
            "PIPELINE_CONFIDENCE_THRESHOLD_UPDATE", 0.45
        )
    )
    # Floor for creating a new curated row. Deliberately NOT swept: the gold
    # standard is a curated subset, so it cannot tell an incorrect new gene
    # from an unreviewed one, and a number tuned against it would look
    # measured without being so. 0.65 is a curation policy, unchanged.
    confidence_threshold_insert: float = field(
        default_factory=lambda: _env_float(
            "PIPELINE_CONFIDENCE_THRESHOLD_INSERT", 0.65
        )
    )
    # When True, drop any gene whose source_quote is not found verbatim in
    # the document the model was sent -- `locate_quote` in citations.py,
    # not the API citation match, which is computed beside it and only
    # logged. Gating on citations would discard two thirds of the genes for
    # want of prose (33% coverage), while the document check drops one gene
    # in 138; see "Provenance" in pipeline/CLAUDE.md. Default False: this
    # lands as a measurement first, so a mismatch shows up in the logs
    # rather than silently removing a gene.
    require_verified_quotes: bool = field(
        default_factory=lambda: _env_bool("PIPELINE_REQUIRE_VERIFIED_QUOTES", False)
    )

    # --- External API rate limits ---
    ncbi_rate_limit: int = field(
        default_factory=lambda: _env_int("PIPELINE_NCBI_RATE_LIMIT", 10)
    )
    uniprot_rate_limit: int = field(
        default_factory=lambda: _env_int("PIPELINE_UNIPROT_RATE_LIMIT", 5)
    )
    clinvar_rate_limit: int = field(
        default_factory=lambda: _env_int("PIPELINE_CLINVAR_RATE_LIMIT", 10)
    )
    # Measured against the live API on 2026-08-31 over all 63 committed genes:
    # 61 return at least one pathogenic record, median 27, max 248 (NOTCH3).
    # 500 is 2x headroom over the largest real gene, so the returned UID set is
    # normally a census rather than a sample; exceeding it is logged because
    # the resulting record counts are only a floor.
    clinvar_max_records: int = field(
        default_factory=lambda: _env_int("PIPELINE_CLINVAR_MAX_RECORDS", 500)
    )
    orphadata_rate_limit: int = field(
        default_factory=lambda: _env_int("PIPELINE_ORPHADATA_RATE_LIMIT", 5)
    )
    opentargets_rate_limit: int = field(
        default_factory=lambda: _env_int("PIPELINE_OPENTARGETS_RATE_LIMIT", 5)
    )
    # The API self-describes as Beta and its schema has already shifted once
    # (knownDrugs -> drugAndClinicalCandidates). Every row records the version
    # it came from, and the sync warns when the live one differs from this.
    opentargets_data_version: str = field(
        default_factory=lambda: _env_str("PIPELINE_OPENTARGETS_DATA_VERSION", "26.06")
    )
    # associatedDiseases returns 490 rows for HTRA1, ranked by the API. Taking
    # the top N is a documented ranking, not an arbitrary sample.
    opentargets_max_diseases: int = field(
        default_factory=lambda: _env_int("PIPELINE_OPENTARGETS_MAX_DISEASES", 25)
    )

    # --- ClinicalTrials.gov sync ---
    ct_enabled: bool = field(
        default_factory=lambda: _env_bool("PIPELINE_CT_ENABLED", True)
    )
    ct_search_terms: tuple[str, ...] = field(
        default_factory=lambda: _env_list(
            "PIPELINE_CT_SEARCH_TERMS", DEFAULT_CT_SEARCH_TERMS
        )
    )
    ct_page_size: int = field(
        default_factory=lambda: _env_int("PIPELINE_CT_PAGE_SIZE", 100)
    )
    ct_max_concurrency: int = field(
        default_factory=lambda: _env_int("PIPELINE_CT_MAX_CONCURRENCY", 5)
    )
    # 6, not 3, and the difference is most of the registry. CTG throttles a
    # paging burst hard enough that four attempts over ~7s of backoff give up
    # mid-term: the first two live syncs each truncated three of the ten
    # search terms at their first page and fetched 382 studies. Measured
    # against the live API at this concurrency, a six-retry budget (~63s of
    # curve, capped per attempt at BACKOFF_CAP_SECONDS) truncates no term and
    # fetches 1423, twice over. Lowering ct_max_concurrency to 2 recovers most
    # of it as well but still truncated a term, so the budget is the fix and
    # the concurrency is not.
    ct_max_retries: int = field(
        default_factory=lambda: _env_int("PIPELINE_CT_MAX_RETRIES", 6)
    )

    # --- Database ---
    db_pool_min_size: int = field(
        default_factory=lambda: _env_int("PIPELINE_DB_POOL_MIN", 2)
    )
    db_pool_max_size: int = field(
        default_factory=lambda: _env_int("PIPELINE_DB_POOL_MAX", 10)
    )
    db_command_timeout: float = field(
        default_factory=lambda: _env_float("PIPELINE_DB_COMMAND_TIMEOUT", 60.0)
    )

    # --- PDF fallback (Docling) ---
    # OCR is a fallback, not a mode: docling's default OcrMode is
    # PDF_AWARE_LAYOUT_REGIONS, which drops every layout cluster that already
    # holds programmatic text, so a born-digital paper reaches the recognizer
    # with nothing to do. Turning it off entirely is the kill switch, not the
    # default.
    pdf_ocr: bool = field(default_factory=lambda: _env_bool("PIPELINE_PDF_OCR", True))
    pdf_ocr_coreml: bool = field(
        default_factory=lambda: _env_bool("PIPELINE_PDF_OCR_COREML", False)
    )
    pdf_device: str = field(
        default_factory=lambda: _env_str("PIPELINE_PDF_DEVICE", "auto")
    )
    pdf_num_threads: int = field(
        default_factory=lambda: _env_int(
            "PIPELINE_PDF_NUM_THREADS", default_pdf_threads()
        )
    )
    # Conversion was previously unbounded: PDF_TIMEOUT in pdf_retrieval bounds
    # the download only, so a pathological PDF could stall a run indefinitely.
    pdf_timeout_seconds: float = field(
        default_factory=lambda: _env_float("PIPELINE_PDF_TIMEOUT_SECONDS", 120.0)
    )
    # Companion to MAX_PDF_BYTES in pdf_retrieval, which caps size but not
    # page count -- a large supplement is the realistic hazard.
    pdf_max_pages: int = field(
        default_factory=lambda: _env_int("PIPELINE_PDF_MAX_PAGES", 200)
    )
    # Empty means "use docling's own cache" (~/.cache/docling/models). Set it
    # to pin the layout/TableFormer/RapidOCR weights to a known directory;
    # `deno task models` prefetches them.
    pdf_artifacts_path: str = field(
        default_factory=lambda: _env_str("PIPELINE_PDF_ARTIFACTS_PATH", "")
    )

    # --- Pipeline range ---
    min_days_back: int = 1
    max_days_back: int = 365 * 10

    # --- Misc ---
    test_mode_preview_count: int = 10

    # --- Notifications (Apprise) ---
    notify_urls: str = field(
        default_factory=lambda: _env_str("PIPELINE_NOTIFY_URLS", "")
    )
    event_db_path: str = field(
        default_factory=lambda: _env_path(
            "PIPELINE_EVENT_DB_PATH", PROJECT_ROOT / "logs" / "events.db"
        )
    )

    # --- Progress reporting ---
    progress_file: str = field(
        default_factory=lambda: _env_path(
            "PIPELINE_PROGRESS_FILE",
            PROJECT_ROOT / "logs" / "json" / "pipeline_progress.json",
        )
    )

    # --- Crash recovery ---
    # Papers already extracted, appended as each finishes. Not under
    # logs/json/: it is working state a run consumes and deletes, not a
    # report anything reads afterwards -- logs/events.db's neighbourhood.
    checkpoint_file: str = field(
        default_factory=lambda: _env_path(
            "PIPELINE_CHECKPOINT_FILE",
            PROJECT_ROOT / "logs" / "pipeline_checkpoint.jsonl",
        )
    )

    notify_max_retries: int = field(
        default_factory=lambda: _env_int("PIPELINE_NOTIFY_MAX_RETRIES", 3)
    )
    notify_retry_min_wait: float = field(
        default_factory=lambda: _env_float("PIPELINE_NOTIFY_RETRY_MIN_WAIT", 4.0)
    )
    notify_retry_max_wait: float = field(
        default_factory=lambda: _env_float("PIPELINE_NOTIFY_RETRY_MAX_WAIT", 30.0)
    )

    def __post_init__(self) -> None:
        if self.llm_max_tokens == 0:
            self.llm_max_tokens = MODEL_MAX_OUTPUT_TOKENS
        # The API rejects a max_tokens the model cannot take with a 400 --
        # per paper, an hour into the run, after every text fetch was paid
        # for. Same for an unknown effort, below.
        if not 1 <= self.llm_max_tokens <= MODEL_MAX_OUTPUT_TOKENS:
            raise ValueError(
                f"llm_max_tokens must be in [1, {MODEL_MAX_OUTPUT_TOKENS}], "
                f"got {self.llm_max_tokens}"
            )
        # Zero sends an empty document block and pays for the answer.
        if self.max_paper_text_chars < 1:
            raise ValueError(
                f"max_paper_text_chars must be >= 1, got {self.max_paper_text_chars}"
            )

        if self.llm_effort not in EFFORT_LEVELS:
            raise ValueError(
                f"llm_effort must be one of {', '.join(EFFORT_LEVELS)}, "
                f"got {self.llm_effort!r}"
            )

        # The floors are compared against a confidence in [0, 1]. _env_float
        # already refuses NaN and infinity; a floor outside the interval is a
        # gate that admits everything or nothing, and an update floor above
        # the insert floor inverts the two-floor design -- a gene could be
        # refused a reference and accepted as a new row.
        for name in ("confidence_threshold_update", "confidence_threshold_insert"):
            value = getattr(self, name)
            if not isfinite(value):
                raise ValueError(f"{name} must be a finite number, got {value}")
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1], got {value}")
        if self.confidence_threshold_update > self.confidence_threshold_insert:
            raise ValueError(
                "confidence_threshold_update "
                f"({self.confidence_threshold_update}) must not exceed "
                f"confidence_threshold_insert ({self.confidence_threshold_insert})"
            )

        # validation.py, ncbi_gene_fetch.py, pubmed_citations.py and
        # uniprot_fetch.py each build a Semaphore from one of these, and
        # Semaphore(0) makes the first lookup wait forever with no log line.
        _require_at_least(
            1,
            ncbi_rate_limit=self.ncbi_rate_limit,
            uniprot_rate_limit=self.uniprot_rate_limit,
        )

        # AsyncRateLimiter admits a request only when its estimate fits
        # under the TPM limit, and with an empty window nothing ages out,
        # so an estimate above the limit made the first paper wait forever
        # with no log line. A negative limit hangs the same way (nothing is
        # ever below it), and Semaphore(0) hangs every paper.
        _require_at_least(
            0,
            rpm_limit=self.rpm_limit,
            tpm_limit=self.tpm_limit,
        )
        if self.tpm_limit and self.estimated_tokens_per_call > self.tpm_limit:
            raise ValueError(
                f"estimated_tokens_per_call ({self.estimated_tokens_per_call}) "
                f"exceeds tpm_limit ({self.tpm_limit}); no request could ever "
                "be admitted"
            )
        if self.max_concurrent_papers < 1:
            raise ValueError(
                f"max_concurrent_papers must be >= 1, got {self.max_concurrent_papers}"
            )

        # Fail fast on CT misconfiguration — Semaphore(0) would hang every
        # fetch until the outer 1-hour timeout, and negative values crash
        # deep inside an async call with no config context.
        if self.ct_max_concurrency < 1:
            raise ValueError(
                f"ct_max_concurrency must be >= 1, got {self.ct_max_concurrency}"
            )
        if not 1 <= self.ct_page_size <= 1000:
            raise ValueError(
                f"ct_page_size must be in [1, 1000], got {self.ct_page_size}"
            )
        if self.ct_max_retries < 0:
            raise ValueError(f"ct_max_retries must be >= 0, got {self.ct_max_retries}")

        # The annotation clients build their semaphores the same way, so the
        # same Semaphore(0) hang applies to each of them. clinvar_rate_limit is
        # also a divisor in _throttle.
        _require_at_least(
            1,
            clinvar_rate_limit=self.clinvar_rate_limit,
            orphadata_rate_limit=self.orphadata_rate_limit,
            opentargets_rate_limit=self.opentargets_rate_limit,
        )
        _require_at_least(
            1,
            clinvar_max_records=self.clinvar_max_records,
            opentargets_max_diseases=self.opentargets_max_diseases,
        )

        # Docling validates the device too, but only when the converter is
        # built -- which is the first PDF, potentially an hour into a run.
        if not _PDF_DEVICE_PATTERN.fullmatch(self.pdf_device):
            raise ValueError(
                "pdf_device must be auto, cpu, mps, xpu, cuda or cuda:N, got "
                f"{self.pdf_device!r}"
            )
        if self.pdf_num_threads < 1:
            raise ValueError(
                f"pdf_num_threads must be >= 1, got {self.pdf_num_threads}"
            )
        if self.pdf_timeout_seconds <= 0:
            raise ValueError(
                f"pdf_timeout_seconds must be > 0, got {self.pdf_timeout_seconds}"
            )
        if self.pdf_max_pages < 1:
            raise ValueError(f"pdf_max_pages must be >= 1, got {self.pdf_max_pages}")

        # A pre-provenance prompt does not ask the model to copy a verbatim
        # sentence, but the schema still requires a non-blank source_quote
        # -- so the model supplies a paraphrase or an invention,
        # min_length=1 passes it, and every quote the run stores is
        # silently untrustworthy. Refuse the run instead: this is the one
        # misconfiguration whose damage is invisible in the output.
        #
        # Checked before the unknown-version guard below, so a version that
        # is both is named by the more specific message.
        if self.prompt_version in PROMPT_VERSIONS_WITHOUT_PROVENANCE:
            raise ValueError(
                f"prompt_version {self.prompt_version!r} predates the "
                "verbatim-quote instruction, but the extraction schema "
                "requires a non-blank source_quote -- the model would "
                "paraphrase or invent one and validation would accept it. "
                f"Use {PROMPT_VERSION!r} (the default); unset "
                "PIPELINE_PROMPT_VERSION to get it."
            )
        # An unrecognised version used to fall back with a warning
        # deep in build_extraction_prompt, which was safe for the *prompt*
        # and false everywhere else: report_metadata, the run report and
        # the checkpoint fingerprint all publish this string, so a typo'd
        # PIPELINE_PROMPT_VERSION named a method that never existed as the
        # provenance of every published row, and changing the typo later
        # invalidated a checkpoint whose papers were extracted with the
        # same prompt. The name a run reports has to be the name it ran.
        if self.prompt_version not in PROMPT_VERSIONS:
            raise ValueError(
                f"prompt_version {self.prompt_version!r} is not a known "
                f"prompt; known versions are {sorted(PROMPT_VERSIONS)}. "
                "The run report, the database record and the checkpoint "
                "fingerprint all publish this name as the method behind "
                "the extracted rows, so it cannot be one that does not "
                "exist. Unset PIPELINE_PROMPT_VERSION to get the default."
            )

    @property
    def llm_model(self) -> str:
        """The pinned extraction model. See EXTRACTION_MODEL."""
        return EXTRACTION_MODEL

    @property
    def model_version(self) -> str:
        """Short version of llm_model, for the run report."""
        return EXTRACTION_MODEL_VERSION

    @property
    def thinking_mode(self) -> str:
        """Thinking-block mode used for this model.

        Constant now that the model is pinned: adaptive is the only mode
        Claude Opus 5.5 accepts. Still reported, because the run report
        records the method a run used rather than only what varies.
        """
        return "adaptive"

    @cached_property
    def extraction_tool(self) -> dict[str, Any]:
        """Tool definition shared by the streaming and batch paths.

        The schema rides on a tool rather than in output_config.format
        because citations and output_config.format are mutually exclusive
        — the API returns 400 "Citations cannot be enabled when output
        format is set."

        **strict is False deliberately, and it is not a default left
        unset.** strict=True corrupts every free-text string in the tool
        input on Claude Opus 5: the model writes the correct value, then
        cannot close the string and writes its way out instead, appending
        guillemets and a cleanup expression -- `...PP4 = 0.91.», ».replace
        ('»','')` -- or, in one measured run, a repeating pseudo-Python
        loop that consumed the whole max_tokens budget. Measured 2026-08-31
        against the live API, holding model, system prompt, thinking config
        and message content fixed and toggling only this flag: 9/9 calls
        corrupt with strict=True, 4/4 clean with strict=False. It
        reproduces with and without citations, with and without a
        prose-first instruction, and on both the document-block and the old
        XML message shape. source_quote is the field it destroys, which is
        precisely the field provenance depends on.

        What that costs: the schema stops being grammar-constrained and
        becomes advisory. It is still sent, the model still follows it, and
        ExtractionResult.model_validate still rejects a violation -- which
        the validation-retry branch in anthropic_client turns into a retry
        rather than a bad row. Enforcement moved from the decoder to
        validate-and-retry; it did not disappear. Do not "fix" this back to
        True without re-running that measurement -- which has not been
        repeated on Claude Opus 5.5, so the finding above is still Opus 5's.

        Keeping one definition here — rather than one per API path — means
        a schema change can't invalidate Anthropic's 24h schema cache on
        one path while leaving the other stale, and the two paths can't
        silently drift into producing different output shapes from the
        same corpus.
        """
        return {
            "name": EXTRACTION_TOOL_NAME,
            "description": (
                "Report every gene with a putative causal link to "
                f"{load_disease().name} found in the document."
            ),
            "input_schema": transform_schema(ExtractionResult),
            "strict": False,
        }

    @property
    def thinking_config(self) -> dict[str, Any]:
        """`thinking` block shared by the streaming and batch paths.

        Adaptive is the only mode Claude Opus 5.5 accepts: manual thinking
        with `budget_tokens` is a 400 on 4.7 and later, and on Opus 5.5
        `{"type": "disabled"}` is a 400 at every effort. Hoisted here so the
        streaming and batch paths cannot drift, which they once did --
        streaming reserved tokens for the response text while batch took
        half of max_tokens unconditionally, giving the same paper a
        different reasoning allowance depending on the API it went through.

        display="summarized" keeps thinking blocks populated for the audit
        trace _record_thinking_trace writes; the API default is "omitted",
        which returns them empty. Token accounting no longer depends on it:
        the API reports the thinking count itself, and the character-ratio
        estimate is only the fallback for a response that does not. On
        Claude Opus 5.5 a "summarized" trace also holds any progress
        updates the model wrote before a tool call, which the API does not
        mark apart from reasoning.
        """
        return {"type": "adaptive", "display": "summarized"}

    @property
    def output_config(self) -> dict[str, Any]:
        """`output_config` block shared by the streaming and batch paths.

        Carries effort only, and always carries it. Effort used to be sent
        only when overridden, because "high" was Claude Opus 5's API
        default; Claude Opus 5.5's default is "medium", so omitting it would
        run every default-config paper one level below the setting the run
        report publishes and the checkpoint fingerprints. Stating it makes
        the request say what the method is rather than inheriting whatever
        the API defaults to. The schema moved to extraction_tool, because
        `format` is the one parameter citations refuse to sit beside.
        """
        return {"effort": self.llm_effort}
