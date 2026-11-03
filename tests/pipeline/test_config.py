"""Tests for pipeline.config — defaults, env-var overrides, constants."""

import os
import subprocess

import pytest

from pipeline.config import (
    EXTRACTION_MODEL,
    MODEL_MAX_OUTPUT_TOKENS,
    PROJECT_ROOT,
    PipelineConfig,
    default_pdf_threads,
    validate_pmid,
)
from pipeline.prompts import (
    _PROMPTS,
    PROMPT_VERSIONS,
    PROMPT_VERSIONS_WITHOUT_PROVENANCE,
)


@pytest.fixture(scope="module")
def leaked_dotenv_override():
    """Put a `PIPELINE_*` value in os.environ the way load_dotenv() does.

    Module scope is the whole point: a higher-scoped fixture is set up
    before the function-scoped autouse ones, which is the order a real
    `.env` arrives in -- `pipeline/main.py` calls `load_dotenv()` at
    import, long before any fixture runs.
    """
    os.environ["PIPELINE_LLM_EFFORT"] = "low"
    yield
    os.environ.pop("PIPELINE_LLM_EFFORT", None)


def test_no_dotenv_override_reaches_a_default_config(leaked_dotenv_override) -> None:
    """The measured defaults are measured, not read off the developer's .env.

    `.env.example` lists twenty `PIPELINE_*` keys and `PipelineConfig()`
    reads all of them, so uncommenting one for a local experiment used to
    change what every default-config assertion in this file measures --
    the effort sweeps, the two confidence floors, the checkpoint
    fingerprint. `_isolate_credentials` clears the namespace for that
    reason, and this is what fails if it stops.
    """
    assert PipelineConfig().llm_effort == "high"


class TestPipelineConfigDefaults:
    """Verify default values are sensible and stable."""

    def test_default_model(self):
        cfg = PipelineConfig()
        assert cfg.llm_model == "claude-opus-5-5"

    def test_default_max_tokens_matches_model(self):
        cfg = PipelineConfig()
        assert cfg.llm_max_tokens == MODEL_MAX_OUTPUT_TOKENS

    def test_default_effort(self):
        cfg = PipelineConfig()
        assert cfg.llm_effort == "high"

    def test_the_model_is_pinned_and_not_environment_overridable(
        self, monkeypatch
    ) -> None:
        """One model, so a stray env var cannot silently change the corpus.

        Extraction output is compared across runs and against recorded
        cassettes; a per-machine model override would make two runs of the
        same code incomparable without anything saying so.
        """
        monkeypatch.setenv("PIPELINE_LLM_MODEL", "claude-haiku-4-5")
        assert PipelineConfig().llm_model == "claude-opus-5-5"

    def test_effort_defaults_to_high_and_stays_overridable(
        self, monkeypatch
    ) -> None:
        assert PipelineConfig().llm_effort == "high"
        monkeypatch.setenv("PIPELINE_LLM_EFFORT", "medium")
        assert PipelineConfig().llm_effort == "medium"

    def test_thinking_is_always_adaptive(self) -> None:
        assert PipelineConfig().thinking_config == {
            "type": "adaptive",
            "display": "summarized",
        }

    def test_the_schema_travels_on_a_strict_tool_not_output_format(self) -> None:
        """output_config.format is the exact parameter citations reject.

        The API's message is "Citations cannot be enabled when output
        format is set." Strict tool use gives the same schema guarantee
        without setting it.
        """
        config = PipelineConfig()
        tool = config.extraction_tool

        assert tool["name"] == "report_genes"
        assert tool["input_schema"]["type"] == "object"
        assert "format" not in config.output_config

    def test_the_tool_is_not_strict_and_that_is_measured_not_forgotten(self) -> None:
        """strict=True corrupts every free-text string on Claude Opus 5.

        The model writes the right value, cannot close the string, and
        writes its way out -- `...PP4 = 0.91.», ».replace('»','')` -- or
        loops until max_tokens. Measured 9/9 corrupt with strict=True
        against 4/4 clean with strict=False, everything else held fixed.
        source_quote is the field it destroys, which is the one provenance
        rests on. See PipelineConfig.extraction_tool for the full record.

        This asserts False so that flipping it back is a deliberate act
        with a test to answer to, rather than a plausible-looking
        one-word "fix".
        """
        assert PipelineConfig().extraction_tool["strict"] is False

    def test_output_config_states_the_default_effort(self) -> None:
        """Claude Opus 5.5's API default is "medium", not "high".

        Omitting effort at the default used to be wire noise, when the
        model's default and ours agreed. They no longer do, so an omitted
        effort would run every paper one level below what the run report
        and the checkpoint fingerprint claim.
        """
        assert PipelineConfig().output_config == {"effort": "high"}

    def test_output_config_carries_a_non_default_effort(self, monkeypatch) -> None:
        monkeypatch.setenv("PIPELINE_LLM_EFFORT", "medium")
        assert PipelineConfig().output_config == {"effort": "medium"}

    def test_the_tool_constrains_gwas_trait_to_the_shared_vocabulary(self) -> None:
        """The enum is derived from disease/vocabulary.json, never restated.

        And it enumerates the *full* canonical list -- tracked traits and
        `untracked` ones alike. Narrowing it to the tracked set would
        destroy the signal the vocabulary work exists to surface: PVWMH
        was emitted 29 times because the model was correctly naming a
        phenotype the dashboard lacked, and ICH-non-lobar 13 times across
        the committed cassettes. Constrained to the tracked set, either
        silently becomes something else or vanishes.
        """
        import json

        from pipeline.config import PROJECT_ROOT

        vocabulary_path = PROJECT_ROOT / "disease" / "vocabulary.json"
        with vocabulary_path.open(encoding="utf-8") as fh:
            vocabulary = json.load(fh)
        expected = (
            {t["key"] for t in vocabulary["traits"]}
            | {s["from"] for s in vocabulary["synonyms"] if s["source"] == "prompt"}
            | {u["term"] for u in vocabulary["untracked"]}
        )

        schema = PipelineConfig().extraction_tool["input_schema"]
        gene = schema["$defs"]["GeneEntry"]
        assert set(gene["properties"]["gwas_trait"]["items"]["enum"]) == expected

    def test_the_truncation_limit_is_sized_for_a_1m_context(self):
        """100,000 chars was a 200K-context limit; Opus 5.5 has 1M.

        Truncation is still wanted -- an unbounded paper is an unbounded
        bill, and _read_pdf_bytes already caps input at 100 MB -- but the
        ceiling should be a deliberate cost decision rather than a
        leftover from a smaller context window.
        """
        assert PipelineConfig().max_paper_text_chars == 400_000

    def test_default_max_retries(self):
        cfg = PipelineConfig()
        assert cfg.max_retries == 1

    def test_the_two_confidence_floors_have_their_measured_defaults(self):
        """One floor decided two things with opposite risk profiles.

        The update floor sits where gold recall saturates: 0.45 is the
        highest floor that still recovers every reachable gold gene the
        model finds (41/46 across the ten cassettes). The insert floor
        stays a curation policy at 0.65 and is deliberately not swept --
        the gold standard cannot tell an incorrect new gene from an
        unreviewed one.
        """
        cfg = PipelineConfig()
        assert cfg.confidence_threshold_update == 0.45
        assert cfg.confidence_threshold_insert == 0.65
        assert not hasattr(cfg, "confidence_threshold"), (
            "the single floor is gone; every call site names one of the two"
        )

    def test_default_rpm_limit(self):
        cfg = PipelineConfig()
        assert cfg.rpm_limit == 50

    def test_default_tpm_limit(self):
        cfg = PipelineConfig()
        assert cfg.tpm_limit == 100_000

    def test_default_db_pool_sizes(self):
        cfg = PipelineConfig()
        assert cfg.db_pool_min_size == 2
        assert cfg.db_pool_max_size == 10

    def test_days_back_range(self):
        cfg = PipelineConfig()
        assert cfg.min_days_back == 1
        assert cfg.max_days_back == 3650

    def test_default_prompt_version(self, monkeypatch):
        monkeypatch.delenv("PIPELINE_PROMPT_VERSION", raising=False)
        cfg = PipelineConfig()
        assert cfg.prompt_version == "v7"

    def test_construction_does_not_require_api_key(self, monkeypatch):
        """Non-LLM pipeline modes can still build config without Anthropic creds."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg = PipelineConfig()
        assert cfg.llm_model == "claude-opus-5-5"


class TestPipelineConfigEnvOverrides:
    """Verify env-var overrides via monkeypatch."""

    def test_override_max_tokens(self, monkeypatch):
        monkeypatch.setenv("PIPELINE_LLM_MAX_TOKENS", "16000")
        cfg = PipelineConfig()
        assert cfg.llm_max_tokens == 16_000

    def test_override_confidence_thresholds(self, monkeypatch):
        monkeypatch.setenv("PIPELINE_CONFIDENCE_THRESHOLD_UPDATE", "0.55")
        monkeypatch.setenv("PIPELINE_CONFIDENCE_THRESHOLD_INSERT", "0.85")
        cfg = PipelineConfig()
        assert cfg.confidence_threshold_update == 0.55
        assert cfg.confidence_threshold_insert == 0.85

    def test_override_rpm(self, monkeypatch):
        monkeypatch.setenv("PIPELINE_RPM_LIMIT", "100")
        cfg = PipelineConfig()
        assert cfg.rpm_limit == 100

    def test_override_effort(self, monkeypatch):
        monkeypatch.setenv("PIPELINE_LLM_EFFORT", "low")
        cfg = PipelineConfig()
        assert cfg.llm_effort == "low"

    @pytest.mark.parametrize(
        ("name", "value", "type_name"),
        [
            ("PIPELINE_RPM_LIMIT", "not-an-int", "an integer"),
            ("PIPELINE_CONFIDENCE_THRESHOLD_UPDATE", "not-a-float", "a finite float"),
        ],
    )
    def test_invalid_numeric_override_has_actionable_error(
        self, monkeypatch, name, value, type_name
    ):
        monkeypatch.setenv(name, value)

        with pytest.raises(ValueError, match=rf"{name} must be {type_name}"):
            PipelineConfig()

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("YES", True), ("off", False)],
    )
    def test_boolean_override(self, monkeypatch, value, expected):
        monkeypatch.setenv("PIPELINE_CT_ENABLED", value)

        assert PipelineConfig().ct_enabled is expected

    def test_list_override_strips_and_drops_empty_entries(self, monkeypatch):
        monkeypatch.setenv("PIPELINE_CT_SEARCH_TERMS", " WMH, ,lacunar stroke ")

        assert PipelineConfig().ct_search_terms == ("WMH", "lacunar stroke")


class TestConstants:
    """Verify module-level constants are valid."""

    def test_project_root_exists(self):
        assert PROJECT_ROOT.exists()
        assert (PROJECT_ROOT / "pipeline").is_dir()


class TestValidatePmid:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("12345678", "12345678"),
            ("1", "1"),
            ("  12345678  ", "12345678"),
            ("123456789", "123456789"),
        ],
    )
    def test_accepts_valid(self, raw, expected):
        assert validate_pmid(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["abc123", "", "1234567890", "1234-5678"],
    )
    def test_rejects_invalid(self, raw):
        with pytest.raises(ValueError, match="Invalid PMID"):
            validate_pmid(raw)


# ---------------------------------------------------------------------------
# prompt_version — the pre-provenance bypass
# ---------------------------------------------------------------------------


def test_a_pre_provenance_prompt_version_is_refused_at_config_time(
    monkeypatch,
) -> None:
    """A prompt with no verbatim-quote instruction must fail before a run.

    Such a version never tells the model to copy a sentence, but the
    schema still requires a non-blank source_quote -- so the model
    paraphrases or invents one and min_length=1 accepts it. Every quote
    the run stores would be untrustworthy with nothing in the output to
    show it, so it has to fail at startup rather than warn.

    The set is empty now that v7 is the only prompt, and parametrising
    over it made this test silently skip -- an empty parameter set asserts
    nothing while still reporting green. So the rule is tested against a
    synthetic version instead, which is what keeps the guard covered no
    matter what the table happens to hold.
    """
    monkeypatch.setattr(
        "pipeline.config.PROMPT_VERSIONS_WITHOUT_PROVENANCE", frozenset({"v0"})
    )
    with pytest.raises(ValueError, match="predates the verbatim-quote"):
        PipelineConfig(prompt_version="v0")


def test_the_refused_set_is_exactly_the_versions_without_the_block() -> None:
    """Derived from the prompt table, not a hardcoded list.

    The set is empty now that v7 is the only version, and the machinery
    stays for that reason rather than in spite of it: a future version
    that forgets the Provenance block has to land in this set on its own,
    and one that keeps it must not. Asserting emptiness would pin the
    accident; asserting the derivation pins the rule.
    """
    assert set(PROMPT_VERSIONS_WITHOUT_PROVENANCE) == {
        version
        for version, (_, instructions) in _PROMPTS.items()
        if "## Provenance" not in instructions
    }
    for version in set(_PROMPTS) - PROMPT_VERSIONS_WITHOUT_PROVENANCE:
        assert "## Provenance" in _PROMPTS[version][1]


def test_the_production_default_is_accepted() -> None:
    assert PipelineConfig().prompt_version == "v7"
    assert PipelineConfig(prompt_version="v7").prompt_version == "v7"


def test_an_unrecognised_version_is_refused_before_the_run() -> None:
    """A typo'd PIPELINE_PROMPT_VERSION must not become a published method.

    It used to be accepted here and fall back to the default with a warning inside
    build_extraction_prompt -- safe for the prompt, false for every record
    of the run: report_metadata, `pipeline_runs.report`,
    data/pipeline_run.json and the checkpoint fingerprint all carry
    `config.prompt_version` verbatim, so a typo'd PIPELINE_PROMPT_VERSION
    published a name that never existed as the method behind the rows, and
    correcting the typo later discarded a checkpoint whose papers had been
    extracted with the very same prompt.
    """
    with pytest.raises(ValueError, match="not a known prompt"):
        PipelineConfig(prompt_version="v99")


def test_the_refusal_names_the_versions_that_do_exist() -> None:
    """The operator's next move is in the message, not in the source.

    Derived from the registry rather than spelled "v7", so adding v8 needs
    no edit here -- the same reason the refused set is derived above.
    """
    with pytest.raises(ValueError) as raised:
        PipelineConfig(prompt_version="v99")

    assert str(sorted(PROMPT_VERSIONS)) in str(raised.value)


# ---------------------------------------------------------------------------
# The pinned model's reported identity
# ---------------------------------------------------------------------------


def test_the_reported_model_identity_is_the_pinned_one() -> None:
    """model_version and thinking_mode go into every run report.

    They were derived — a regex over the model name, and a lookup against
    the legacy-thinking set — back when the model was selectable. Both are
    constants now, and the run report still has to carry them, so this pins
    what a report claims about the method it used.
    """
    cfg = PipelineConfig()
    assert cfg.llm_model == EXTRACTION_MODEL
    assert cfg.model_version == "5.5"
    assert cfg.thinking_mode == "adaptive"


# ---------------------------------------------------------------------------
# PipelineConfig.extraction_schema — hoisted from anthropic_client._OUTPUT_CONFIG
# (Task 15 pre-flight ruling: one definition shared by streaming and batch)
# ---------------------------------------------------------------------------


class TestExtractionTool:
    def test_shape_matches_the_tool_contract(self):
        cfg = PipelineConfig()
        tool = cfg.extraction_tool
        assert tool["name"] == "report_genes"
        assert "GeneEntry" in tool["input_schema"]["$defs"]

    def test_is_cached_on_the_instance(self):
        """Recomputing transform_schema() on every call would be wasted
        work — same instance must return the identical object."""
        cfg = PipelineConfig()
        assert cfg.extraction_tool is cfg.extraction_tool

    def test_each_config_instance_gets_its_own_copy(self):
        """Caching is per-instance, not a shared module global — two
        configs must not hand back the same object."""
        first, second = PipelineConfig(), PipelineConfig()
        assert first.extraction_tool is not second.extraction_tool
        assert first.extraction_tool == second.extraction_tool


class TestPdfFallbackSettings:
    """The Docling knobs. Every one is a runtime-cost or correctness lever, so
    each gets a default, an override and (where nonsense is possible) a
    fail-fast check with an actionable message."""

    def test_defaults(self):
        cfg = PipelineConfig()
        assert cfg.pdf_ocr is True
        # CoreML measured 1.65x slower than CPU on Apple Silicon.
        assert cfg.pdf_ocr_coreml is False
        assert cfg.pdf_device == "auto"
        assert cfg.pdf_num_threads == default_pdf_threads()
        assert cfg.pdf_timeout_seconds == 120.0
        assert cfg.pdf_max_pages == 200
        assert cfg.pdf_artifacts_path == ""

    def test_overrides(self, monkeypatch):
        monkeypatch.setenv("PIPELINE_PDF_OCR", "no")
        monkeypatch.setenv("PIPELINE_PDF_OCR_COREML", "yes")
        monkeypatch.setenv("PIPELINE_PDF_DEVICE", "mps")
        monkeypatch.setenv("PIPELINE_PDF_NUM_THREADS", "8")
        monkeypatch.setenv("PIPELINE_PDF_TIMEOUT_SECONDS", "45.5")
        monkeypatch.setenv("PIPELINE_PDF_MAX_PAGES", "50")
        monkeypatch.setenv("PIPELINE_PDF_ARTIFACTS_PATH", "/models")

        cfg = PipelineConfig()
        assert cfg.pdf_ocr is False
        assert cfg.pdf_ocr_coreml is True
        assert cfg.pdf_device == "mps"
        assert cfg.pdf_num_threads == 8
        assert cfg.pdf_timeout_seconds == 45.5
        assert cfg.pdf_max_pages == 50
        assert cfg.pdf_artifacts_path == "/models"

    @pytest.mark.parametrize("device", ["auto", "cpu", "mps", "xpu", "cuda", "cuda:1"])
    def test_every_device_docling_accepts_is_accepted(self, monkeypatch, device):
        monkeypatch.setenv("PIPELINE_PDF_DEVICE", device)
        assert PipelineConfig().pdf_device == device

    def test_an_unknown_device_fails_at_construction(self, monkeypatch):
        """Docling validates this too, but only when the converter is built --
        the first PDF, potentially an hour into a run."""
        monkeypatch.setenv("PIPELINE_PDF_DEVICE", "metal")

        with pytest.raises(ValueError, match="pdf_device must be"):
            PipelineConfig()

    def test_a_device_with_a_trailing_newline_fails_at_construction(self):
        """`$` also matches before a final newline, so `match` let "cpu\\n"
        through to the converter -- the late failure this check exists to
        bring forward."""
        with pytest.raises(ValueError, match="pdf_device must be"):
            PipelineConfig(pdf_device="cpu\n")

    @pytest.mark.parametrize(
        ("name", "value", "message"),
        [
            ("PIPELINE_PDF_NUM_THREADS", "0", "pdf_num_threads must be >= 1"),
            ("PIPELINE_PDF_TIMEOUT_SECONDS", "0", "pdf_timeout_seconds must be > 0"),
            ("PIPELINE_PDF_MAX_PAGES", "0", "pdf_max_pages must be >= 1"),
        ],
    )
    def test_nonsensical_values_fail_fast(self, monkeypatch, name, value, message):
        monkeypatch.setenv(name, value)

        with pytest.raises(ValueError, match=message):
            PipelineConfig()


class TestDefaultPdfThreads:
    """Performance cores, not every core.

    Measured on an M1 Pro (8P+2E): 8 threads converts the benchmark corpus
    in 59.1s against 69.3s at 4, while 10 -- what os.cpu_count() reports --
    regresses to 81.5s. So the obvious cpu_count() default would have been
    slower than the constant it replaced.
    """

    @staticmethod
    def _fresh():
        default_pdf_threads.cache_clear()
        return default_pdf_threads

    def test_uses_the_performance_core_count_on_macos(self, mocker):
        mocker.patch("pipeline.config.sys.platform", "darwin")
        run = mocker.patch("pipeline.config.subprocess.run")
        run.return_value.stdout = "8\n"

        assert self._fresh()() == 8
        assert run.call_args.args[0] == [
            "sysctl",
            "-n",
            "hw.perflevel0.logicalcpu",
        ]

    def test_falls_back_to_every_core_off_macos(self, mocker):
        """A homogeneous CPU has no slow tier to avoid."""
        mocker.patch("pipeline.config.sys.platform", "linux")
        mocker.patch("pipeline.config.os.cpu_count", return_value=16)
        run = mocker.patch("pipeline.config.subprocess.run")

        assert self._fresh()() == 16
        run.assert_not_called()

    @pytest.mark.parametrize(
        "failure",
        [OSError("no sysctl"), subprocess.SubprocessError("boom")],
    )
    def test_a_sysctl_failure_is_not_fatal(self, mocker, failure):
        mocker.patch("pipeline.config.sys.platform", "darwin")
        mocker.patch("pipeline.config.os.cpu_count", return_value=12)
        mocker.patch("pipeline.config.subprocess.run", side_effect=failure)

        assert self._fresh()() == 12

    def test_unparseable_output_falls_back(self, mocker):
        mocker.patch("pipeline.config.sys.platform", "darwin")
        mocker.patch("pipeline.config.os.cpu_count", return_value=12)
        run = mocker.patch("pipeline.config.subprocess.run")
        run.return_value.stdout = "not-a-number"

        assert self._fresh()() == 12

    def test_cpu_count_of_none_still_yields_a_usable_thread_count(self, mocker):
        mocker.patch("pipeline.config.sys.platform", "linux")
        mocker.patch("pipeline.config.os.cpu_count", return_value=None)

        assert self._fresh()() == 4

    def test_result_is_cached(self, mocker):
        """PipelineConfig is constructed constantly in tests and this shells
        out; the answer cannot change within a process."""
        mocker.patch("pipeline.config.sys.platform", "darwin")
        run = mocker.patch("pipeline.config.subprocess.run")
        run.return_value.stdout = "8"

        fn = self._fresh()
        fn()
        fn()
        run.assert_called_once()


class TestAnnotationConfigGuards:
    """Semaphore(0) hangs every fetch until the one-hour outer timeout.

    The CT client's guard names exactly this failure; the annotation clients
    build their semaphores the same way, and clinvar_rate_limit is also a
    divisor in _throttle.
    """

    @pytest.mark.parametrize(
        ("variable", "field_name"),
        [
            ("PIPELINE_CLINVAR_RATE_LIMIT", "clinvar_rate_limit"),
            ("PIPELINE_ORPHADATA_RATE_LIMIT", "orphadata_rate_limit"),
            ("PIPELINE_OPENTARGETS_RATE_LIMIT", "opentargets_rate_limit"),
        ],
    )
    def test_a_rate_limit_below_one_is_rejected(
        self, monkeypatch, variable: str, field_name: str
    ) -> None:
        monkeypatch.setenv(variable, "0")
        with pytest.raises(ValueError, match=f"{field_name} must be >= 1"):
            PipelineConfig()

    def test_a_zero_record_cap_is_rejected(self, monkeypatch) -> None:
        monkeypatch.setenv("PIPELINE_CLINVAR_MAX_RECORDS", "0")
        with pytest.raises(ValueError, match="clinvar_max_records must be >= 1"):
            PipelineConfig()

    def test_a_zero_disease_cap_is_rejected(self, monkeypatch) -> None:
        monkeypatch.setenv("PIPELINE_OPENTARGETS_MAX_DISEASES", "0")
        with pytest.raises(ValueError, match="opentargets_max_diseases must be >= 1"):
            PipelineConfig()


class TestExtractionConfigGuards:
    """Misconfigurations that used to fail an hour into a run, or never."""

    def test_the_default_reservation_admits_the_default_concurrency(self) -> None:
        # acquire() reserves estimated_tokens_per_call up front and only
        # corrects it when the call returns, ~50 s later. While the default
        # reservation was 40_000 against a 100_000 TPM limit, only two of the
        # five configured papers could be in flight at once: the pre-call
        # estimate, not max_concurrent_papers, was the throughput ceiling.
        config = PipelineConfig()
        assert (
            config.estimated_tokens_per_call * config.max_concurrent_papers
            <= config.tpm_limit
        )

    def test_an_estimate_above_the_tpm_limit_is_rejected(self, monkeypatch) -> None:
        # acquire() admits a request only when the estimate fits under the
        # limit, and with an empty window nothing ages out, so an estimate
        # above the limit made the first paper wait forever, silently.
        monkeypatch.setenv("PIPELINE_TPM_LIMIT", "30000")
        monkeypatch.setenv("PIPELINE_ESTIMATED_TOKENS_PER_CALL", "40000")
        with pytest.raises(ValueError, match="estimated_tokens_per_call"):
            PipelineConfig()

    @pytest.mark.parametrize("variable", ["PIPELINE_RPM_LIMIT", "PIPELINE_TPM_LIMIT"])
    def test_a_negative_limit_is_rejected(self, monkeypatch, variable: str) -> None:
        monkeypatch.setenv(variable, "-1")
        with pytest.raises(ValueError, match="must be >= 0"):
            PipelineConfig()

    def test_zero_concurrency_is_rejected(self, monkeypatch) -> None:
        monkeypatch.setenv("PIPELINE_MAX_CONCURRENT_PAPERS", "0")
        with pytest.raises(ValueError, match="max_concurrent_papers must be >= 1"):
            PipelineConfig()

    @pytest.mark.parametrize(
        ("variable", "field_name"),
        [
            ("PIPELINE_NCBI_RATE_LIMIT", "ncbi_rate_limit"),
            ("PIPELINE_UNIPROT_RATE_LIMIT", "uniprot_rate_limit"),
        ],
    )
    def test_a_lookup_rate_limit_below_one_is_rejected(
        self, monkeypatch, variable: str, field_name: str
    ) -> None:
        # Semaphore(0) makes the first gene validation wait forever with
        # no log line; the annotation clients had this guard, these did not.
        monkeypatch.setenv(variable, "0")
        with pytest.raises(ValueError, match=f"{field_name} must be >= 1"):
            PipelineConfig()

    @pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
    @pytest.mark.parametrize(
        "variable",
        [
            "PIPELINE_CONFIDENCE_THRESHOLD_UPDATE",
            "PIPELINE_CONFIDENCE_THRESHOLD_INSERT",
        ],
    )
    def test_a_non_finite_floor_is_rejected(self, monkeypatch, variable, value):
        # `confidence < nan` is always False, so a NaN update floor admits
        # every confidence-0 result while looking like a number.
        monkeypatch.setenv(variable, value)
        with pytest.raises(ValueError, match="finite"):
            PipelineConfig()

    def test_a_non_finite_floor_is_rejected_when_set_in_code_too(self):
        # The env parser refuses NaN first; a constructor argument bypasses
        # it and meets the same guard.
        with pytest.raises(ValueError, match="finite"):
            PipelineConfig(confidence_threshold_update=float("nan"))

    @pytest.mark.parametrize("value", ["-0.1", "1.5"])
    def test_a_floor_outside_the_unit_interval_is_rejected(self, monkeypatch, value):
        monkeypatch.setenv("PIPELINE_CONFIDENCE_THRESHOLD_UPDATE", value)
        with pytest.raises(ValueError, match=r"must be in \[0, 1\]"):
            PipelineConfig()

    def test_an_update_floor_above_the_insert_floor_is_rejected(self, monkeypatch):
        # The update floor is the permissive one by design; inverted, a
        # gene could be refused a reference while being accepted as new.
        monkeypatch.setenv("PIPELINE_CONFIDENCE_THRESHOLD_UPDATE", "0.7")
        monkeypatch.setenv("PIPELINE_CONFIDENCE_THRESHOLD_INSERT", "0.65")
        with pytest.raises(ValueError, match="confidence_threshold_update"):
            PipelineConfig()

    @pytest.mark.parametrize("value", ["-1", str(MODEL_MAX_OUTPUT_TOKENS + 1)])
    def test_a_token_ceiling_the_model_cannot_take_is_rejected(
        self, monkeypatch, value
    ):
        # A negative or over-model max_tokens is a 400 per paper, an hour
        # into the run, after every text fetch was paid for.
        monkeypatch.setenv("PIPELINE_LLM_MAX_TOKENS", value)
        with pytest.raises(ValueError, match="llm_max_tokens"):
            PipelineConfig()

    @pytest.mark.parametrize("value", ["0", "-5"])
    def test_an_empty_document_budget_is_rejected(self, monkeypatch, value):
        # max_paper_text_chars <= 0 sends an empty document block.
        monkeypatch.setenv("PIPELINE_MAX_PAPER_TEXT_CHARS", value)
        with pytest.raises(ValueError, match="max_paper_text_chars must be >= 1"):
            PipelineConfig()

    @pytest.mark.parametrize("value", ["High", "hi", "xhi", ""])
    def test_an_unknown_effort_is_rejected_at_startup(self, monkeypatch, value):
        # The API rejects it with a 400 per paper; every paper failed after
        # all the text fetches were paid for.
        monkeypatch.setenv("PIPELINE_LLM_EFFORT", value)
        with pytest.raises(ValueError, match="llm_effort"):
            PipelineConfig()

    @pytest.mark.parametrize("value", ["low", "medium", "high", "xhigh", "max"])
    def test_every_documented_effort_is_accepted(self, monkeypatch, value):
        monkeypatch.setenv("PIPELINE_LLM_EFFORT", value)
        assert PipelineConfig().llm_effort == value
