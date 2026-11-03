import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic.types import ErrorResponse, InvalidRequestError
from anthropic.types.messages import (
    MessageBatchErroredResult,
    MessageBatchExpiredResult,
)

from pipeline.api_telemetry import current_recorder, reset_recorder
from pipeline.batch_extraction import (
    build_batch_request,
    results_by_custom_id,
    submit_and_collect,
)
from pipeline.citations import current_tally, reset_tally
from pipeline.config import PipelineConfig
from pipeline.extraction_models import ExtractionFailedError
from pipeline.quality_metrics import TokenUsage

# A gene-bearing response, distinguishable from an empty one, so tests can
# assert on *which* content ends up under *which* key — not just that the
# right set of keys exists (a sort-then-zip bug would get the key set right
# while pairing each key with the wrong entry's content).
_NOTCH3_GENES = {
    "genes": [
        {
            "gene_symbol": "NOTCH3",
            "confidence": 0.9,
            "source_quote": "NOTCH3 variants were significantly associated (p=1e-9).",
        }
    ]
}
_EMPTY_GENES: dict = {"genes": []}


def _fake_batch_entry(
    custom_id: str,
    genes: dict | None,
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 0,
    output_tokens: int = 0,
    thinking: str | None = None,
    reported_thinking_tokens: int | None = None,
) -> Any:
    """Build a duck-typed stand-in for one MessageBatchIndividualResponse.

    `genes` is the strict tool call's input; None builds the one response
    shape that never called the tool. `thinking` adds the extended-thinking
    block a batch message carries like any other.
    """
    content: list[Any] = []
    if thinking is not None:
        content.append(type("K", (), {"type": "thinking", "thinking": thinking})())
    content.append(type("T", (), {"type": "text", "text": "Reporting the genes."})())
    if genes is not None:
        content.append(
            type(
                "B",
                (),
                {"type": "tool_use", "name": "report_genes", "input": genes},
            )()
        )
    return type(
        "Entry",
        (),
        {
            "custom_id": custom_id,
            "result": type(
                "R",
                (),
                {
                    "type": "succeeded",
                    "message": type(
                        "M",
                        (),
                        {
                            "content": content,
                            "stop_reason": stop_reason,
                            "usage": type(
                                "U",
                                (),
                                {
                                    "input_tokens": input_tokens,
                                    "output_tokens": output_tokens,
                                    "cache_creation_input_tokens": 0,
                                    "cache_read_input_tokens": 0,
                                    "output_tokens_details": (
                                        None
                                        if reported_thinking_tokens is None
                                        else SimpleNamespace(
                                            thinking_tokens=reported_thinking_tokens
                                        )
                                    ),
                                },
                            )(),
                        },
                    )(),
                },
            )(),
        },
    )()


def test_custom_id_is_the_pmid() -> None:
    request = build_batch_request("37063705", "paper text", PipelineConfig())
    assert request["custom_id"] == "37063705"


def test_batch_requests_carry_the_1h_cache_ttl() -> None:
    """The 5-minute default would expire mid-batch."""
    request = build_batch_request("37063705", "paper text", PipelineConfig())
    system = request["params"]["system"]
    assert all(b["cache_control"]["ttl"] == "1h" for b in system)


def test_batch_sends_the_shared_config_tool() -> None:
    config = PipelineConfig()
    request = build_batch_request("37063705", "paper text", config)
    assert request["params"]["tools"] == [config.extraction_tool]
    assert request["params"]["tool_choice"] == {"type": "auto"}


def test_batch_transmits_effort_at_every_level() -> None:
    """Claude Opus 5.5 defaults to "medium", so "high" must be stated.

    The batch path shares _build_message_params, so it would drop to the
    API default exactly as the streaming path would.
    """
    params = build_batch_request("1", "paper text", PipelineConfig())["params"]
    assert params["output_config"] == {"effort": "high"}

    params = build_batch_request(
        "1", "paper text", PipelineConfig(llm_effort="medium")
    )["params"]
    assert params["output_config"] == {"effort": "medium"}


def test_batch_sends_no_budget_tokens_to_an_adaptive_thinking_model() -> None:
    """budget_tokens returns HTTP 400 on every Claude 5 model."""
    config = PipelineConfig(llm_max_tokens=64_000)
    request = build_batch_request("1", "paper text", config)
    assert request["params"]["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
    }


def test_results_are_keyed_by_custom_id_not_position() -> None:
    """Batch results arrive in any order — B is listed before A, and each
    carries distinguishable content, so a sort-then-zip implementation
    (right key set, content paired with the wrong key) fails this test
    even though it would pass a keys-only assertion."""
    out = results_by_custom_id(
        [
            _fake_batch_entry("B", _EMPTY_GENES),
            _fake_batch_entry("A", _NOTCH3_GENES),
        ]
    )
    assert set(out) == {"A", "B"}
    assert out["B"] == []
    assert len(out["A"]) == 1
    assert out["A"][0].gene_symbol == "NOTCH3"
    assert out["A"][0].pmid == "A"


def _entry_with_result(custom_id: str, result: Any):
    """One MessageBatchIndividualResponse around a real SDK result object."""
    return type("Entry", (), {"custom_id": custom_id, "result": result})()


def test_unsuccessful_batch_result_is_skipped(caplog) -> None:
    """An expired result carries no error payload -- there is nothing the
    API could say beyond the type -- so the log line is the type alone."""
    entry = _entry_with_result("A", MessageBatchExpiredResult(type="expired"))

    assert results_by_custom_id([entry]) == {}
    assert "Batch entry A: expired" in caplog.text


def test_an_errored_entry_is_logged_with_the_api_s_own_reason(caplog) -> None:
    """A request-level rejection applies to every entry of the batch, so
    without its payload the log reads "errored" over every paper with no
    cause anywhere and the operator's only move is to re-submit and pay
    for the same rejection again.

    The real SDK shapes are used rather than stand-ins: the payload is two
    levels deep (`result.error.error`), and that nesting is the thing
    worth pinning.
    """
    entry = _entry_with_result(
        "37063705",
        MessageBatchErroredResult(
            type="errored",
            error=ErrorResponse(
                type="error",
                error=InvalidRequestError(
                    type="invalid_request_error",
                    message="messages.0.content.0.document: text exceeds the maximum",
                ),
            ),
        ),
    )

    assert results_by_custom_id([entry]) == {}
    assert "Batch entry 37063705: errored" in caplog.text
    assert "invalid_request_error" in caplog.text
    assert "text exceeds the maximum" in caplog.text


def test_unparseable_batch_result_is_skipped(caplog) -> None:
    entry = _fake_batch_entry("A", {"genes": [{"gene_symbol": "X"}]})

    assert results_by_custom_id([entry]) == {}
    assert "Could not parse batch entry A" in caplog.text


def test_a_batch_entry_that_never_called_the_tool_is_skipped(caplog) -> None:
    """tool_choice is "auto" on this path too, so prose alone is possible.

    There is no retry here -- the batch has already run -- so it is a
    dropped paper rather than a second attempt, and it says so.
    """
    assert results_by_custom_id([_fake_batch_entry("A", None)]) == {}
    assert "No report_genes tool call in batch entry A" in caplog.text


async def test_submit_and_collect_handles_scrambled_sdk_result_order(
    mocker, monkeypatch
) -> None:
    """submit_and_collect — not just results_by_custom_id in isolation — must
    survive the SDK itself returning batch results out of submission order.
    This is the actual boundary where arbitrary ordering is introduced, and
    it previously had zero direct coverage (every test_main.py test mocks
    submit_and_collect away entirely)."""

    class _FakeBatch:
        id = "batch_123"

    class _FakeStatus:
        processing_status = "ended"

    async def _fake_results_stream():
        # Submitted as {"A": ..., "B": ...} below; returned B-before-A.
        yield _fake_batch_entry("B", _EMPTY_GENES)
        yield _fake_batch_entry("A", _NOTCH3_GENES)

    mock_client = MagicMock()
    mock_client.messages.batches.create = AsyncMock(return_value=_FakeBatch())
    mock_client.messages.batches.retrieve = AsyncMock(return_value=_FakeStatus())
    mock_client.messages.batches.results = AsyncMock(
        return_value=_fake_results_stream()
    )
    mock_client.close = AsyncMock()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    mocker.patch(
        "pipeline.batch_extraction.build_async_client",
        return_value=mock_client,
    )

    out = await submit_and_collect(
        {"A": "paper text A", "B": "paper text B"}, PipelineConfig()
    )

    assert out["B"] == []
    assert len(out["A"]) == 1
    assert out["A"][0].gene_symbol == "NOTCH3"
    mock_client.close.assert_awaited_once()


async def test_submit_and_collect_polls_until_batch_ends(mocker, monkeypatch) -> None:
    batch = MagicMock(id="batch_123")
    in_progress = MagicMock(processing_status="in_progress")
    ended = MagicMock(processing_status="ended")

    async def empty_results():
        if False:
            yield None

    mock_client = MagicMock()
    mock_client.messages.batches.create = AsyncMock(return_value=batch)
    mock_client.messages.batches.retrieve = AsyncMock(
        side_effect=[in_progress, ended]
    )
    mock_client.messages.batches.results = AsyncMock(return_value=empty_results())
    mock_client.close = AsyncMock()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    mocker.patch(
        "pipeline.batch_extraction.build_async_client",
        return_value=mock_client,
    )
    sleep = mocker.patch("pipeline.batch_extraction.asyncio.sleep", AsyncMock())

    assert await submit_and_collect({"A": "paper"}, PipelineConfig()) == {}
    sleep.assert_awaited_once_with(30)


async def test_submit_and_collect_names_the_missing_api_key(monkeypatch) -> None:
    """The streaming path raises a friendly ExtractionFailedError here; the
    batch path used to fall through to the SDK's own error several frames
    from the thing the operator has to fix.

    Deliberately unpatched: the precheck now lives inside
    build_async_client, shared by both paths, so patching the builder here
    would test the mock rather than the guarantee.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ExtractionFailedError, match="ANTHROPIC_API_KEY is required"):
        await submit_and_collect({"A": "paper text"}, PipelineConfig())


async def test_submit_and_collect_gives_up_after_the_max_wait(
    mocker, monkeypatch
) -> None:
    """`while True` with no deadline waits forever on a batch that never
    ends, on a machine nobody is watching.

    The 24-hour production deadline is shortened to zero rather than
    simulated, so the first poll of a batch that has not ended trips it
    and the test runs in milliseconds. The batch is deliberately not
    cancelled — its id is in the message so its results stay retrievable.
    """

    class _FakeBatch:
        id = "batch_123"

    class _FakeStatus:
        processing_status = "in_progress"

    mock_client = MagicMock()
    mock_client.messages.batches.create = AsyncMock(return_value=_FakeBatch())
    mock_client.messages.batches.retrieve = AsyncMock(return_value=_FakeStatus())
    mock_client.close = AsyncMock()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    mocker.patch(
        "pipeline.batch_extraction.build_async_client",
        return_value=mock_client,
    )
    mocker.patch("pipeline.batch_extraction.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("pipeline.batch_extraction._MAX_WAIT_SECONDS", 0)

    with pytest.raises(ExtractionFailedError, match="batch_123 is still in_progress"):
        await submit_and_collect({"A": "paper text"}, PipelineConfig())

    # The client is still closed on the way out, as on every other path.
    mock_client.close.assert_awaited_once()


class TestBatchResultsAreAccountedLikeStreamedOnes:
    """The streaming path's guards live in _stream_and_parse; the batch path
    read the tool block straight off the message and so saw none of them:
    a refusal was logged as a prose-only answer, a truncated entry could
    never raise the token-ceiling warning, and no token or call was ever
    recorded for the run report.
    """

    def test_usage_is_accumulated_across_entries(self) -> None:
        usage = TokenUsage()
        results_by_custom_id(
            [
                _fake_batch_entry(
                    "1", {"genes": []}, input_tokens=100, output_tokens=10
                ),
                _fake_batch_entry(
                    "2", {"genes": []}, input_tokens=200, output_tokens=20
                ),
            ],
            usage=usage,
        )
        assert usage.input_tokens == 300
        assert usage.output_tokens == 30

    def test_a_refusal_is_reported_as_one_and_skipped(self, caplog) -> None:
        with caplog.at_level(logging.ERROR):
            out = results_by_custom_id(
                [_fake_batch_entry("1", None, stop_reason="refusal")],
                usage=TokenUsage(),
            )
        assert out == {}
        assert "refused" in caplog.text.lower()
        assert "No report_genes tool call" not in caplog.text
        # The fake carries no stop_details, which the API may also omit.
        assert "category: unspecified" in caplog.text

    def test_a_refusal_names_the_classifier_that_declined(self, caplog) -> None:
        entry = _fake_batch_entry("1", None, stop_reason="refusal")
        entry.result.message.stop_details = SimpleNamespace(category="bio")
        with caplog.at_level(logging.ERROR):
            results_by_custom_id([entry], usage=TokenUsage())
        assert "category: bio" in caplog.text

    def test_a_truncated_entry_counts_toward_the_ceiling_warning(self) -> None:
        usage = TokenUsage()
        out = results_by_custom_id(
            [_fake_batch_entry("1", {"genes": []}, stop_reason="max_tokens")],
            usage=usage,
        )
        assert out == {}
        assert usage.truncated_responses == 1

    def test_every_entry_is_recorded_as_an_anthropic_call(self) -> None:
        reset_recorder()
        results_by_custom_id(
            [
                _fake_batch_entry("1", {"genes": []}),
                _fake_batch_entry("2", {"genes": []}),
            ],
            usage=TokenUsage(),
        )
        row = next(r for r in current_recorder().records() if r.service == "anthropic")
        assert row.calls == 2
        assert row.ok == 2

    def test_every_report_genes_block_contributes(self) -> None:
        entry = _fake_batch_entry(
            "1",
            {
                "genes": [
                    {
                        "gene_symbol": "NOTCH3",
                        "confidence": 0.9,
                        "source_quote": "NOTCH3 was associated with WMH.",
                    }
                ]
            },
        )
        entry.result.message.content.append(
            type(
                "B",
                (),
                {
                    "type": "tool_use",
                    "name": "report_genes",
                    "input": {"genes": [{"gene_symbol": "HTRA1", "confidence": 0.8,
                                         "source_quote": "HTRA1 was associated."}]},
                },
            )()
        )
        out = results_by_custom_id([entry], usage=TokenUsage())
        assert [g.gene_symbol for g in out["1"]] == ["NOTCH3", "HTRA1"]


class TestBatchReasoningIsAccountedLikeStreamedReasoning:
    """The trace and its tokens are the streaming path's, and were the
    batch path's only omission left.

    `_stream_and_parse` charges the reasoning its share of the response's
    output tokens and writes it to the audit event log; the batch path
    called neither, so a `--batch` run published `thinkingTokens: 0`
    beside the batch's real output count -- an implied text-only split
    that the same message on the streaming path contradicts -- and
    events.db held no `paper_extraction_thinking` row for any paper the
    batch extracted.
    """

    # "Reporting the genes." is 20 chars, so 400 thinking chars are 400/420
    # of the response's characters: 2,000 output tokens * 0.952 = 1,904.
    _THINKING = "r" * 400

    def test_the_trace_is_charged_its_share_of_the_output_tokens(self) -> None:
        usage = TokenUsage()

        results_by_custom_id(
            [
                _fake_batch_entry(
                    "1", _EMPTY_GENES, output_tokens=2_000, thinking=self._THINKING
                )
            ],
            usage=usage,
        )

        assert usage.thinking_tokens == 1_904
        assert usage.text_output_tokens == 96

    def test_reported_thinking_tokens_replace_the_estimate(self) -> None:
        """The API's split wins on the batch path too, as on the stream."""
        usage = TokenUsage()

        results_by_custom_id(
            [
                _fake_batch_entry(
                    "1",
                    _EMPTY_GENES,
                    output_tokens=2_000,
                    thinking=self._THINKING,
                    reported_thinking_tokens=1_990,
                )
            ],
            usage=usage,
        )

        assert usage.thinking_tokens == 1_990
        assert usage.text_output_tokens == 10

    def test_the_trace_reaches_the_audit_event_log(self, mocker) -> None:
        event_log = mocker.patch("pipeline.anthropic_client.EventLog")

        results_by_custom_id(
            [
                _fake_batch_entry(
                    "37063705", _EMPTY_GENES, output_tokens=2_000,
                    thinking=self._THINKING,
                )
            ],
            usage=TokenUsage(),
            config=PipelineConfig(),
        )

        record = event_log.return_value.__enter__.return_value.record
        event_type, payload = record.call_args.args
        assert event_type == "paper_extraction_thinking"
        assert payload["pmid"] == "37063705"
        assert payload["thinking"] == self._THINKING
        # One call per entry: the batch has already run, so there is no
        # second attempt to tell this one from.
        assert (payload["attempt"], payload["accepted"]) == (1, True)

    def test_a_dropped_entry_s_trace_is_kept_and_marked_discarded(
        self, mocker
    ) -> None:
        """The reasoning behind a paper the batch answered in prose is the
        one worth reading, and it says it produced nothing."""
        event_log = mocker.patch("pipeline.anthropic_client.EventLog")

        out = results_by_custom_id(
            [_fake_batch_entry("1", None, thinking=self._THINKING)],
            usage=TokenUsage(),
        )

        assert out == {}
        _event_type, payload = (
            event_log.return_value.__enter__.return_value.record.call_args.args
        )
        assert payload["accepted"] is False

    def test_an_entry_with_no_thinking_block_records_nothing(self, mocker) -> None:
        event_log = mocker.patch("pipeline.anthropic_client.EventLog")

        results_by_custom_id([_fake_batch_entry("1", _EMPTY_GENES)], usage=TokenUsage())

        event_log.assert_not_called()

    def test_the_estimate_is_skipped_when_nobody_counts_usage(self, mocker) -> None:
        """A caller parsing results on its own still gets the audit event;
        there is simply no accumulator to charge."""
        event_log = mocker.patch("pipeline.anthropic_client.EventLog")

        results_by_custom_id(
            [_fake_batch_entry("1", _EMPTY_GENES, thinking=self._THINKING)]
        )

        event_log.assert_called_once()


def test_a_truncated_entry_is_skipped_even_when_nobody_counts_usage() -> None:
    out = results_by_custom_id(
        [_fake_batch_entry("1", {"genes": []}, stop_reason="max_tokens")]
    )
    assert out == {}


class TestBatchProvenance:
    """The batch path runs the same quote check as the streaming path.

    It parsed the tool blocks and returned, so under --batch the
    require_verified_quotes gate dropped nothing, no "quote not in paper"
    warning was logged, and the run report published 0/0 quotes checked
    over every gene the batch found.
    """

    _QUOTE = "NOTCH3 variants were significantly associated (p=1e-9)."

    def test_a_quote_absent_from_the_paper_is_dropped_when_required(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("PIPELINE_REQUIRE_VERIFIED_QUOTES", "true")
        reset_tally()
        out = results_by_custom_id(
            [_fake_batch_entry("1", _NOTCH3_GENES)],
            documents={"1": "A paper that says something else entirely."},
            config=PipelineConfig(),
        )
        assert out == {"1": []}
        tally = current_tally()
        assert tally.paper("1") == {
            "genes": 1,
            "verbatim": 0,
            "cited": 0,
            "dropped_unverified": 1,
        }
        assert [gene.gene_symbol for gene in tally.dropped("1")] == ["NOTCH3"]

    def test_the_tally_counts_a_verbatim_quote(self) -> None:
        reset_tally()
        out = results_by_custom_id(
            [_fake_batch_entry("1", _NOTCH3_GENES)],
            documents={"1": f"Background. {self._QUOTE} Methods."},
            config=PipelineConfig(),
        )
        assert [g.gene_symbol for g in out["1"]] == ["NOTCH3"]
        tally = current_tally()
        assert (tally.genes, tally.verbatim, tally.cited) == (1, 1, 0)

    def test_the_tally_attributes_the_quotes_to_their_paper(self) -> None:
        # The checkpoint stores each paper's share of the tally, so the batch
        # path has to record under the paper's scope as the streaming path
        # does, or a resumed batch run restores the genes without the quotes.
        reset_tally()
        results_by_custom_id(
            [_fake_batch_entry("1", _NOTCH3_GENES)],
            documents={"1": f"Background. {self._QUOTE} Methods."},
            config=PipelineConfig(),
        )
        assert current_tally().paper("1") == {
            "genes": 1,
            "verbatim": 1,
            "cited": 0,
            "dropped_unverified": 0,
        }

    def test_an_unverified_quote_is_kept_by_default(self) -> None:
        reset_tally()
        out = results_by_custom_id(
            [_fake_batch_entry("1", _NOTCH3_GENES)],
            documents={"1": "A paper that says something else entirely."},
            config=PipelineConfig(),
        )
        assert [g.gene_symbol for g in out["1"]] == ["NOTCH3"]
        assert current_tally().verbatim == 0

    async def test_submit_and_collect_checks_every_quote_against_its_paper(
        self, mocker, monkeypatch
    ) -> None:
        """The real entry point wires the document text through.

        `results_by_custom_id` only checks when handed the documents, so
        this is the guarantee that a `--batch` run gets the check at all.
        """
        reset_tally()

        class _FakeBatch:
            id = "batch_123"

        class _FakeStatus:
            processing_status = "ended"

        async def _fake_results_stream():
            yield _fake_batch_entry("A", _NOTCH3_GENES)

        mock_client = MagicMock()
        mock_client.messages.batches.create = AsyncMock(return_value=_FakeBatch())
        mock_client.messages.batches.retrieve = AsyncMock(return_value=_FakeStatus())
        mock_client.messages.batches.results = AsyncMock(
            return_value=_fake_results_stream()
        )
        mock_client.close = AsyncMock()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        mocker.patch(
            "pipeline.batch_extraction.build_async_client",
            return_value=mock_client,
        )

        await submit_and_collect({"A": f"Intro. {self._QUOTE}"}, PipelineConfig())

        tally = current_tally()
        assert (tally.genes, tally.verbatim) == (1, 1)
