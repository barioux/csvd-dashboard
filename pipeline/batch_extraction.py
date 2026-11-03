"""Batch API submission for gene extraction.

Half the price of the streaming path, and this pipeline is offline and
manual so the latency costs nothing. Caching uses the 1-hour TTL: the
5-minute default would expire partway through a batch.
"""

import asyncio
import logging
import time
from typing import Any, cast

from anthropic.types.messages import batch_create_params

from pipeline.anthropic_client import (
    _account_reasoning,
    _build_message_params,
    _document_text,
    _extraction_tool_blocks,
    _merge_tool_blocks,
    _record_thinking_trace,
    _refusal_category,
    build_async_client,
)
from pipeline.api_telemetry import record_service_call
from pipeline.citations import collect_spans, paper_scope, report_provenance
from pipeline.config import EXTRACTION_TOOL_NAME, PipelineConfig
from pipeline.extraction_models import ExtractionFailedError, GeneEntry
from pipeline.quality_metrics import TokenUsage, accumulate_usage

logger = logging.getLogger(__name__)

_POLL_SECONDS = 30

# Anthropic's published target is that a batch completes within 24 hours,
# and the API expires a batch that has not after 24 hours (results stay
# retrievable for 29 days). Polling past that is waiting on something that
# will not happen, so the loop gives up there instead of running forever on
# an unattended machine.
_MAX_WAIT_SECONDS = 24 * 60 * 60


def _error_detail(result: Any) -> str:
    """The API's own account of why one entry failed, when it gave one.

    Only an ``errored`` result carries an ``error`` payload -- expired and
    canceled ones genuinely have none, hence the getattr -- and it is the
    whole diagnosis when a request-level rejection applies to every entry
    (an oversized document block, an invalid parameter after an SDK
    upgrade). Without it the run log said only "errored" over every paper
    in the batch, and the operator's only move was to re-submit and pay
    for the same rejection again.
    """
    error = getattr(getattr(result, "error", None), "error", None)
    if error is None:
        return ""
    return f" ({getattr(error, 'type', None)}: {getattr(error, 'message', None)})"


def _parse_batch_genes(message: Any, *, custom_id: str) -> list[GeneEntry] | None:
    """Parse one succeeded entry's tool calls, or None when there are none
    to parse.

    None is a dropped paper. There is no retry on this path -- the batch
    has already run -- so a missing tool call and an unparseable one are
    both logged and skipped rather than re-sent.
    """
    tool_blocks = _extraction_tool_blocks(message)
    if not tool_blocks:
        # tool_choice is "auto", so a turn can answer in prose without
        # calling the tool.
        logger.warning(
            "No %s tool call in batch entry %s", EXTRACTION_TOOL_NAME, custom_id
        )
        return None
    try:
        # Every block, not the first: parallel tool use can split the
        # genes across two calls in one turn.
        return _merge_tool_blocks(tool_blocks, pmid=custom_id)
    # One malformed row must not discard the rest of the batch.
    except Exception as exc:
        logger.warning("Could not parse batch entry %s: %s", custom_id, exc)
        return None


def build_batch_request(pmid: str, text: str, config: PipelineConfig) -> dict[str, Any]:
    """Build one batch request. custom_id is the PMID."""
    return {
        "custom_id": pmid,
        "params": _build_message_params(text, pmid, config),
    }


def results_by_custom_id(
    results: Any,
    *,
    usage: TokenUsage | None = None,
    documents: dict[str, str] | None = None,
    config: PipelineConfig | None = None,
) -> dict[str, list[GeneEntry]]:
    """Key results by custom_id — batch results arrive in any order.

    A custom_id absent from the result is a paper the batch did not
    answer: an entry that errored, expired, was refused, was cut short by
    the token ceiling, never called the tool, or could not be parsed. The
    caller marks those failed so they are retried on a later run.

    Every succeeded entry's usage accumulates into ``usage`` and each entry
    is recorded as one Anthropic call. The streaming path does both inside
    ``_stream_and_parse``, which this path never calls -- so a ``--batch``
    run published zero tokens, $0.00, no Anthropic row in the services
    panel, and could never raise the token-ceiling warning. The reasoning
    trace goes the same way: ``_account_reasoning`` charges it its share of
    the entry's output tokens and ``_record_thinking_trace`` writes it to
    the audit event log, both shared with the streaming path, because a
    ``--batch`` run otherwise reported ``thinkingTokens: 0`` and left no
    trace for an auditor to read.

    A non-succeeded entry is logged with the ``error`` payload the API
    attached to it, when it attached one: without it a request-level
    rejection that applies to every entry reads as an unexplained
    "errored" over every paper in the batch.

    ``documents`` maps each custom_id to the text its document block
    carried -- the truncated paper, so citation offsets line up -- and
    turns on the same provenance check the streaming path runs: every
    quote is looked for in the paper, the run tally counts it, and
    ``require_verified_quotes`` drops the ones that fail. Without it the
    batch path parsed the tool blocks and returned, so the gate did
    nothing under ``--batch`` and the report published 0/0 quotes checked.
    ``submit_and_collect`` always passes it; callers parsing results on
    their own get the parse only.
    """
    settings = config or PipelineConfig()
    out: dict[str, list[GeneEntry]] = {}
    for entry in results:
        if entry.result.type != "succeeded":
            # No HTTP status exists for one entry of a batch. A result the
            # API could not produce is a failed request all the same, and
            # is counted as one so the panel does not read as clean.
            record_service_call(
                "anthropic", endpoint="/v1/messages", method="POST", status=500
            )
            logger.warning(
                "Batch entry %s: %s%s",
                entry.custom_id,
                entry.result.type,
                _error_detail(entry.result),
            )
            continue
        message = entry.result.message
        record_service_call(
            "anthropic", endpoint="/v1/messages", method="POST", status=200
        )
        if usage is not None:
            accumulate_usage(usage, message)
        # The same two guards _stream_and_parse applies, in the same order
        # and before any content is read. The server-side `fallbacks`
        # parameter is unavailable on the Message Batches API, so this is
        # the only refusal handling the batch path has.
        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason == "refusal":
            logger.error(
                "Batch entry %s was refused by the safety classifier (category: %s)",
                entry.custom_id,
                _refusal_category(message),
            )
            continue
        if stop_reason == "max_tokens":
            logger.error(
                "Batch entry %s was cut short by the token ceiling "
                "(stop_reason=max_tokens); raise PIPELINE_LLM_MAX_TOKENS",
                entry.custom_id,
            )
            if usage is not None:
                usage.truncated_responses += 1
            continue
        # The reasoning trace and its share of the output tokens, as the
        # streaming path accounts for them: a `--batch` run wrote no
        # `paper_extraction_thinking` event at all and published
        # `thinkingTokens: 0` beside the batch's real output count. One
        # attempt per entry -- the batch has already run, so there is no
        # second one to distinguish.
        _, thinking_text = _account_reasoning(message, usage)
        genes = _parse_batch_genes(message, custom_id=entry.custom_id)
        _record_thinking_trace(
            settings,
            pmid=entry.custom_id,
            thinking=thinking_text,
            attempt=1,
            accepted=genes is not None,
        )
        if genes is None:
            continue
        if documents is not None and entry.custom_id in documents:
            # Under the paper's scope, as the streaming path records: the
            # checkpoint stores each paper's share of the tally.
            with paper_scope(entry.custom_id):
                genes = report_provenance(
                    genes,
                    spans=collect_spans(message),
                    document=documents[entry.custom_id],
                    pmid=entry.custom_id,
                    require_verified_quotes=settings.require_verified_quotes,
                )
        out[entry.custom_id] = genes
    return out


async def submit_and_collect(
    papers: dict[str, str],
    config: PipelineConfig,
    *,
    usage: TokenUsage | None = None,
) -> dict[str, list[GeneEntry]]:
    """Submit every paper as one batch and poll until it ends.

    Raises ExtractionFailedError without a key, and again if the batch is
    still running after _MAX_WAIT_SECONDS -- the batch itself is not
    cancelled, so its id is in the message and results stay retrievable.

    ``usage`` receives the whole batch's token usage; see
    ``results_by_custom_id``.
    """
    # Shared with the streaming path so the key precheck and the
    # identity-linked-key workspace header cannot drift between the two.
    client = build_async_client()
    try:
        requests = [build_batch_request(p, t, config) for p, t in papers.items()]
        # What each document block carried, for the provenance check: the
        # truncated text, so locate_quote's offsets mean what the API's do.
        documents = {
            request["custom_id"]: _document_text(request["params"])
            for request in requests
        }
        batch = await client.messages.batches.create(
            requests=cast(list[batch_create_params.Request], requests)
        )
        logger.info("Submitted batch %s (%d papers)", batch.id, len(papers))
        deadline = time.monotonic() + _MAX_WAIT_SECONDS
        while True:
            current = await client.messages.batches.retrieve(batch.id)
            if current.processing_status == "ended":
                break
            if time.monotonic() >= deadline:
                raise ExtractionFailedError(
                    f"Batch {batch.id} is still "
                    f"{current.processing_status} after "
                    f"{_MAX_WAIT_SECONDS // 3600}h. It was not cancelled; "
                    "retrieve its results directly if it finishes later."
                )
            logger.info("Batch %s: %s", batch.id, current.processing_status)
            await asyncio.sleep(_POLL_SECONDS)
        return results_by_custom_id(
            [r async for r in await client.messages.batches.results(batch.id)],
            usage=usage,
            documents=documents,
            config=config,
        )
    finally:
        await client.close()
