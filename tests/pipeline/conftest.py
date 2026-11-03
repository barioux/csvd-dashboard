"""Shared fixtures for pipeline tests."""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path
from urllib.parse import unquote, urlsplit

# Ensure project root is on sys.path before any pipeline imports
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from collections.abc import Iterator  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import Any, Final  # noqa: E402

import pytest  # noqa: E402

from pipeline.config import PipelineConfig  # noqa: E402
from pipeline.extraction_models import TRACKED_TRAITS  # noqa: E402
from pipeline.llm_extraction import GeneEntry  # noqa: E402
from pipeline.quality_metrics import PipelineMetrics, TokenUsage  # noqa: E402

# ---------------------------------------------------------------------------
# Lazy imports, resolved before the first test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="session")
def _resolve_lazy_imports() -> None:
    """Bind every name a `__lazy_modules__` declaration left lazy.

    On Python 3.15 `mock.patch` reads the original out of the module's
    `__dict__`, which for a name not yet used is the lazy proxy rather than
    the function: it then builds a `MagicMock` where the target is async, and
    restores the proxy afterwards, so the name never resolves. Patching a
    name at its source module would instead bind the mock here for the rest
    of the session, at whichever use came first. Resolving them all up front
    gives 3.15 the eager semantics every test was written against; on 3.14
    there is nothing to resolve.
    """
    lazy_import_type = getattr(types, "LazyImportType", None)
    if lazy_import_type is None:
        return
    for module_name, module in list(sys.modules.items()):
        if module_name.split(".")[0] not in {"pipeline", "scripts"}:
            continue
        for name, value in list(vars(module).items()):
            if isinstance(value, lazy_import_type):
                getattr(module, name)


# ---------------------------------------------------------------------------
# The trait vocabulary and the gene aliases, made independent of the disease
# ---------------------------------------------------------------------------

# The loaded vocabulary's tracked keys, for a test that needs any valid trait
# and nothing about which one. Sorted, so the choice is stable across runs.
_TRACKED: Final[tuple[str, ...]] = tuple(sorted(TRACKED_TRAITS))

# A vocabulary of the test suite's own, for the tests of folding, dropping
# and deduplicating: what those behaviours do must not depend on which
# synonyms and untracked terms disease/vocabulary.json happens to declare.
SYNTHETIC_TRAITS: Final[tuple[str, ...]] = ("TRAIT-A", "TRAIT-B", "TRAIT-C")
SYNTHETIC_SYNONYMS: Final[dict[str, str]] = {"trait-a-spelling": "TRAIT-A"}
SYNTHETIC_UNTRACKED: Final[tuple[str, ...]] = ("UNTRACKED-X", "UNTRACKED-Y")
SYNTHETIC_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "GENEA/B": ("GENEA", "GENEB"),
    "OLDSYM": ("NEWSYM",),
}


@pytest.fixture
def tracked_traits() -> tuple[str, ...]:
    """The loaded vocabulary's tracked keys, sorted; at least two of them."""
    assert len(_TRACKED) >= 2, "disease/vocabulary.json declares fewer than two traits"
    return _TRACKED


@dataclass(frozen=True)
class TraitVocabulary:
    tracked: tuple[str, ...]
    synonyms: dict[str, str]
    untracked: tuple[str, ...]


@pytest.fixture
def trait_vocabulary(monkeypatch: pytest.MonkeyPatch) -> TraitVocabulary:
    """Put the synthetic vocabulary in front of every consumer.

    `extraction_models` validates against `CANONICAL_TRAITS` at call time,
    `data_merger` folds and filters through its own imported names, and the
    export's `tables` module carries the rewrites and the untracked set --
    all module globals, all read when the function runs, so patching them
    is enough and nothing is re-imported.
    """
    import pipeline.data_merger as merger
    import pipeline.export.tables as tables
    import pipeline.extraction_models as models

    canonical = SYNTHETIC_TRAITS + tuple(SYNTHETIC_SYNONYMS) + SYNTHETIC_UNTRACKED
    monkeypatch.setattr(models, "CANONICAL_TRAITS", canonical)
    monkeypatch.setattr(models, "TRACKED_TRAITS", frozenset(SYNTHETIC_TRAITS))
    monkeypatch.setattr(models, "TRAIT_SYNONYMS", dict(SYNTHETIC_SYNONYMS))
    monkeypatch.setattr(merger, "TRACKED_TRAITS", frozenset(SYNTHETIC_TRAITS))
    monkeypatch.setattr(merger, "TRAIT_SYNONYMS", dict(SYNTHETIC_SYNONYMS))
    monkeypatch.setattr(tables, "_TRAIT_REWRITES", tuple(SYNTHETIC_SYNONYMS.items()))
    monkeypatch.setattr(tables, "_UNTRACKED_TRAITS", frozenset(SYNTHETIC_UNTRACKED))
    return TraitVocabulary(
        tracked=SYNTHETIC_TRAITS,
        synonyms=dict(SYNTHETIC_SYNONYMS),
        untracked=SYNTHETIC_UNTRACKED,
    )


@pytest.fixture
def gene_aliases(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[str, ...]]:
    """Put the synthetic alias map in front of both of its consumers.

    `annotations._LOOKUP_ALIASES` fans a curated key out to the symbols an
    external database knows; `data_merger._CANONICAL_GENE_SYMBOLS` folds an
    extracted symbol back onto the curated key. Both are read at call time,
    and `canonical_gene_symbol` is imported by name into `main` and
    `run_report`, which is why the dict is patched rather than the function.
    """
    import pipeline.annotations as annotations
    import pipeline.data_merger as merger

    monkeypatch.setattr(annotations, "_LOOKUP_ALIASES", dict(SYNTHETIC_ALIASES))
    monkeypatch.setattr(
        merger, "_CANONICAL_GENE_SYMBOLS", merger.reverse_aliases(SYNTHETIC_ALIASES)
    )
    return dict(SYNTHETIC_ALIASES)

# ---------------------------------------------------------------------------
# GeneEntry factory
# ---------------------------------------------------------------------------


@pytest.fixture
def make_gene_entry():
    """Factory fixture for GeneEntry with sensible defaults."""

    def _make(
        gene_symbol: str = "NOTCH3",
        protein_name: str | None = "Notch receptor 3",
        gwas_trait: list[str] | None = None,
        mendelian_randomization: bool = False,
        omics_evidence: list[str] | None = None,
        confidence: float = 0.9,
        causal_evidence_summary: str | None = "Strong GWAS association",
        pmid: str = "12345678",
        source_quote: str = (
            "NOTCH3 mutations were significantly associated with white "
            "matter hyperintensity volume (p=2.1e-9)."
        ),
    ) -> GeneEntry:
        return GeneEntry(
            gene_symbol=gene_symbol,
            protein_name=protein_name,
            gwas_trait=gwas_trait or [],
            mendelian_randomization=mendelian_randomization,
            omics_evidence=omics_evidence or [],
            confidence=confidence,
            causal_evidence_summary=causal_evidence_summary,
            pmid=pmid,
            source_quote=source_quote,
        )

    return _make


@pytest.fixture
def sample_gene_entry(make_gene_entry):
    """A single GeneEntry with typical values."""
    return make_gene_entry()


@pytest.fixture
def sample_gene_entries(make_gene_entry):
    """Multiple GeneEntry instances for batch testing."""
    return [
        make_gene_entry(gene_symbol="NOTCH3", pmid="11111111"),
        make_gene_entry(
            gene_symbol="HTRA1",
            protein_name="Serine protease HTRA1",
            gwas_trait=[_TRACKED[0]],
            confidence=0.95,
            pmid="22222222",
        ),
        make_gene_entry(
            gene_symbol="COL4A1",
            protein_name="Collagen type IV alpha 1",
            gwas_trait=[_TRACKED[1], _TRACKED[0]],
            omics_evidence=["TWAS"],
            confidence=0.85,
            pmid="33333333",
        ),
    ]


# ---------------------------------------------------------------------------
# PipelineConfig variants
# ---------------------------------------------------------------------------


@pytest.fixture
def config():
    """Default pipeline config."""
    return PipelineConfig()


# ---------------------------------------------------------------------------
# Committed data
# ---------------------------------------------------------------------------

_DATA_DIR = Path(_project_root) / "data"


@pytest.fixture
def committed_rows():
    """Rows of a committed data/*.json, or a skip when the file is empty.

    A test that pins key order against `[0]` of a committed file has nothing
    to compare on the empty export a fork starts from (`deno task
    data:empty`); skipping with the file's name is the honest vacuous pass,
    and the pin returns the moment the fork has rows.
    """

    def read(name: str) -> list[dict[str, Any]]:
        rows = json.loads((_DATA_DIR / name).read_text(encoding="utf-8"))
        if not rows:
            pytest.skip(f"data/{name} is empty -- nothing committed to pin against")
        return rows

    return read


# ---------------------------------------------------------------------------
# Metrics fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_metrics():
    """Fresh PipelineMetrics with all zeros."""
    return PipelineMetrics()


@pytest.fixture
def populated_metrics():
    """PipelineMetrics with realistic values."""
    return PipelineMetrics(
        papers_processed=10,
        fulltext_retrieved=7,
        abstract_only=3,
        genes_extracted=25,
        genes_validated=20,
        genes_rejected=5,
        token_usage=TokenUsage(
            input_tokens=50_000,
            output_tokens=10_000,
            cache_creation_input_tokens=5_000,
            cache_read_input_tokens=15_000,
        ),
    )


# ---------------------------------------------------------------------------
# Mock Anthropic helpers
# ---------------------------------------------------------------------------


@dataclass
class MockUsage:
    """Mimics anthropic response.usage."""

    input_tokens: int = 1000
    output_tokens: int = 500
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    # The SDK types it optional; None is a response that reports no split.
    output_tokens_details: Any = None


@dataclass
class MockTextBlock:
    """Mimics a text content block.

    `citations` is where the API hangs the spans that verify a quote --
    on text blocks only, never on a tool_use block.
    """

    type: str = "text"
    text: str = ""
    citations: list[Any] | None = None


@dataclass
class MockThinkingBlock:
    """Mimics a thinking content block."""

    type: str = "thinking"
    thinking: str = "reasoning..."


@dataclass
class MockCitation:
    """Mimics one char_location citation on a text block."""

    cited_text: str
    start_char_index: int = 0
    end_char_index: int = 0
    type: str = "char_location"

    def __post_init__(self):
        if self.end_char_index == 0:
            self.end_char_index = self.start_char_index + len(self.cited_text)


@dataclass
class MockToolUseBlock:
    """Mimics a tool_use content block.

    The genes arrive here rather than as JSON in a text block: the schema
    rides on a strict tool, because output_config.format and citations are
    mutually exclusive.
    """

    type: str = "tool_use"
    name: str = "report_genes"
    input: dict[str, Any] | None = None

    def __post_init__(self):
        if self.input is None:
            self.input = {"genes": []}


@dataclass
class MockAnthropicResponse:
    """Mimics an Anthropic message response."""

    usage: MockUsage | None = None
    content: list[Any] | None = None
    parsed_output: Any = None
    stop_reason: str = "end_turn"
    # Populated by the API only when stop_reason is "refusal".
    stop_details: Any = None

    def __post_init__(self):
        if self.usage is None:
            self.usage = MockUsage()
        if self.content is None:
            self.content = [MockTextBlock(text=""), MockToolUseBlock()]


@pytest.fixture
def mock_anthropic_response():
    """Factory for mock Anthropic responses.

    `text` is the prose the model writes before calling the tool -- with
    tool_choice "auto" there is always some, and it is what citations
    attach to. `genes` is the tool call's input; `omit_tool_call` builds
    the one response shape that has none, which is a retryable fault
    rather than an empty result.
    """

    def _make(
        text: str = "The genes are reported in the tool call.",
        *,
        genes: dict[str, Any] | None = None,
        omit_tool_call: bool = False,
        cited: list[str] | None = None,
        input_tokens: int = 1000,
        output_tokens: int = 500,
        include_thinking: bool = False,
    ) -> MockAnthropicResponse:
        content: list[Any] = []
        if include_thinking:
            content.append(MockThinkingBlock())
        content.append(
            MockTextBlock(
                text=text,
                citations=[MockCitation(cited_text=q) for q in cited]
                if cited
                else None,
            )
        )
        if not omit_tool_call:
            content.append(MockToolUseBlock(input=genes or {"genes": []}))
        return MockAnthropicResponse(
            usage=MockUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
            content=content,
        )

    return _make


# ---------------------------------------------------------------------------
# Autouse fixtures to reset module-level singletons
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_validation_cache():
    """Clear the NCBI gene cache after each test."""
    from collections import OrderedDict

    yield
    import pipeline.validation as val

    val._gene_cache = OrderedDict()


@pytest.fixture(autouse=True)
def _reset_llm_client() -> Iterator[None]:
    """The extraction module caches one client at module scope."""
    yield
    import pipeline.llm_extraction as llm_extraction

    llm_extraction._client = None


@pytest.fixture(autouse=True)
def _reset_validation_client():
    """Clear the shared validation HTTP client and throttle state after each test."""
    yield
    import pipeline.ncbi_http as ncbi_http
    import pipeline.validation as val

    val._client_manager.reset()
    val._ncbi_semaphore = None
    val._cache_lock = None
    ncbi_http.reset_pacing()


@pytest.fixture(autouse=True)
def _reset_database_singleton():
    """Clear the Database singleton state after each test."""
    yield
    from pipeline.database import Database

    Database._pool = None
    Database._config = None


@pytest.fixture(autouse=True)
def _reset_pdf_client():
    """Clear the shared pdf_retrieval HTTP client after each test."""
    yield
    import pipeline.pdf_retrieval as pdf

    pdf._client_manager.reset()


@pytest.fixture(autouse=True)
def _reset_pubmed_config():
    """Reset Entrez configured flag after each test."""
    yield
    import pipeline.pubmed_search as ps

    ps._entrez_configured = False


@pytest.fixture(autouse=True)
def _reset_main_client():
    """Clear the metadata client in main after each test."""
    yield
    import pipeline.main as m

    m._metadata_client_manager.reset()


@pytest.fixture(autouse=True)
def _reset_ncbi_gene_client():
    """Clear the shared ncbi_gene_fetch HTTP client and cache after each test."""
    from collections import OrderedDict

    yield
    import pipeline.ncbi_gene_fetch as ncbi

    ncbi._client_manager.reset()
    ncbi._gene_cache = OrderedDict()
    ncbi._ncbi_semaphore = None
    ncbi._cache_lock = None


@pytest.fixture(autouse=True)
def _reset_uniprot_client():
    """Clear the shared uniprot_fetch HTTP client and cache after each test."""
    from collections import OrderedDict

    yield
    import pipeline.uniprot_fetch as uni

    uni._client_manager.reset()
    uni._uniprot_cache = OrderedDict()
    uni._uniprot_semaphore = None
    uni._cache_lock = None


@pytest.fixture(autouse=True)
def _reset_opentargets_client():
    """Clear the shared opentargets_fetch HTTP client and cache after each test."""
    from collections import OrderedDict

    yield
    import pipeline.opentargets_fetch as ot

    ot._client_manager.reset()
    ot._target_cache = OrderedDict()
    ot._opentargets_semaphore = None
    ot._cache_lock = None


@pytest.fixture(autouse=True)
def _reset_orphadata_client():
    """Clear the shared orphadata_fetch HTTP client and cache after each test."""
    from collections import OrderedDict

    yield
    import pipeline.orphadata_fetch as od

    od._client_manager.reset()
    od._orphadata_cache = OrderedDict()
    od._orphadata_semaphore = None
    od._cache_lock = None


@pytest.fixture(autouse=True)
def _reset_clinvar_client():
    """Clear the shared clinvar_fetch HTTP client and cache after each test."""
    from collections import OrderedDict

    yield
    import pipeline.clinvar_fetch as cv

    cv._client_manager.reset()
    cv._clinvar_cache = OrderedDict()
    cv._clinvar_semaphore = None
    cv._cache_lock = None
    cv._rate_lock = None
    # Without this the pacing clock carries a future slot into the next test,
    # which then really sleeps for it.
    cv._next_request_at = 0.0


@pytest.fixture(autouse=True)
def _reset_pubmed_citations_client():
    """Clear the shared pubmed_citations HTTP client and cache after each test."""
    from collections import OrderedDict

    yield
    import pipeline.pubmed_citations as pc

    pc._client_manager.reset()
    pc._citation_cache = OrderedDict()
    pc._ncbi_semaphore = None
    pc._cache_lock = None


@pytest.fixture(autouse=True)
def _reset_clinical_trials_client():
    """Clear the shared clinical_trials_fetch HTTP client after each test."""
    yield
    import pipeline.clinical_trials_fetch as ctg

    ctg._client_manager.reset()
    ctg._ctg_semaphore = None


@pytest.fixture(autouse=True)
def _isolate_checkpoint(_isolate_credentials, monkeypatch, tmp_path):
    """Point the checkpoint at a temp file for every test.

    Both processing paths write `PipelineConfig().checkpoint_file` as
    papers finish, and the default is `logs/pipeline_checkpoint.jsonl` in
    the working tree. A test that drives either path with a default config
    would otherwise leave a record there that the next test -- or the next
    real run -- restores as extraction it never did.

    It takes `_isolate_credentials` so that this `setenv` lands *after*
    that fixture has cleared the whole `PIPELINE_*` namespace; autouse
    fixtures at one scope otherwise run in definition order, and this one
    is defined first.
    """
    monkeypatch.setenv(
        "PIPELINE_CHECKPOINT_FILE", str(tmp_path / "pipeline_checkpoint.jsonl")
    )


@pytest.fixture(autouse=True)
def _isolate_credentials(monkeypatch):
    """Hide the developer's real .env -- and real database -- from every test.

    pipeline/main.py calls load_dotenv() at *import*, so importing it --
    which most of these modules do transitively -- writes the real .env
    into os.environ for the whole pytest process. Tests that exercise a
    "variable absent" path then silently stop exercising it: populating
    .env locally dropped coverage of pubmed_search's ENTREZ_EMAIL warning
    and config's no-API-key branch, while CI, which has no .env, stayed at
    100% and never showed it. Clearing them here makes the suite hermetic
    in both environments; a test that needs a value sets it itself.

    The whole `PIPELINE_*` namespace goes too. `.env.example` lists twenty
    of them and `PipelineConfig()` reads every one, so a value left in
    `.env` for a local experiment -- `PIPELINE_CONFIDENCE_THRESHOLD_UPDATE`
    or `PIPELINE_LLM_EFFORT`, say -- is what the tests that assert on the
    measured defaults would measure. The three fixtures that redirect a
    `PIPELINE_*` path take this fixture, so their values are set after the
    clearing rather than swept up by it.
    """
    for name in [n for n in os.environ if n.startswith("PIPELINE_")]:
        monkeypatch.delenv(name, raising=False)
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_WORKSPACE_ID",
        "ENTREZ_EMAIL",
        "ENTREZ_KEY",
        "NCBI_API_KEY",
        "UNPAYWALL_EMAIL",
        # The connection settings belong here for a sharper reason than
        # coverage: without them a test that reaches a real
        # `Database.connect()` opened the developer's *production*
        # database and wrote to it. `record_pipeline_run` inside a
        # best-effort `except` made that silent -- in CI, which has no
        # .env, the connect raised and was swallowed and the test passed;
        # locally, with .env populated and PostgreSQL up, the same test
        # inserted a `pipeline_runs` row of zeros. The export publishes
        # the *newest* row, so a suite run could put a fabricated empty
        # run on the About page. Clearing these makes the local run
        # behave exactly as CI's does.
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_USER",
        "DB_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_pipeline_progress_file(_isolate_credentials, tmp_path, monkeypatch):
    """Redirect run_pipeline progress writes to a temp path so tests don't
    clobber the real logs/json/pipeline_progress.json that the dashboard
    polls.

    Takes `_isolate_credentials` for the ordering reason recorded on
    `_isolate_checkpoint`.
    """
    monkeypatch.setenv(
        "PIPELINE_PROGRESS_FILE", str(tmp_path / "pipeline_progress.json")
    )


@pytest.fixture(autouse=True)
def _isolate_event_log(_isolate_credentials, tmp_path, monkeypatch):
    """Point the audit event log at a temp file for every test.

    `_stream_and_parse` (anthropic_client.py) writes one
    "paper_extraction_thinking" event per streamed response through
    `PipelineConfig().event_db_path`, and the default is
    `logs/events.db` in the working tree. A test that streams a mocked
    response with a default config would otherwise write real thinking
    text into that file, following the same reasoning as
    `_isolate_checkpoint` and `_isolate_pipeline_progress_file` above --
    including their dependency on `_isolate_credentials`.
    """
    monkeypatch.setenv("PIPELINE_EVENT_DB_PATH", str(tmp_path / "events.db"))


# ---------------------------------------------------------------------------
# VCR (pytest-recording) — tests/pipeline/test_extraction_golden.py
# ---------------------------------------------------------------------------


# Response headers echo two account identifiers straight back:
# anthropic-organization-id and anthropic-workspace-id. Neither can
# authenticate anything, but both name a real account and the cassettes are
# committed to a public repository. `filter_headers` does not reach a
# response -- VCR applies it to requests only -- so scrubbing them needs
# this hook.
_REDACTED_RESPONSE_HEADERS: Final = (
    "anthropic-organization-id",
    "anthropic-workspace-id",
)


# `filter_query_parameters` rewrites a URL and never inspects a body.
# Biopython switches Entrez from GET to POST once a query grows past a length
# threshold and then sends the very same parameters in the body, so the URL
# guard protects short requests and silently misses long ones -- a [uid]
# disjunction over a gold standard being exactly a long one.
_REDACTED_BODY_PARAMS: Final = re.compile(rb"\b(email|api_key)=[^&\s]*")


def _redact_request_credentials(request: Any) -> Any:
    """Blank credentials out of a request body before it is written."""
    body = request.body
    if not body:
        return request
    raw = body if isinstance(body, bytes) else str(body).encode()
    request.body = _REDACTED_BODY_PARAMS.sub(rb"\1=REDACTED", raw)
    return request


def _redact_response_headers(response: dict[str, Any]) -> dict[str, Any]:
    """Blank account identifiers out of a response before it is written."""
    headers = response.get("headers") or {}
    for name in list(headers):
        if name.lower() in _REDACTED_RESPONSE_HEADERS:
            headers[name] = ["REDACTED"]
    return response


@pytest.fixture(scope="module")
def vcr_config() -> dict[str, object]:
    """Never let an API key reach a committed cassette.

    `record_mode` is deliberately absent here. pytest-recording merges this
    fixture's dict with any `pytest.mark.vcr(**kwargs)` and, if the result
    contains a `record_mode` key, that value wins outright — it is not
    merely a default, it overrides an explicit `--record-mode=once` passed
    on the command line (see `use_cassette` in
    `pytest_recording/_vcr.py`). Hard-coding "none" here would make that
    flag silently do nothing, so the recording command in
    `test_extraction_golden.py`'s module docstring would never really
    record. Leaving `record_mode` unset lets pytest-recording's own default
    ("none" unless `--record-mode` says otherwise) do the job instead.
    """
    return {
        "filter_headers": [
            ("x-api-key", "REDACTED"),
            ("authorization", "REDACTED"),
            # Not a credential, but an account identifier, and
            # `build_async_client` sends it on every request once
            # ANTHROPIC_WORKSPACE_ID is set. Cassettes are committed.
            ("anthropic-workspace-id", "REDACTED"),
        ],
        # NCBI E-utilities carries its credentials in the URL, not in a
        # header, so `filter_headers` above does not reach them.
        # `_isolate_credentials` normally keeps a real key out of any request
        # to begin with; this covers the case that fixture's docstring
        # invites, where a test sets a value it needs and would otherwise
        # record it. Paired with `before_record_request` below, which is what
        # covers the same parameters when Biopython sends them in a body.
        "filter_query_parameters": [
            ("api_key", "REDACTED"),
            ("email", "REDACTED"),
        ],
        "before_record_request": _redact_request_credentials,
        "before_record_response": _redact_response_headers,
    }


# The skip machinery for test_extraction_golden.py's gitignored inputs lives in
# tests/pipeline/csvd/conftest.py beside the suite it serves.


# ---------------------------------------------------------------------------
# A throwaway PostgreSQL, from the hardened image
# ---------------------------------------------------------------------------

_TEST_IMAGE: Final = "dhi.io/postgres:18-alpine3.23"
_TEST_CONTAINER: Final = "csvd-pg-pytest"
# An already-running throwaway PostgreSQL, as a libpq URL. It is what lets
# these tests run somewhere Apple's macOS-only `container` runtime does not
# exist -- CI sets it to its own `postgres` service.
_TEST_DB_URL_VAR: Final = "CSVD_TEST_DB_URL"


def _settings_from_url(url: str) -> dict[str, str]:
    """Split a `postgresql://user:password@host:port/name` URL into DB_*.

    Raises rather than skips on a malformed value: a URL was supplied, so
    the intent was to run these tests, and skipping would report that
    intent as coverage.
    """
    parsed = urlsplit(url)
    name = unquote(parsed.path).lstrip("/")
    if not (parsed.hostname and parsed.username and name):
        raise ValueError(
            f"{_TEST_DB_URL_VAR} must be "
            "postgresql://user:password@host:port/database, not " + url
        )
    return {
        "DB_HOST": parsed.hostname,
        "DB_PORT": str(parsed.port or 5432),
        "DB_NAME": name,
        "DB_USER": unquote(parsed.username),
        "DB_PASSWORD": unquote(parsed.password or ""),
    }


def _container(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run Apple's `container` CLI, the macOS-native container runtime."""
    return subprocess.run(
        ("container", *args), capture_output=True, text=True, check=check
    )


def _container_is_usable() -> str | None:
    """Return the reason the container cannot be started, or None."""
    if shutil.which("container") is None:
        return "the container CLI is not installed"
    if _container("system", "status", check=False).returncode != 0:
        return "the container system service is not running (`container system start`)"
    if _container("image", "inspect", _TEST_IMAGE, check=False).returncode != 0:
        return (
            f"{_TEST_IMAGE} is not present locally. It is a Docker Hardened "
            "Image: `container registry login dhi.io` then "
            f"`container image pull {_TEST_IMAGE}`."
        )
    return None


def _container_address(name: str, *, timeout: float = 30.0) -> str:
    """Return the container's own IPv4 address.

    Every container gets an address on the runtime's `default` network
    rather than a port published onto the host, so this -- not a
    host-side port -- is what the tests connect to. `container inspect`
    reports it in CIDR form under `status.networks`, and the interface
    is configured a moment after `container run` returns, so poll for it.
    """
    deadline = time.monotonic() + timeout
    while True:
        result = _container("inspect", name, check=False)
        if result.returncode == 0:
            networks = json.loads(result.stdout)[0]["status"]["networks"]
            for network in networks:
                address = network.get("ipv4Address")
                if address:
                    return address.partition("/")[0]
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"{name} never reported an IPv4 address:"
                f"\n{result.stdout}{result.stderr}"
            )
        time.sleep(0.2)


def _wait_until_ready(
    settings: dict[str, str],
    *,
    timeout: float = 60.0,
    container_name: str | None = None,
) -> None:
    """Block until a real connection succeeds from *this* process.

    Not `container exec pg_isready`: the entrypoint runs initdb against a
    temporary socket-only server first, and pg_isready answers yes to that
    one several seconds before the TCP port is listening. The probe has to
    be the thing the tests will actually do, over the address they will
    actually use, or the fixture races its own container.

    `container_name` is None for a database this fixture did not start --
    there is no `container` CLI to ask for logs where that one runs.
    """
    import asyncpg

    async def _probe() -> None:
        connection = await asyncpg.connect(
            host=settings["DB_HOST"],
            port=int(settings["DB_PORT"]),
            database=settings["DB_NAME"],
            user=settings["DB_USER"],
            password=settings["DB_PASSWORD"],
        )
        await connection.close()

    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            asyncio.run(_probe())
            return
        except Exception as exc:  # any failure means "not yet"
            last = exc
            time.sleep(0.3)
    tail = ""
    if container_name is not None:
        logs = _container("logs", "-n", "20", container_name, check=False)
        tail = f" container log tail:\n{logs.stdout}{logs.stderr}"
    raise RuntimeError(
        f"{settings['DB_HOST']}:{settings['DB_PORT']} never accepted a"
        f" connection ({last});{tail}"
    )


@pytest.fixture(scope="session")
def throwaway_postgres() -> Iterator[dict[str, str]]:
    """A disposable PostgreSQL for tests that need a real one.

    The production database is a *local* PostgreSQL whose credentials sit
    in the developer's `.env`, and `_isolate_credentials` hides those from
    every test precisely so none can reach it. A test that wants real SQL
    asks for this instead: `dhi.io/postgres:18-alpine3.23`, run under its
    own name on its own address by Apple's `container` runtime, migrated to
    head, and destroyed with the session.

    That is deliberately *not* the glibc `dhi.io/postgres:18.6` the
    `regenerate-data` skill documents for a mounted data directory. The
    skill pins glibc because musl collates text differently and would
    reorder every index in an existing `PGDATA`; this fixture mounts no
    volume and initdb's a fresh cluster every session, so it has no data
    directory to stay compatible with. Both images serve PostgreSQL 18.6.
    The one thing that constraint buys has to be kept by hand:
    `test_database_integration.py`, the only consumer, asserts on no SQL
    text ordering, and an assertion added here that depends on a text
    `ORDER BY` would be comparing musl's collation against production's
    glibc.

    It is never the long-lived container a developer may keep running --
    it is created and deleted here by name, so nothing a test does
    outlives the session.

    No volume is mounted, so the data directory lives in the container's
    writable layer and goes with it -- `PGDATA` sits at
    `/var/lib/postgresql/18/data/pgdata` in this image, one level deeper
    than the Debian image's `/var/lib/postgresql/18/data`, and the point
    here is that nothing survives the test session at all.

    `CSVD_TEST_DB_URL` comes first, and points at a throwaway PostgreSQL
    someone else started -- CI's `postgres` service, which is what makes
    these tests run on `ubuntu-latest` rather than only on a developer
    Mac, where `container` (macOS-only) and the `dhi.io` login live. The
    database it names is migrated to head and written to; never point it
    at anything whose contents matter.

    Skips rather than fails when neither that variable nor the runtime and
    its image is available.
    """
    url = os.environ.get(_TEST_DB_URL_VAR)
    if url:
        settings = _settings_from_url(url)
        _wait_until_ready(settings)
        _upgrade_to_head(settings)
        yield settings
        return

    reason = _container_is_usable()
    if reason is not None:
        pytest.skip(
            f"no PostgreSQL: {_TEST_DB_URL_VAR} is unset and {reason}",
        )

    settings = {
        "DB_NAME": "csvd_pytest",
        "DB_USER": "csvd_pytest",
        "DB_PASSWORD": "csvd_pytest",
    }

    _container("delete", "--force", _TEST_CONTAINER, check=False)
    _container(
        "run",
        "--detach",
        "--name",
        _TEST_CONTAINER,
        "--env",
        f"POSTGRES_USER={settings['DB_USER']}",
        "--env",
        f"POSTGRES_PASSWORD={settings['DB_PASSWORD']}",
        "--env",
        f"POSTGRES_DB={settings['DB_NAME']}",
        _TEST_IMAGE,
    )
    try:
        # No port is published: the container answers on 5432 at its own
        # address, so the production server on the host's 5432 is neither
        # shadowed nor reachable from here.
        settings["DB_HOST"] = _container_address(_TEST_CONTAINER)
        settings["DB_PORT"] = "5432"
        _wait_until_ready(settings, container_name=_TEST_CONTAINER)
        _upgrade_to_head(settings)
        yield settings
    finally:
        _container("delete", "--force", _TEST_CONTAINER, check=False)


def _upgrade_to_head(settings: dict[str, str]) -> None:
    """Run Alembic against the test database, in-process.

    `alembic/env.py` reads the same `DB_*` variables the pipeline does and
    calls `load_dotenv()`, which does not override what is already set --
    so putting the container's settings in `os.environ` first is what
    keeps the developer's `.env` out of the upgrade.
    """
    from alembic import command
    from alembic.config import Config

    alembic_root = Path(_project_root) / "pipeline" / "alembic"
    previous = {name: os.environ.get(name) for name in settings}
    os.environ.update(settings)
    try:
        config = Config()
        config.set_main_option("script_location", str(alembic_root))
        command.upgrade(config, "head")
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture
def database_env(throwaway_postgres, monkeypatch) -> dict[str, str]:
    """Point the pipeline's database layer at the throwaway PostgreSQL.

    Ordering matters: `_isolate_credentials` is autouse and clears these,
    so this must be requested explicitly to put them back, and it puts
    back the throwaway database's rather than the developer's.
    """
    for name, value in throwaway_postgres.items():
        monkeypatch.setenv(name, value)
    return throwaway_postgres
