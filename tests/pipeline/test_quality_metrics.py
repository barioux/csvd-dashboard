"""Tests for pipeline.quality_metrics — TokenUsage, PipelineMetrics."""

from pipeline.quality_metrics import (
    PipelineMetrics,
    TokenUsage,
    accumulate_usage,
)

# ---------------------------------------------------------------------------
# TokenUsage
# ---------------------------------------------------------------------------


class TestTokenUsage:
    def test_defaults_zero(self):
        tu = TokenUsage()
        assert tu.input_tokens == 0
        assert tu.output_tokens == 0
        assert tu.cache_creation_input_tokens == 0
        assert tu.cache_read_input_tokens == 0

    def test_total_tokens(self):
        tu = TokenUsage(input_tokens=100, output_tokens=50)
        assert tu.total_tokens == 150

    def test_cache_hit_rate_zero_input(self):
        tu = TokenUsage()
        assert tu.cache_hit_rate == 0.0

    def test_cache_hit_rate_calculated(self):
        tu = TokenUsage(input_tokens=80, cache_read_input_tokens=20)
        assert tu.cache_hit_rate == pytest.approx(0.2)

    def test_cache_hit_rate_all_cached(self):
        tu = TokenUsage(input_tokens=0, cache_read_input_tokens=100)
        assert tu.cache_hit_rate == 1.0

    def test_iadd(self):
        a = TokenUsage(input_tokens=100, output_tokens=50)
        b = TokenUsage(
            input_tokens=200,
            output_tokens=100,
            cache_creation_input_tokens=10,
            cache_read_input_tokens=20,
        )
        a += b
        assert a.input_tokens == 300
        assert a.output_tokens == 150
        assert a.cache_creation_input_tokens == 10
        assert a.cache_read_input_tokens == 20

    def test_iadd_returns_self(self):
        a = TokenUsage(input_tokens=10)
        b = TokenUsage(input_tokens=5)
        result = a.__iadd__(b)
        assert result is a

    def test_iadd_chain(self):
        total = TokenUsage()
        for _i in range(5):
            total += TokenUsage(input_tokens=10, output_tokens=5)
        assert total.input_tokens == 50
        assert total.output_tokens == 25


# ---------------------------------------------------------------------------
# accumulate_usage
# ---------------------------------------------------------------------------


class TestAccumulateUsage:
    def test_accumulate_from_response(self, mock_anthropic_response):
        usage = TokenUsage()
        resp = mock_anthropic_response(input_tokens=500, output_tokens=200)
        accumulate_usage(usage, resp)
        assert usage.input_tokens == 500
        assert usage.output_tokens == 200

    def test_accumulate_no_usage_attr(self):
        usage = TokenUsage()

        class NoUsage:
            pass

        accumulate_usage(usage, NoUsage())
        assert usage.total_tokens == 0

    def test_accumulate_none_usage(self):
        usage = TokenUsage()

        class NullUsage:
            usage = None

        accumulate_usage(usage, NullUsage())
        assert usage.total_tokens == 0

    def test_accumulate_with_cache_tokens(self):
        usage = TokenUsage()

        class CachedResponse:
            class usage:
                input_tokens = 100
                output_tokens = 50
                cache_creation_input_tokens = 30
                cache_read_input_tokens = 20

        accumulate_usage(usage, CachedResponse())
        assert usage.cache_creation_input_tokens == 30
        assert usage.cache_read_input_tokens == 20

    def test_accumulate_takes_the_reported_thinking_tokens(self):
        """`output_tokens_details.thinking_tokens` is the exact split.

        It is the raw reasoning the model generated. Nothing in the
        response's content measures it: the trace is a summary, and the
        tool call's JSON is output that is neither thinking nor text.
        """
        usage = TokenUsage()

        class ThinkingResponse:
            class usage:
                input_tokens = 100
                output_tokens = 5_000
                cache_creation_input_tokens = 0
                cache_read_input_tokens = 0

                class output_tokens_details:
                    thinking_tokens = 4_200

        accumulate_usage(usage, ThinkingResponse())
        accumulate_usage(usage, ThinkingResponse())
        assert usage.thinking_tokens == 8_400
        assert usage.text_output_tokens == 1_600

    def test_accumulate_leaves_an_unreported_split_at_zero(
        self, mock_anthropic_response
    ):
        """No details is not a measurement; the caller estimates instead."""
        usage = TokenUsage()
        accumulate_usage(usage, mock_anthropic_response(output_tokens=500))
        assert usage.thinking_tokens == 0

    def test_accumulate_multiple(self, mock_anthropic_response):
        usage = TokenUsage()
        for _ in range(3):
            resp = mock_anthropic_response(input_tokens=100, output_tokens=50)
            accumulate_usage(usage, resp)
        assert usage.input_tokens == 300
        assert usage.output_tokens == 150


# ---------------------------------------------------------------------------
# PipelineMetrics
# ---------------------------------------------------------------------------


class TestPipelineMetrics:
    def test_defaults_zero(self):
        m = PipelineMetrics()
        assert m.papers_processed == 0
        assert m.genes_extracted == 0
        assert m.genes_validated == 0
        assert m.genes_rejected == 0

    def test_gene_acceptance_rate_zero(self, empty_metrics):
        assert empty_metrics.gene_acceptance_rate == 0.0

    def test_gene_acceptance_rate_calculated(self, populated_metrics):
        # 20/25 = 0.8
        assert populated_metrics.gene_acceptance_rate == pytest.approx(0.8)

    def test_fulltext_rate_zero(self, empty_metrics):
        assert empty_metrics.fulltext_rate == 0.0

    def test_fulltext_rate_calculated(self, populated_metrics):
        # 7/10 = 0.7
        assert populated_metrics.fulltext_rate == pytest.approx(0.7)

    def test_folding_one_paper_into_the_run_sums_every_counter(self):
        # Papers accumulate into their own metrics so a completed one can be
        # checkpointed with exactly what it contributed; this is how that
        # contribution reaches the run.
        run = PipelineMetrics(
            papers_processed=1,
            fulltext_retrieved=1,
            abstract_only=0,
            genes_extracted=4,
            genes_validated=3,
            genes_rejected=1,
            token_usage=TokenUsage(input_tokens=10, output_tokens=2),
        )
        paper = PipelineMetrics(
            papers_processed=1,
            fulltext_retrieved=0,
            abstract_only=1,
            genes_extracted=2,
            genes_validated=2,
            genes_rejected=0,
            token_usage=TokenUsage(input_tokens=5, output_tokens=1),
        )

        run += paper

        assert run.papers_processed == 2
        assert run.fulltext_retrieved == 1
        assert run.abstract_only == 1
        assert run.genes_extracted == 6
        assert run.genes_validated == 5
        assert run.genes_rejected == 1
        assert run.token_usage.input_tokens == 15
        assert run.token_usage.output_tokens == 3

    def test_folding_does_not_alias_the_token_usage(self):
        # += on TokenUsage mutates in place, so a shared instance would make
        # every later fold double-count through the other metrics object.
        run = PipelineMetrics()
        paper = PipelineMetrics(token_usage=TokenUsage(input_tokens=5))

        run += paper
        run += paper

        assert run.token_usage.input_tokens == 10
        assert paper.token_usage.input_tokens == 5


import pytest  # noqa: E402
