"""Tests for pipeline.anthropic_client — streaming, thinking,
structured outputs, and retry logic."""

import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx2
import pytest
from tests.pipeline.conftest import MockToolUseBlock

from pipeline.anthropic_client import (
    _MAX_THINKING_CHARS_LOGGED,
    MODEL_PRICING,
    AnthropicClient,
    _build_message_params,
    _extract_response_text,
    _record_thinking_trace,
    build_async_client,
)
from pipeline.api_telemetry import current_recorder, reset_recorder
from pipeline.config import (
    EXTRACTION_MODEL,
    MODEL_MAX_OUTPUT_TOKENS,
    PROJECT_ROOT,
    PipelineConfig,
)
from pipeline.extraction_models import ExtractionFailedError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_keys(schema: Any, keys: set[str]) -> list[str]:
    """Recursively collect dotted paths where *schema* uses one of *keys*
    as an actual JSON-schema keyword (dict key) — not merely mentioned
    inside a description string, which is where transform_schema parks
    constraints the API's schema subset does not support.
    """
    hits: list[str] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in keys:
                    hits.append(f"{path}.{key}" if path else key)
                _walk(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, f"{path}[{i}]")

    _walk(schema, "")
    return hits


# ---------------------------------------------------------------------------
# output_config.format.schema — API-rejected keyword regression guard
# ---------------------------------------------------------------------------

# Anthropic's structured-output JSON-schema subset rejects these keywords
# outright (HTTP 400) if they appear as real schema keys. transform_schema
# (anthropic SDK) is expected to strip them into description text instead —
# see pipeline.extraction_models.GeneEntry.source_quote's min_length=1.
_API_REJECTED_KEYWORDS: frozenset[str] = frozenset(
    {"minLength", "maxLength", "minimum", "maximum"}
)


class TestToolSchemaIsApiSafe:
    """Pins the actual schema sent on the strict tool — not Pydantic's
    docs, not transform_schema's docstring, the real emitted schema.

    If whatever builds this schema ever regresses to passing minLength (or
    minimum/maximum/maxLength) straight through, every extraction call
    fails with a 400 and no offline test would otherwise catch it, because
    the schema is only rejected at the API boundary.

    The schema itself now lives on ``PipelineConfig.extraction_tool``
    (shared with the batch path — see ``pipeline.batch_extraction``), but
    these tests go through ``_build_message_params`` so they keep pinning
    the actual streaming wire payload, not the property in isolation.
    """

    @pytest.fixture
    def tool(self) -> dict[str, Any]:
        kwargs = _build_message_params("paper text", "12345678", PipelineConfig())
        return kwargs["tools"][0]

    def test_emitted_schema_carries_no_rejected_keyword(self, tool):
        hits = _find_keys(tool["input_schema"], set(_API_REJECTED_KEYWORDS))
        assert hits == [], (
            f"the tool's input_schema contains API-rejected keyword(s) "
            f"at: {hits}. These return HTTP 400 from the Anthropic API."
        )

    def test_source_quote_min_length_is_not_a_schema_keyword(self, tool):
        """min_length=1 must be enforced by Pydantic after parsing, never
        emitted as an API schema keyword.
        """
        props = tool["input_schema"]["$defs"]["GeneEntry"]["properties"]
        source_quote_schema = props["source_quote"]
        assert "minLength" not in source_quote_schema
        assert source_quote_schema["type"] == "string"
        # transform_schema parks the constraint as a hint in the
        # description instead of a real schema keyword — confirms the
        # constraint's intent survives, just not as an enforced keyword.
        assert "minLength" in source_quote_schema.get("description", "")

    def test_confidence_bounds_are_also_not_schema_keywords(self, tool):
        """Same pattern already existed for confidence's ge/le — this is
        not a new risk, min_length follows established precedent."""
        props = tool["input_schema"]["$defs"]["GeneEntry"]["properties"]
        confidence_schema = props["confidence"]
        assert "minimum" not in confidence_schema
        assert "maximum" not in confidence_schema

    def test_an_enum_is_not_a_rejected_keyword(self, tool):
        """gwas_trait's Literal survives as a real enum, unlike the
        numeric and length constraints above.

        That asymmetry is the whole reason the trait vocabulary can be
        enforced by the decoder while confidence bounds cannot: an enum is
        a keyword the API accepts, so it constrains generation rather than
        only validating afterwards.
        """
        props = tool["input_schema"]["$defs"]["GeneEntry"]["properties"]
        assert props["gwas_trait"]["items"]["enum"]

    def test_source_quote_is_required_in_emitted_schema(self, tool):
        """The API schema still enforces *presence* (required) — only the
        content constraint (non-blank) is deferred to Pydantic."""
        gene_entry_schema = tool["input_schema"]["$defs"]["GeneEntry"]
        assert "source_quote" in gene_entry_schema["required"]

    def test_the_tool_round_trips_through_json(self, tool):
        """The exact dict handed to the Anthropic API must round-trip
        through JSON — this is what actually gets sent over the wire."""
        assert json.loads(json.dumps(tool)) == tool


class TestRequestSharedWithBatchPath:
    """Task 15's pre-flight ruling hoisted the extraction schema onto
    PipelineConfig specifically so streaming and batch cannot drift apart.

    It stopped one line short, and the two paths then drifted on the very
    next field: streaming reserved tokens for the response text while the
    batch copy floored the budget at half of llm_max_tokens, so the same
    paper got a different reasoning allowance depending on which API it
    went through. thinking and output_config now come off config too, and
    these pin the agreement across both effort settings.
    """

    def test_the_stream_sends_the_shared_config_tool(self):
        config = PipelineConfig()
        kwargs = _build_message_params("paper text", "12345678", config)
        assert kwargs["tools"] == [config.extraction_tool]
        # Forcing the call would produce no text blocks, and citations
        # attach to text. Task 6 depends on this staying "auto".
        assert kwargs["tool_choice"] == {"type": "auto"}

    @pytest.mark.parametrize("effort", ["medium", "high"])
    def test_stream_and_batch_build_the_same_thinking_and_output_config(
        self, effort
    ):
        """Same PipelineConfig instance, both call sites, equal payloads."""
        from pipeline.batch_extraction import build_batch_request

        config = PipelineConfig(llm_effort=effort)
        stream = _build_message_params("paper text", "12345678", config)
        batch = build_batch_request("12345678", "paper text", config)["params"]

        assert stream == batch

    def test_neither_path_sends_a_token_budget(self):
        """The block the drift was about, pinned on both paths outright.

        The equality test above is satisfied by any two payloads that
        agree, including two that agree on a manual budget. Adaptive is
        the only mode Claude Opus 5.5 accepts -- budget_tokens is a 400 --
        so the value itself is asserted rather than just the agreement.
        """
        from pipeline.batch_extraction import build_batch_request

        config = PipelineConfig(llm_max_tokens=64_000)
        expected = {"type": "adaptive", "display": "summarized"}
        assert _build_message_params("paper text", "1", config)["thinking"] == expected
        assert (
            build_batch_request("1", "paper text", config)["params"]["thinking"]
            == expected
        )


def _make_mock_client(response):
    """Create a mock Anthropic client with properly mocked streaming.

    client.messages.stream(**kwargs) is a sync call that returns an
    async context manager. We use MagicMock for the sync parts and
    AsyncMock for the async parts.
    """
    stream_obj = AsyncMock()
    stream_obj.get_final_message = AsyncMock(return_value=response)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=stream_obj)
    cm.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.messages.stream.return_value = cm
    return mock_client


def test_extract_response_text_handles_multiple_thinking_and_unknown_blocks():
    response = MagicMock(
        content=[
            MagicMock(type="thinking", thinking="ab"),
            MagicMock(type="thinking", thinking="c"),
            MagicMock(type="tool_use"),
        ]
    )

    assert _extract_response_text(response) == ("", "abc")


def test_extract_response_text_folds_redacted_thinking_into_the_trace():
    """A redacted_thinking block carries no readable text -- the API
    encrypts it -- so it must not be silently counted as neither thinking
    nor text, which the original two-case match did.
    """
    response = MagicMock(
        content=[
            MagicMock(type="thinking", thinking="visible reasoning"),
            MagicMock(type="redacted_thinking"),
            MagicMock(type="text", text="answer"),
        ]
    )

    text, thinking = _extract_response_text(response)
    assert text == "answer"
    assert thinking == "visible reasoning[redacted_thinking]"


def test_extract_response_text_with_no_thinking_returns_an_empty_trace():
    response = MagicMock(content=[MagicMock(type="text", text="answer")])
    assert _extract_response_text(response) == ("answer", "")


def test_record_thinking_trace_caps_a_long_trace_and_flags_it(mocker) -> None:
    """Uncapped, a long paper's trace could run the SQLite event log past a
    reasonable size over a `--days-back 365` run's ~795 papers.
    """
    event_log = mocker.patch("pipeline.anthropic_client.EventLog")
    long_trace = "x" * (_MAX_THINKING_CHARS_LOGGED + 500)

    _record_thinking_trace(
        PipelineConfig(),
        pmid="12345678",
        thinking=long_trace,
        attempt=1,
        accepted=True,
    )

    record = event_log.return_value.__enter__.return_value.record
    _event_type, payload = record.call_args.args
    assert payload["thinking"] == "x" * _MAX_THINKING_CHARS_LOGGED
    assert payload["thinking_chars"] == _MAX_THINKING_CHARS_LOGGED + 500
    assert payload["truncated"] is True


def test_record_thinking_trace_skips_an_empty_trace(mocker) -> None:
    event_log = mocker.patch("pipeline.anthropic_client.EventLog")

    _record_thinking_trace(
        PipelineConfig(), pmid="12345678", thinking="", attempt=1, accepted=True
    )

    event_log.assert_not_called()


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def test_pricing_is_the_pinned_model_s_published_rate() -> None:
    """One model, one price, and no lookup that can miss.

    The old table keyed off config.llm_model verbatim and returned None on
    a miss, so a selectable model absent from it dropped the cost line out
    of the run report with only a log line to say so. There is nothing
    left to miss; what is worth pinning is the published rate itself.
    """
    assert MODEL_PRICING == (4.0, 20.0)


# ---------------------------------------------------------------------------
# _build_message_params — thinking / effort gating regression tests
# ---------------------------------------------------------------------------


def test_the_request_carries_no_budget_tokens() -> None:
    """budget_tokens returns HTTP 400 on every Claude 5 model."""
    config = PipelineConfig(llm_max_tokens=64_000)
    kwargs = _build_message_params("paper text", "12345678", config)
    assert kwargs["model"] == EXTRACTION_MODEL
    assert kwargs["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "budget_tokens" not in kwargs["thinking"]


def test_the_request_asks_for_the_model_s_full_output_budget() -> None:
    """llm_max_tokens auto-resolves to the pinned model's maximum."""
    kwargs = _build_message_params("paper text", "12345678", PipelineConfig())
    assert kwargs["max_tokens"] == MODEL_MAX_OUTPUT_TOKENS


def test_the_default_effort_travels_on_the_wire() -> None:
    """Our default effort is "high", and that is no longer the API's.

    Claude Opus 5.5 defaults to "medium", so the block that used to be
    dropped at "high" as wire noise now carries the setting the run
    report publishes.
    """
    config = PipelineConfig(llm_effort="high")
    kwargs = _build_message_params("paper", "1", config)
    assert kwargs["output_config"] == {"effort": "high"}


def test_a_non_default_effort_still_travels() -> None:
    config = PipelineConfig(llm_effort="medium")
    kwargs = _build_message_params("paper text", "12345678", config)
    assert kwargs["output_config"] == {"effort": "medium"}


# ---------------------------------------------------------------------------
# AnthropicClient.extract — Claude-specific behavior
# ---------------------------------------------------------------------------


class TestAnthropicClientExtract:
    async def test_successful_extraction(self, mocker, mock_anthropic_response):
        response = mock_anthropic_response(
            genes={
                "genes": [
                    {
                        "gene_symbol": "NOTCH3",
                        "confidence": 0.9,
                        "protein_name": "Notch 3",
                        "source_quote": (
                            "NOTCH3 variants were associated with WMH (p=1e-12)."
                        ),
                    }
                ]
            },
            input_tokens=500,
            output_tokens=200,
        )
        mock_client = _make_mock_client(response)

        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)

        from pipeline.config import PipelineConfig

        genes, usage = await client.extract(
            "This paper discusses NOTCH3...", "12345678", PipelineConfig(), None
        )
        assert len(genes) == 1
        assert genes[0].gene_symbol == "NOTCH3"
        assert genes[0].pmid == "12345678"
        assert usage.input_tokens == 500
        assert usage.output_tokens == 200

    async def test_empty_prose_is_not_a_failure_when_the_tool_was_called(
        self, mocker, mock_anthropic_response, caplog
    ):
        """The answer is the tool call, so silence around it is not a fault.

        Under tool_choice "auto" the model may say little or nothing
        before calling the tool. That used to be fatal, because the JSON
        *was* the text. It is worth a warning -- citations attach to text
        blocks, so no prose means no spans to verify a quote against --
        but the genes are valid.
        """
        response = mock_anthropic_response(text="   ")
        mock_client = _make_mock_client(response)

        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)

        from pipeline.config import PipelineConfig

        genes, _ = await client.extract(
            "Some paper text", "12345678", PipelineConfig(), None
        )
        assert genes == []
        assert "Empty text response" in caplog.text

    async def test_a_turn_that_never_calls_the_tool_is_retried(
        self, mocker, mock_anthropic_response
    ):
        """tool_choice is "auto", so answering in prose alone is possible.

        It is a malformed response rather than a refusal or an empty one,
        so it goes through the validation-retry budget -- which is what
        distinguishes it from the truncation and refusal paths, both of
        which fail the paper outright.
        """
        response = mock_anthropic_response(omit_tool_call=True)
        client = AnthropicClient()
        mocker.patch.object(
            client, "_get_client", return_value=_make_mock_client(response)
        )

        from pipeline.config import PipelineConfig

        with pytest.raises(
            ExtractionFailedError, match="No report_genes tool call"
        ):
            await client.extract(
                "Some paper text", "12345678", PipelineConfig(max_retries=0), None
            )

    async def test_refusal_is_reported_as_a_refusal_not_a_parse_failure(
        self, mocker, mock_anthropic_response
    ):
        """A refusal is a 200 with stop_reason='refusal', not an exception.

        Treating its content as an extraction would burn the validation-retry
        budget and then report a missing tool call,
        which is the wrong diagnosis and the wrong cost.
        """
        response = mock_anthropic_response(text="")
        response.stop_reason = "refusal"
        mock_client = _make_mock_client(response)

        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)

        from pipeline.config import PipelineConfig

        with pytest.raises(ExtractionFailedError, match="Refused by safety"):
            await client.extract("paper text", "12345678", PipelineConfig(), None)

    async def test_a_refusal_names_the_classifier_that_declined(
        self, mocker, mock_anthropic_response
    ):
        """Claude Opus 5.5 adds a `bio` classifier, and these are biology papers.

        Which classifier declined is what tells a false positive on a
        genetics paper from anything else, so it reaches the error.
        """
        response = mock_anthropic_response(text="")
        response.stop_reason = "refusal"
        response.stop_details = SimpleNamespace(category="bio")
        mock_client = _make_mock_client(response)

        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)

        from pipeline.config import PipelineConfig

        with pytest.raises(ExtractionFailedError, match=r"\(category: bio\)"):
            await client.extract("paper text", "12345678", PipelineConfig(), None)

    async def test_a_refusal_without_stop_details_still_reports(
        self, mocker, mock_anthropic_response
    ):
        """stop_details is informational and may be null."""
        response = mock_anthropic_response(text="")
        response.stop_reason = "refusal"
        response.stop_details = None
        mock_client = _make_mock_client(response)

        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)

        from pipeline.config import PipelineConfig

        with pytest.raises(ExtractionFailedError, match=r"category: unspecified"):
            await client.extract("paper text", "12345678", PipelineConfig(), None)

    async def test_unverified_quotes_are_kept_by_default(
        self, mocker, mock_anthropic_response, caplog
    ):
        """Default is measurement, not enforcement.

        A quote the API did not locate is a finding about the prompt. It
        is reported and kept, because silently dropping a gene would hide
        the very rate this measurement exists to establish.
        """
        response = mock_anthropic_response(
            genes={
                "genes": [
                    {
                        "gene_symbol": "NOTCH3",
                        "confidence": 0.9,
                        "source_quote": "A sentence the API never cited.",
                    }
                ]
            },
            cited=["Some entirely different sentence."],
        )
        caplog.set_level(logging.INFO)
        client = AnthropicClient()
        mocker.patch.object(
            client, "_get_client", return_value=_make_mock_client(response)
        )

        from pipeline.config import PipelineConfig

        genes, _ = await client.extract(
            "paper", "12345678", PipelineConfig(), None
        )
        assert len(genes) == 1
        assert "0/1 quotes verbatim in the paper" in caplog.text
        assert "0/1 matched an API citation span" in caplog.text

    async def test_a_quote_absent_from_the_paper_is_dropped_when_required(
        self, mocker, mock_anthropic_response, monkeypatch
    ):
        """The gate turns on the local check, not the citation match.

        Gating on citations would discard two thirds of the genes for want
        of prose -- measured 45/138 cited against 137/138 verbatim. What
        this drops is a quote that is not in the paper at all.
        """
        monkeypatch.setenv("PIPELINE_REQUIRE_VERIFIED_QUOTES", "true")
        response = mock_anthropic_response(
            genes={
                "genes": [
                    {
                        "gene_symbol": "NOTCH3",
                        "confidence": 0.9,
                        "source_quote": "A sentence the API never cited.",
                    }
                ]
            },
            cited=["Some entirely different sentence."],
        )
        client = AnthropicClient()
        mocker.patch.object(
            client, "_get_client", return_value=_make_mock_client(response)
        )

        from pipeline.config import PipelineConfig

        genes, _ = await client.extract("paper", "12345678", PipelineConfig(), None)
        assert genes == []

    async def test_an_uncited_but_verbatim_quote_survives_the_gate(
        self, mocker, mock_anthropic_response, monkeypatch
    ):
        """The case the citation ceiling makes ordinary.

        On a 47-gene paper the API cites a handful of sentences and the
        rest go unmatched while being perfectly verbatim. Gating on the
        citation would delete them; gating on the document does not.
        """
        monkeypatch.setenv("PIPELINE_REQUIRE_VERIFIED_QUOTES", "true")
        quote = "NOTCH3 variants were associated with WMH (p=1e-12)."
        response = mock_anthropic_response(
            genes={
                "genes": [
                    {
                        "gene_symbol": "NOTCH3",
                        "confidence": 0.9,
                        "source_quote": quote,
                    }
                ]
            },
            cited=["An entirely different sentence."],
        )
        client = AnthropicClient()
        mocker.patch.object(
            client, "_get_client", return_value=_make_mock_client(response)
        )

        from pipeline.config import PipelineConfig

        genes, _ = await client.extract(
            f"Intro. {quote} Outro.", "12345678", PipelineConfig(), None
        )
        assert [g.gene_symbol for g in genes] == ["NOTCH3"]

    async def test_a_verified_quote_survives_the_gate(
        self, mocker, mock_anthropic_response, monkeypatch, caplog
    ):
        """The span's trailing space must not cost a gene its provenance.

        The API's cited_text keeps the source's trailing whitespace and
        the model's quote does not, so an exact comparison would reject a
        quote that is in fact verbatim -- and under the gate, delete it.
        """
        monkeypatch.setenv("PIPELINE_REQUIRE_VERIFIED_QUOTES", "true")
        quote = "NOTCH3 variants were associated with WMH (p=1e-12)."
        response = mock_anthropic_response(
            genes={
                "genes": [
                    {
                        "gene_symbol": "NOTCH3",
                        "confidence": 0.9,
                        "source_quote": quote,
                    }
                ]
            },
            cited=[quote + " "],
        )
        caplog.set_level(logging.INFO)
        client = AnthropicClient()
        mocker.patch.object(
            client, "_get_client", return_value=_make_mock_client(response)
        )

        from pipeline.config import PipelineConfig

        genes, _ = await client.extract(quote, "12345678", PipelineConfig(), None)
        assert [g.gene_symbol for g in genes] == ["NOTCH3"]
        assert "1/1 quotes verbatim in the paper" in caplog.text
        assert "1/1 matched an API citation span" in caplog.text

    async def test_the_run_tally_counts_what_the_log_line_reports(
        self, mocker, mock_anthropic_response
    ):
        """The counts reach the run report, not only the log.

        They were computed here and discarded, so the dashboard could show
        a gene's quote without being able to say how many checked out.
        """
        from pipeline.citations import current_tally, reset_tally
        from pipeline.config import PipelineConfig

        reset_tally()
        quote = "NOTCH3 variants were associated with WMH (p=1e-12)."
        response = mock_anthropic_response(
            genes={
                "genes": [
                    {
                        "gene_symbol": "NOTCH3",
                        "confidence": 0.9,
                        "source_quote": quote,
                    },
                    {
                        "gene_symbol": "HTRA1",
                        "confidence": 0.9,
                        "source_quote": "A sentence that is not in the paper.",
                    },
                ]
            },
            cited=[quote + " "],
        )
        client = AnthropicClient()
        mocker.patch.object(
            client, "_get_client", return_value=_make_mock_client(response)
        )

        await client.extract(quote, "12345678", PipelineConfig(), None)

        tally = current_tally()
        assert tally.genes == 2
        # One quote is in the paper; the other is not. `cited` is the
        # stronger claim and only the first has an API span.
        assert tally.verbatim == 1
        assert tally.cited == 1

    async def test_the_tally_records_what_the_gate_dropped(
        self, mocker, mock_anthropic_response, monkeypatch
    ):
        from pipeline.citations import current_tally, reset_tally
        from pipeline.config import PipelineConfig

        monkeypatch.setenv("PIPELINE_REQUIRE_VERIFIED_QUOTES", "true")
        reset_tally()
        response = mock_anthropic_response(
            genes={
                "genes": [
                    {
                        "gene_symbol": "HTRA1",
                        "confidence": 0.9,
                        "source_quote": "A sentence that is not in the paper.",
                    }
                ]
            },
        )
        client = AnthropicClient()
        mocker.patch.object(
            client, "_get_client", return_value=_make_mock_client(response)
        )

        genes, _ = await client.extract(
            "Some other text.", "12345678", PipelineConfig(), None
        )

        assert genes == []
        assert [gene.gene_symbol for gene in current_tally().dropped("12345678")] == [
            "HTRA1"
        ]

    async def test_no_genes_reports_no_provenance_line(
        self, mocker, mock_anthropic_response, caplog
    ):
        """0/0 would be a line that says nothing on every empty paper."""
        caplog.set_level(logging.INFO)
        client = AnthropicClient()
        mocker.patch.object(
            client,
            "_get_client",
            return_value=_make_mock_client(mock_anthropic_response()),
        )

        from pipeline.config import PipelineConfig

        genes, _ = await client.extract("paper", "12345678", PipelineConfig(), None)
        assert genes == []
        assert "Provenance:" not in caplog.text

    async def test_thinking_blocks_skipped(self, mocker, mock_anthropic_response):
        response = mock_anthropic_response(include_thinking=True)
        mock_client = _make_mock_client(response)

        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)

        from pipeline.config import PipelineConfig

        genes, _ = await client.extract(
            "Paper text", "12345678", PipelineConfig(), None
        )
        assert genes == []

    async def test_thinking_is_retained_as_an_audit_event(
        self, mocker, mock_anthropic_response
    ):
        """The reasoning trace is billed, so it must not be dropped on the
        floor -- but it is an audit artifact, never published data, so it
        goes to the event log and nowhere else.
        """
        event_log = mocker.patch("pipeline.anthropic_client.EventLog")
        response = mock_anthropic_response(include_thinking=True)
        client = AnthropicClient()
        mocker.patch.object(
            client, "_get_client", return_value=_make_mock_client(response)
        )

        from pipeline.config import PipelineConfig

        await client.extract("Paper text", "12345678", PipelineConfig(), None)

        event_log.assert_called_once_with(PipelineConfig().event_db_path)
        record = event_log.return_value.__enter__.return_value.record
        record.assert_called_once()
        event_type, payload = record.call_args.args
        assert event_type == "paper_extraction_thinking"
        assert payload == {
            "pmid": "12345678",
            "thinking": "reasoning...",
            "thinking_chars": len("reasoning..."),
            "truncated": False,
            "attempt": 1,
            "accepted": True,
        }

    async def test_a_retried_paper_s_two_traces_say_which_was_accepted(
        self, mocker, mock_anthropic_response
    ):
        """A validation retry re-sends the paper and the model reasons
        again, so one PMID leaves two events. They used to be identical in
        shape -- same pmid, same three keys -- and an auditor reading the
        trace behind that paper's stored genes could not tell that the
        first belonged to a discarded attempt.
        """
        event_log = mocker.patch("pipeline.anthropic_client.EventLog")
        first = mock_anthropic_response(
            text="prose", omit_tool_call=True, include_thinking=True
        )
        second = mock_anthropic_response(text="prose", include_thinking=True)
        streams = []
        for response in (first, second):
            stream = AsyncMock()
            stream.get_final_message = AsyncMock(return_value=response)
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=stream)
            cm.__aexit__ = AsyncMock(return_value=False)
            streams.append(cm)
        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = streams
        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)

        await client.extract(
            "Paper text", "12345678", PipelineConfig(max_retries=1), None
        )

        record = event_log.return_value.__enter__.return_value.record
        payloads = [call.args[1] for call in record.call_args_list]
        assert [(p["attempt"], p["accepted"]) for p in payloads] == [
            (1, False),
            (2, True),
        ]
        assert {p["pmid"] for p in payloads} == {"12345678"}

    async def test_no_thinking_block_records_no_audit_event(
        self, mocker, mock_anthropic_response
    ):
        event_log = mocker.patch("pipeline.anthropic_client.EventLog")
        response = mock_anthropic_response(include_thinking=False)
        client = AnthropicClient()
        mocker.patch.object(
            client, "_get_client", return_value=_make_mock_client(response)
        )

        from pipeline.config import PipelineConfig

        await client.extract("Paper text", "12345678", PipelineConfig(), None)

        event_log.assert_not_called()

    async def test_zero_output_tokens_skips_thinking_estimate(
        self, mocker, mock_anthropic_response
    ):
        response = mock_anthropic_response(output_tokens=0, include_thinking=True)
        client = AnthropicClient()
        mocker.patch.object(
            client, "_get_client", return_value=_make_mock_client(response)
        )

        genes, usage = await client.extract(
            "Paper text", "12345678", PipelineConfig(), None
        )

        assert genes == []
        assert usage.thinking_tokens == 0

    async def test_truncated_response_fails_without_retry(
        self, mocker, mock_anthropic_response
    ):
        response = mock_anthropic_response(output_tokens=100)
        response.stop_reason = "max_tokens"
        mock_client = _make_mock_client(response)
        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)

        with pytest.raises(ExtractionFailedError, match="Response truncated") as exc:
            await client.extract("Paper text", "12345678", PipelineConfig(), None)

        assert exc.value.token_usage is not None
        assert exc.value.token_usage.truncated_responses == 1
        assert mock_client.messages.stream.call_count == 1

    async def test_rate_limit_retries_exhausted(self, mocker):
        response = MagicMock(headers={})
        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = anthropic.RateLimitError(
            message="limited", response=response, body=None
        )
        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)

        with pytest.raises(ExtractionFailedError, match="Rate limit retries exhausted"):
            await client.extract(
                "Paper text",
                "12345678",
                PipelineConfig(max_rate_limit_retries=0),
                None,
            )

    async def test_rate_limit_retry_without_shared_limiter(
        self, mocker, mock_anthropic_response
    ):
        response = MagicMock(headers={"retry-after": "0"})
        good = _make_mock_client(mock_anthropic_response())
        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = [
            anthropic.RateLimitError(
                message="limited", response=response, body=None
            ),
            good.messages.stream.return_value,
        ]
        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)
        sleep = mocker.patch("pipeline.anthropic_client.asyncio.sleep", AsyncMock())

        genes, _ = await client.extract(
            "Paper text",
            "12345678",
            PipelineConfig(max_rate_limit_retries=1),
            None,
        )

        assert genes == []
        sleep.assert_awaited_once_with(0.0)

    async def test_validation_retries_exhausted(
        self, mocker, mock_anthropic_response
    ):
        response = mock_anthropic_response(omit_tool_call=True)
        client = AnthropicClient()
        mocker.patch.object(
            client, "_get_client", return_value=_make_mock_client(response)
        )

        with pytest.raises(ExtractionFailedError, match="Validation retries exhausted"):
            await client.extract(
                "Paper text", "12345678", PipelineConfig(max_retries=0), None
            )

    async def test_unexpected_error_is_wrapped(self, mocker):
        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = RuntimeError("unexpected")
        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)

        with pytest.raises(
            ExtractionFailedError, match="Extraction failed.*unexpected"
        ):
            await client.extract("Paper text", "12345678", PipelineConfig(), None)

    async def test_api_error_returns_empty(self, mocker):
        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = anthropic.APIError(
            message="Internal server error",
            request=MagicMock(),
            body=None,
        )

        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)

        from pipeline.config import PipelineConfig

        with pytest.raises(ExtractionFailedError, match="Claude API error"):
            await client.extract("Paper text", "12345678", PipelineConfig(), None)

    async def test_rate_limiter_called(self, mocker, mock_anthropic_response, config):
        response = mock_anthropic_response()
        mock_client = _make_mock_client(response)

        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)

        rate_limiter = AsyncMock()
        rate_limiter.acquire = AsyncMock(return_value=0)
        rate_limiter.record_actual_usage = AsyncMock()

        await client.extract(
            "Paper text",
            "12345678",
            config,
            rate_limiter,
        )
        rate_limiter.acquire.assert_awaited_once()

    async def test_rate_limiter_zeroed_on_rate_limit_error(
        self, mocker, mock_anthropic_response
    ):
        """Bug 2: rate limiter reservation released on 429 error."""
        # First call raises RateLimitError, second succeeds
        good_response = mock_anthropic_response()

        good_stream = AsyncMock()
        good_stream.get_final_message = AsyncMock(return_value=good_response)
        good_cm = MagicMock()
        good_cm.__aenter__ = AsyncMock(return_value=good_stream)
        good_cm.__aexit__ = AsyncMock(return_value=False)

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.headers = {"retry-after": "0.01"}
        mock_client.messages.stream.side_effect = [
            anthropic.RateLimitError(
                message="rate limited",
                response=mock_response,
                body=None,
            ),
            good_cm,
        ]

        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)
        mocker.patch("asyncio.sleep", new_callable=AsyncMock)

        rate_limiter = MagicMock()
        # Return different request IDs for each acquire call
        rate_limiter.acquire = AsyncMock(side_effect=[0, 1])
        rate_limiter.record_actual_usage = AsyncMock()
        rate_limiter.signal_rate_limit = AsyncMock()

        from pipeline.config import PipelineConfig

        cfg = PipelineConfig(max_rate_limit_retries=3)
        await client.extract("Paper text", "12345678", cfg, rate_limiter)

        # First call should zero out reservation (request_id=0, actual=0)
        first_call = rate_limiter.record_actual_usage.call_args_list[0]
        assert first_call.args == (0, 0)

    async def test_rate_limiter_zeroed_on_connection_error(
        self, mocker, mock_anthropic_response
    ):
        """Bug 2: rate limiter reservation released on connection error."""
        good_response = mock_anthropic_response()

        good_stream = AsyncMock()
        good_stream.get_final_message = AsyncMock(return_value=good_response)
        good_cm = MagicMock()
        good_cm.__aenter__ = AsyncMock(return_value=good_stream)
        good_cm.__aexit__ = AsyncMock(return_value=False)

        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = [
            httpx2.RemoteProtocolError("connection lost"),
            good_cm,
        ]

        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)
        mocker.patch("asyncio.sleep", new_callable=AsyncMock)

        rate_limiter = AsyncMock()
        rate_limiter.acquire = AsyncMock(side_effect=[0, 1])
        rate_limiter.record_actual_usage = AsyncMock()

        from pipeline.config import PipelineConfig

        cfg = PipelineConfig(max_connection_retries=3)
        await client.extract("Paper text", "12345678", cfg, rate_limiter)

        # First call should zero out reservation (request_id=0, actual=0)
        first_call = rate_limiter.record_actual_usage.call_args_list[0]
        assert first_call.args == (0, 0)

    async def test_validation_retry_on_bad_confidence(
        self, mocker, mock_anthropic_response
    ):
        """Out-of-range confidence in first response triggers retry; 2nd passes."""
        bad_response = mock_anthropic_response(
            genes={
                "genes": [
                    {
                        "gene_symbol": "X",
                        "confidence": 1.5,
                        "source_quote": "X was associated with WMH (p=1e-8).",
                    }
                ]
            }
        )
        good_response = mock_anthropic_response()

        bad_stream = AsyncMock()
        bad_stream.get_final_message = AsyncMock(return_value=bad_response)
        bad_cm = MagicMock()
        bad_cm.__aenter__ = AsyncMock(return_value=bad_stream)
        bad_cm.__aexit__ = AsyncMock(return_value=False)

        good_stream = AsyncMock()
        good_stream.get_final_message = AsyncMock(return_value=good_response)
        good_cm = MagicMock()
        good_cm.__aenter__ = AsyncMock(return_value=good_stream)
        good_cm.__aexit__ = AsyncMock(return_value=False)

        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = [bad_cm, good_cm]

        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)

        from pipeline.config import PipelineConfig

        cfg = PipelineConfig(max_retries=2)
        genes, _ = await client.extract("Paper text", "12345678", cfg, None)
        assert genes == []  # Second call should succeed with empty genes
        assert mock_client.messages.stream.call_count == 2

    async def test_connection_error_retries_then_succeeds(
        self, mocker, mock_anthropic_response
    ):
        """Connection error on first call, success on second → 2 total calls."""
        response = mock_anthropic_response()

        good_stream = AsyncMock()
        good_stream.get_final_message = AsyncMock(return_value=response)
        good_cm = MagicMock()
        good_cm.__aenter__ = AsyncMock(return_value=good_stream)
        good_cm.__aexit__ = AsyncMock(return_value=False)

        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = [
            httpx2.RemoteProtocolError(
                "peer closed connection without sending complete message body"
            ),
            good_cm,
        ]

        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)
        mocker.patch("asyncio.sleep", new_callable=AsyncMock)

        from pipeline.config import PipelineConfig

        cfg = PipelineConfig(max_connection_retries=3)
        genes, _ = await client.extract("Paper text", "12345678", cfg, None)
        assert genes == []  # empty genes from good response
        assert mock_client.messages.stream.call_count == 2

    async def test_connection_error_retries_exhausted(self, mocker):
        """Persistent connection error exhausts retries → returns empty."""
        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = httpx2.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )

        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)
        mocker.patch("asyncio.sleep", new_callable=AsyncMock)

        from pipeline.config import PipelineConfig

        cfg = PipelineConfig(max_connection_retries=3)
        with pytest.raises(ExtractionFailedError, match="Connection retries exhausted"):
            await client.extract("Paper text", "12345678", cfg, None)
        assert mock_client.messages.stream.call_count == cfg.max_connection_retries + 1


# ---------------------------------------------------------------------------
# AnthropicClient lifecycle
# ---------------------------------------------------------------------------


class TestAnthropicClientLifecycle:
    def test_client_lazy_created(self, mocker, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        mock_cls = mocker.patch("pipeline.anthropic_client.anthropic.AsyncAnthropic")
        client = AnthropicClient()
        assert client._client is None
        client._get_client()
        mock_cls.assert_called_once()
        assert client._client is not None

    def test_get_client_reuses_existing_instance(self, mocker, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        constructor = mocker.patch("pipeline.anthropic_client.anthropic.AsyncAnthropic")
        client = AnthropicClient()

        assert client._get_client() is client._get_client()
        constructor.assert_called_once()

    def test_client_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        client = AnthropicClient()
        with pytest.raises(ExtractionFailedError, match="ANTHROPIC_API_KEY"):
            client._get_client()

    async def test_close_clears_client(self, mocker, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        mock_cls = mocker.patch("pipeline.anthropic_client.anthropic.AsyncAnthropic")
        mock_instance = AsyncMock()
        mock_cls.return_value = mock_instance

        client = AnthropicClient()
        client._get_client()
        await client.close()

        mock_instance.close.assert_awaited_once()
        assert client._client is None

    async def test_close_idempotent(self):
        client = AnthropicClient()
        # No client created — should not raise
        await client.close()
        await client.close()


class TestBuildAsyncClient:
    """The one place both the streaming and batch paths get a client."""

    def test_no_workspace_header_when_unset(self, mocker, monkeypatch):
        """A workspace-scoped key carries its own workspace; sending an
        empty header would be wrong, not merely redundant."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
        constructor = mocker.patch("pipeline.anthropic_client.anthropic.AsyncAnthropic")

        build_async_client()

        assert constructor.call_args.kwargs["default_headers"] is None

    def test_workspace_header_is_sent_when_set(self, mocker, monkeypatch):
        """An identity-linked key does not imply a workspace, so the API
        rejects every request with a 400 until one is named. The SDK reads
        ANTHROPIC_WORKSPACE_ID only on its Workload-Identity path, which
        ANTHROPIC_API_KEY short-circuits, so it must travel as a header."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_abc123")
        constructor = mocker.patch("pipeline.anthropic_client.anthropic.AsyncAnthropic")

        build_async_client()

        assert constructor.call_args.kwargs["default_headers"] == {
            "anthropic-workspace-id": "wrkspc_abc123"
        }

    def test_blank_workspace_is_treated_as_unset(self, mocker, monkeypatch):
        """A defaulted-but-empty variable must not put an empty header on
        the wire -- the same falsy coercion the SDK applies internally."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "   ")
        constructor = mocker.patch("pipeline.anthropic_client.anthropic.AsyncAnthropic")

        build_async_client()

        assert constructor.call_args.kwargs["default_headers"] is None

    def test_missing_api_key_names_itself(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with pytest.raises(ExtractionFailedError, match="ANTHROPIC_API_KEY"):
            build_async_client()

    def test_the_sdk_keeps_its_own_retries_and_the_inventory_says_so(
        self, monkeypatch
    ):
        """The SDK retries 408/409/429/5xx inside `send()`, before any
        branch of this module runs, so those attempts are counted neither
        as calls nor as retries: the Anthropic row undercounts HTTP
        attempts by up to `max_retries` per call, and a run that only
        succeeded because of them raises no `api_retried` warning.

        Left on the SDK default deliberately -- `_is_retryable_status_error`
        retries only 529 and the mid-stream 200, and the 24-hour
        `batches.retrieve` poll has no retry wrapper at all, so switching
        them off would buy an exact inventory with lost papers. This pins
        the choice against a silent change, and pins that the undercount
        is written down where the panel's reader would look for it.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        assert build_async_client().max_retries == anthropic.DEFAULT_MAX_RETRIES

        telemetry = (PROJECT_ROOT / "pipeline" / "CLAUDE.md").read_text(
            encoding="utf-8"
        )
        assert "undercounts" in telemetry and "DEFAULT_MAX_RETRIES" in telemetry


class TestTransportErrorsAndAccounting:
    """The SDK streams over httpx2, and a stream fails after the headers.

    `client.send()` wraps a connection failure into APIConnectionError,
    but with stream=True it returns as soon as the headers arrive; the
    body is read later by get_final_message(), where a disconnect or
    read timeout surfaces as a raw httpx2 exception. The pipeline pins
    `httpx` 0.28 for its own clients, and `httpx2.ReadError` is not a
    subclass of `httpx.ReadError`, so an except clause naming the httpx
    classes never matched the one failure a ten-minute stream is exposed
    to -- it fell through to the generic branch with zero retries.
    """

    @pytest.mark.parametrize(
        "error",
        [
            httpx2.RemoteProtocolError("peer closed connection"),
            httpx2.ReadError("connection reset"),
            httpx2.ReadTimeout("read timed out"),
        ],
    )
    async def test_mid_stream_transport_error_is_retried(
        self, error, mocker, mock_anthropic_response
    ):
        reset_recorder()
        broken_stream = AsyncMock()
        broken_stream.get_final_message = AsyncMock(side_effect=error)
        broken_cm = MagicMock()
        broken_cm.__aenter__ = AsyncMock(return_value=broken_stream)
        broken_cm.__aexit__ = AsyncMock(return_value=False)

        good_stream = AsyncMock()
        good_stream.get_final_message = AsyncMock(
            return_value=mock_anthropic_response()
        )
        good_cm = MagicMock()
        good_cm.__aenter__ = AsyncMock(return_value=good_stream)
        good_cm.__aexit__ = AsyncMock(return_value=False)

        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = [broken_cm, good_cm]
        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)
        mocker.patch("asyncio.sleep", new_callable=AsyncMock)

        genes, _ = await client.extract(
            "Paper text", "12345678", PipelineConfig(max_connection_retries=3), None
        )

        assert genes == []
        assert mock_client.messages.stream.call_count == 2
        # The retry reaches the run report: without it the External
        # services panel showed one clean Anthropic call.
        row = next(r for r in current_recorder().records() if r.service == "anthropic")
        assert row.retries == 1

    async def test_rate_limit_is_recorded_as_an_error_and_a_retry(
        self, mocker, mock_anthropic_response
    ):
        reset_recorder()
        good = _make_mock_client(mock_anthropic_response())
        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = [
            anthropic.RateLimitError(
                message="limited", response=MagicMock(headers={}), body=None
            ),
            good.messages.stream.return_value,
        ]
        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)
        mocker.patch("asyncio.sleep", new_callable=AsyncMock)

        await client.extract("Paper text", "12345678", PipelineConfig(), None)

        row = next(r for r in current_recorder().records() if r.service == "anthropic")
        assert row.calls == 2
        assert row.errors == 1
        assert row.retries == 1
        assert row.had_trouble

    async def test_connection_failures_are_recorded_as_errors(self, mocker):
        """Four failed attempts are four errors, not four clean calls.

        `_retry_connection` recorded each attempt with `status=None`, which
        the recorder reads as success, so the services panel showed
        `calls=4, ok=4, errors=0` beside `retries=3` for a paper that never
        reached the API.
        """
        reset_recorder()
        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = httpx2.ConnectError("refused")
        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)
        mocker.patch("asyncio.sleep", new_callable=AsyncMock)

        with pytest.raises(ExtractionFailedError, match="Connection retries"):
            await client.extract(
                "Paper text", "12345678", PipelineConfig(max_connection_retries=3), None
            )

        row = next(r for r in current_recorder().records() if r.service == "anthropic")
        assert (row.calls, row.ok, row.errors, row.retries) == (4, 0, 4, 3)

    @pytest.mark.parametrize("where", ["mid-stream", "on-open"])
    async def test_an_overload_is_retried_and_recorded_as_an_error(
        self, where, mocker, mock_anthropic_response
    ):
        """`overloaded_error` is the one failure a ten-minute stream is exposed to.

        Mid-stream it arrives as an SSE `error` event, which the SDK raises
        through `_make_status_error` with the stream's own 200 response: a
        bare APIStatusError with status_code 200. On open, after the SDK's
        own retries, it is an OverloadedError (529). Both used to fall into
        the fatal API-error branch, and the first was recorded as a clean
        200 call.
        """
        reset_recorder()
        response = httpx2.Response(
            200, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        body = {"type": "error", "error": {"type": "overloaded_error"}}
        good = _make_mock_client(mock_anthropic_response())
        mock_client = MagicMock()
        if where == "mid-stream":
            error = anthropic.APIStatusError("overloaded", response=response, body=body)
            broken_stream = AsyncMock()
            broken_stream.get_final_message = AsyncMock(side_effect=error)
            broken_cm = MagicMock()
            broken_cm.__aenter__ = AsyncMock(return_value=broken_stream)
            broken_cm.__aexit__ = AsyncMock(return_value=False)
            first: Any = broken_cm
        else:
            overloaded = httpx2.Response(529, request=response.request)
            first = anthropic.OverloadedError(
                "overloaded", response=overloaded, body=body
            )
        mock_client.messages.stream.side_effect = [
            first,
            good.messages.stream.return_value,
        ]
        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)
        mocker.patch("asyncio.sleep", new_callable=AsyncMock)

        genes, _ = await client.extract(
            "Paper text", "12345678", PipelineConfig(max_connection_retries=3), None
        )

        assert genes == []
        assert mock_client.messages.stream.call_count == 2
        row = next(r for r in current_recorder().records() if r.service == "anthropic")
        assert (row.calls, row.ok, row.errors, row.retries) == (2, 1, 1, 1)

    async def test_an_overload_is_not_retried_past_the_budget(self, mocker):
        response = httpx2.Response(
            529, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = anthropic.OverloadedError(
            "overloaded", response=response, body=None
        )
        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)
        mocker.patch("asyncio.sleep", new_callable=AsyncMock)

        with pytest.raises(ExtractionFailedError, match="Connection retries exhausted"):
            await client.extract(
                "Paper text", "12345678", PipelineConfig(max_connection_retries=1), None
            )
        assert mock_client.messages.stream.call_count == 2

    @pytest.mark.parametrize(
        "error",
        [
            anthropic.APIError(message="server error", request=MagicMock(), body=None),
            RuntimeError("unexpected"),
        ],
        ids=["api-error", "unexpected"],
    )
    async def test_the_reservation_is_released_on_every_failure(self, error, mocker):
        """A failed call must not hold its 20,000-token reservation.

        Only the rate-limit and connection branches released it. A 4xx/5xx
        or an unexpected exception left the estimate in the TPM window for
        the rest of the minute, so a burst of five failed papers blocked
        admission of every other paper with no log line.
        """
        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = error
        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)
        rate_limiter = AsyncMock()
        rate_limiter.acquire = AsyncMock(return_value=7)
        rate_limiter.record_actual_usage = AsyncMock()

        with pytest.raises(ExtractionFailedError):
            await client.extract("Paper", "12345678", PipelineConfig(), rate_limiter)

        rate_limiter.record_actual_usage.assert_awaited_once_with(7, 0)

    async def test_reported_thinking_tokens_replace_the_estimate(
        self, mocker, mock_anthropic_response
    ):
        """The API's own split wins; the character ratio is not added on top.

        The response is half thinking by characters, so the estimate would
        say 4,000 of 8,000. The API reports 7,500, and only that reaches
        the run report: the content blocks cannot measure the split.
        """
        response = mock_anthropic_response(
            text="y" * 12, include_thinking=True, output_tokens=8_000
        )
        response.usage.output_tokens_details = SimpleNamespace(thinking_tokens=7_500)
        client = AnthropicClient()
        mocker.patch.object(
            client, "_get_client", return_value=_make_mock_client(response)
        )

        _, usage = await client.extract(
            "Paper text", "12345678", PipelineConfig(), None
        )

        assert usage.thinking_tokens == 7_500
        assert usage.text_output_tokens == 500

    async def test_a_refused_response_still_counts_its_reported_thinking(
        self, mocker, mock_anthropic_response
    ):
        """A refusal after reasoning is billed, so its thinking is counted."""
        response = mock_anthropic_response(text="", output_tokens=900)
        response.stop_reason = "refusal"
        response.usage.output_tokens_details = SimpleNamespace(thinking_tokens=850)
        client = AnthropicClient()
        mocker.patch.object(
            client, "_get_client", return_value=_make_mock_client(response)
        )

        with pytest.raises(ExtractionFailedError) as raised:
            await client.extract("Paper text", "12345678", PipelineConfig(), None)

        assert raised.value.token_usage is not None
        assert raised.value.token_usage.thinking_tokens == 850

    async def test_thinking_estimate_is_per_response_across_retries(
        self, mocker, mock_anthropic_response
    ):
        # Attempt 1: 10,000 output tokens, no thinking block, fails
        # validation. Attempt 2: 8,000 tokens, half of the characters in
        # a thinking block. The estimate used the *cumulative* output
        # tokens with the *second* response's ratio: 18,000 * 0.5 = 9,000
        # thinking tokens attributed to a run that thought for ~4,000.
        bad = mock_anthropic_response(
            text="x" * 100,
            genes={"genes": [{"gene_symbol": "X", "confidence": 1.5,
                              "source_quote": "X was associated with WMH."}]},
            output_tokens=10_000,
        )
        good = mock_anthropic_response(
            text="y" * 12, include_thinking=True, output_tokens=8_000
        )
        # MockThinkingBlock carries "reasoning..." (12 chars) -> 50/50.
        bad_stream = AsyncMock()
        bad_stream.get_final_message = AsyncMock(return_value=bad)
        bad_cm = MagicMock()
        bad_cm.__aenter__ = AsyncMock(return_value=bad_stream)
        bad_cm.__aexit__ = AsyncMock(return_value=False)
        good_stream = AsyncMock()
        good_stream.get_final_message = AsyncMock(return_value=good)
        good_cm = MagicMock()
        good_cm.__aenter__ = AsyncMock(return_value=good_stream)
        good_cm.__aexit__ = AsyncMock(return_value=False)
        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = [bad_cm, good_cm]
        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)

        _, usage = await client.extract(
            "Paper text", "12345678", PipelineConfig(max_retries=2), None
        )

        assert usage.output_tokens == 18_000
        assert usage.thinking_tokens == 4_000

    async def test_every_report_genes_block_contributes(
        self, mocker, mock_anthropic_response
    ):
        # tool_choice is "auto" and parallel tool use is on by default, so
        # one turn may carry two report_genes calls. Only the first was
        # read; the second's genes vanished with no log line.
        response = mock_anthropic_response(
            genes={"genes": [{"gene_symbol": "NOTCH3", "confidence": 0.9,
                              "source_quote": "NOTCH3 was associated with WMH."}]}
        )
        response.content.append(
            MockToolUseBlock(
                input={"genes": [{"gene_symbol": "HTRA1", "confidence": 0.8,
                                  "source_quote": "HTRA1 was associated with WMH."}]}
            )
        )
        mock_client = _make_mock_client(response)
        client = AnthropicClient()
        mocker.patch.object(client, "_get_client", return_value=mock_client)

        genes, _ = await client.extract(
            "Paper text", "12345678", PipelineConfig(), None
        )

        assert [g.gene_symbol for g in genes] == ["NOTCH3", "HTRA1"]
