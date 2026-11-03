"""Tests for pipeline.report — cost estimation, report building, output."""

import json
from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from pipeline.anthropic_client import AnthropicClient
from pipeline.config import PipelineConfig
from pipeline.disease import load_disease
from pipeline.llm_extraction import GeneEntry
from pipeline.prompts import prompt_sha256
from pipeline.quality_metrics import PipelineMetrics, TokenUsage
from pipeline.report import (
    PipelineRunData,
    _confidence_style,
    _overview_lines,
    _paper_results_to_summaries,
    _print_database_panel,
    _print_papers_table,
    _print_validation_panel,
    build_local_pdf_run_data,
    build_pmid_run_data,
    build_run_data,
    print_rich_summary,
    write_comprehensive_report,
)


def _cost(input_tokens: int, output_tokens: int) -> float:
    """Compute cost via AnthropicClient.estimate_cost."""
    usage = TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
    return AnthropicClient().estimate_cost(usage)


# ---------------------------------------------------------------------------
# estimate_cost (via AnthropicClient)
# ---------------------------------------------------------------------------


class TestEstimateCost:
    def test_opus_5_5_pricing(self):
        cost = _cost(1_000_000, 100_000)
        # $4/M input + $20/M output -> 4 + 2 = 6
        assert cost == pytest.approx(6.0)

    def test_zero_tokens(self):
        assert _cost(0, 0) == 0.0

    def test_small_cost(self):
        cost = _cost(1000, 500)
        assert 0 < cost < 0.1


# ---------------------------------------------------------------------------
# Mock PaperResult for testing
# ---------------------------------------------------------------------------


@dataclass
class MockPaperResult:
    pmid: str
    genes: list[GeneEntry] = field(default_factory=list)
    rejected_genes: list = field(default_factory=list)
    fulltext: bool = False
    source: str = "abstract"
    error: str | None = None
    pdf_parse_time: float = 0.0
    llm_time: float = 0.0
    validation_time: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# AnthropicClient.report_metadata
# ---------------------------------------------------------------------------


class TestAnthropicClientReportMetadata:
    def test_anthropic_uses_llm_model(self):
        config = PipelineConfig()
        fields = AnthropicClient().report_metadata(config)
        assert fields["model"] == "claude-opus-5-5"
        assert fields["thinking_mode"] != "none"
        assert fields["effort"] is not None

    def test_report_metadata_carries_disease_and_prompt_hash(self):
        config = PipelineConfig()
        fields = AnthropicClient().report_metadata(config)
        assert fields["disease"] == load_disease().key
        assert fields["prompt_sha256"] == prompt_sha256(config.prompt_version)


# ---------------------------------------------------------------------------
# Anthropic report metadata
# ---------------------------------------------------------------------------


class TestAnthropicReportMetadata:
    """Verify reports produced for Anthropic runs show correct model + cost."""

    def test_anthropic_run_still_has_cost(self):
        config = PipelineConfig()
        data = build_run_data(
            metrics=PipelineMetrics(
                papers_processed=1,
                fulltext_retrieved=1,
                abstract_only=0,
                genes_extracted=2,
                genes_validated=2,
                genes_rejected=0,
                token_usage=TokenUsage(input_tokens=1000, output_tokens=500),
            ),
            results=[],
            gene_result=None,
            batch_warnings=[],
            config=config,
            days_back=7,
            dry_run=False,
            total_pmids_found=1,
            new_pmids_count=1,
        )
        assert data["pipeline_config"]["model"] == "claude-opus-5-5"
        assert data["pipeline_config"]["llm_provider"] == "anthropic"
        assert data["token_usage"]["estimated_cost_usd"] is not None


# ---------------------------------------------------------------------------
# _paper_results_to_summaries
# ---------------------------------------------------------------------------


class TestPaperResultsToSummaries:
    def test_empty(self):
        assert _paper_results_to_summaries([]) == []

    def test_single_result(self):
        result = MockPaperResult(pmid="111", fulltext=True, source="pmc")
        summaries = _paper_results_to_summaries([result])
        assert len(summaries) == 1
        assert summaries[0]["pmid"] == "111"
        assert summaries[0]["fulltext"] is True
        assert summaries[0]["gene_count"] == 0

    def test_result_with_genes(self):
        gene = GeneEntry(
            gene_symbol="NOTCH3",
            confidence=0.9,
            source_quote="NOTCH3 variants were associated with WMH (p=1e-12).",
        )
        result = MockPaperResult(pmid="222", genes=[gene])
        summaries = _paper_results_to_summaries([result])
        assert summaries[0]["gene_count"] == 1
        assert len(summaries[0]["genes"]) == 1

    def test_result_with_error(self):
        result = MockPaperResult(pmid="333", error="Timeout")
        summaries = _paper_results_to_summaries([result])
        assert summaries[0]["error"] == "Timeout"
        assert summaries[0]["gene_count"] == 0

    def test_positive_step_timings_are_included(self):
        result = MockPaperResult(pmid="444")
        result.pdf_parse_time = 1.2
        result.llm_time = 2.3
        result.validation_time = 0.4

        summary = _paper_results_to_summaries([result])[0]

        # The three timing keys are NotRequired -- _paper_results_to_summaries
        # sets each one only when the step reported a positive duration. Assert
        # presence before the values, so this test checks the inclusion its name
        # claims rather than reading through a key that may not be there.
        assert "pdf_parse_time" in summary
        assert "llm_time" in summary
        assert "validation_time" in summary

        assert summary["pdf_parse_time"] == 1.2
        assert summary["llm_time"] == 2.3
        assert summary["validation_time"] == 0.4


# ---------------------------------------------------------------------------
# build_run_data
# ---------------------------------------------------------------------------


class TestBuildRunData:
    def _build(self, **kwargs):
        defaults: dict[str, Any] = {
            "metrics": PipelineMetrics(
                papers_processed=5,
                fulltext_retrieved=3,
                abstract_only=2,
                genes_extracted=10,
                genes_validated=8,
                genes_rejected=2,
                token_usage=TokenUsage(input_tokens=5000, output_tokens=2000),
            ),
            "results": [],
            "gene_result": {"inserted": 3, "updated": 2},
            "batch_warnings": [],
            "config": PipelineConfig(),
            "days_back": 7,
            "dry_run": False,
            "total_pmids_found": 20,
            "new_pmids_count": 10,
        }
        defaults.update(kwargs)
        return build_run_data(**defaults)

    def test_structure(self):
        data = self._build()
        assert "timestamp" in data
        assert "pipeline_config" in data
        assert "search" in data
        assert "papers" in data
        assert "genes" in data
        assert "token_usage" in data
        assert "database" in data

    def test_search_counts(self):
        data = self._build()
        search = data.get("search")
        assert search is not None
        assert search["pmids_found"] == 20
        assert search["pmids_new"] == 10
        assert search["pmids_skipped"] == 10

    def test_dry_run_flag(self):
        data = self._build(dry_run=True)
        assert data["pipeline_config"]["dry_run"] is True

    def test_cost_included(self):
        data = self._build()
        assert "estimated_cost_usd" in data["token_usage"]

    def test_batch_warnings_included(self):
        data = self._build(batch_warnings=["warn1", "warn2"])
        assert data["batch_validation_warnings"] == ["warn1", "warn2"]

    def test_json_serializable(self):
        data = self._build()
        serialized = json.dumps(data, default=str)
        assert isinstance(serialized, str)

    def test_with_results(self):
        result = MockPaperResult(pmid="111", fulltext=True, source="pmc")
        data = self._build(results=[result])
        assert len(data["papers_detail"]) == 1

    def test_database_none_for_dry_run(self):
        data = self._build(gene_result=None, dry_run=True)
        assert data.get("database") is None

    def test_failed_count(self):
        results = [
            MockPaperResult(pmid="1"),
            MockPaperResult(pmid="2", error="Failed"),
        ]
        data = self._build(results=results)
        assert data["papers"]["failed"] == 1


# ---------------------------------------------------------------------------
# write_comprehensive_report
# ---------------------------------------------------------------------------


class TestWriteComprehensiveReport:
    def test_writes_json_file(self, tmp_path):
        data = cast(PipelineRunData, {"test": "data", "timestamp": "2024-01-01"})
        path = write_comprehensive_report(data, tmp_path)
        assert path.exists()
        assert path.suffix == ".json"
        content = json.loads(path.read_text())
        assert content["test"] == "data"

    def test_creates_directory(self, tmp_path):
        log_dir = tmp_path / "nested" / "logs"
        data = cast(PipelineRunData, {"test": True})
        path = write_comprehensive_report(data, log_dir)
        assert path.exists()

    def test_filename_format(self, tmp_path):
        data = cast(PipelineRunData, {})
        path = write_comprehensive_report(data, tmp_path)
        assert path.name.startswith("pipeline_report_")
        assert path.name.endswith(".json")


# ---------------------------------------------------------------------------
# print_rich_summary (smoke test — just ensure no errors)
# ---------------------------------------------------------------------------


class TestPrintRichSummary:
    def test_smoke_empty(self):
        data = cast(
            PipelineRunData,
            {
                "pipeline_config": {},
                "search": {},
                "papers": {},
                "genes": {},
                "token_usage": {},
                "papers_detail": [],
            },
        )
        # Should not raise
        print_rich_summary(data)

    def test_smoke_with_data(self):
        data = cast(
            PipelineRunData,
            {
                "pipeline_config": {
                    "model": "claude-opus-4-7",
                    "dry_run": False,
                    "days_back": 7,
                },
                "search": {"pmids_found": 10, "pmids_new": 5, "pmids_skipped": 5},
                "papers": {
                    "processed": 5,
                    "fulltext": 3,
                    "abstract_only": 2,
                    "fulltext_rate": 0.6,
                    "failed": 0,
                },
                "genes": {
                    "extracted": 10,
                    "validated": 8,
                    "rejected": 2,
                    "acceptance_rate": 0.8,
                },
                "token_usage": {
                    "total_tokens": 7000,
                    "input_tokens": 5000,
                    "output_tokens": 2000,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_hit_rate": 0,
                    "estimated_cost_usd": 0.1125,
                },
                "database": {"inserted": 3, "updated": 2},
                "batch_validation_warnings": ["test warning"],
                "papers_detail": [
                    {
                        "pmid": "111",
                        "source": "pmc",
                        "gene_count": 3,
                        "error": None,
                        "genes": [
                            {
                                "gene_symbol": "NOTCH3",
                                "protein_name": "Notch 3",
                                "pmid": "111",
                                "confidence": 0.9,
                                "gwas_trait": ["WMH"],
                                "mendelian_randomization": False,
                                "omics_evidence": [],
                            }
                        ],
                    }
                ],
            },
        )
        print_rich_summary(data)

    def test_smoke_dry_run(self):
        data = cast(
            PipelineRunData,
            {
                "pipeline_config": {"dry_run": True, "model": "test"},
                "search": {},
                "papers": {},
                "genes": {
                    "extracted": 0,
                    "validated": 0,
                    "rejected": 0,
                    "acceptance_rate": 0,
                },
                "token_usage": {"total_tokens": 0},
                "database": None,
                "batch_validation_warnings": [],
                "papers_detail": [],
            },
        )
        print_rich_summary(data)


class TestRichSummaryBranches:
    def test_local_pdf_overview_includes_failure(self):
        data = cast(
            PipelineRunData,
            {
                "pipeline_config": {
                    "mode": "local_pdf",
                    "pdf_directory": "/tmp/pdfs",
                    "skip_validation": True,
                },
                "papers": {"processed": 1, "total": 2, "failed": 1},
            },
        )

        lines = _overview_lines(data)

        assert any("Directory" in line for line in lines)
        assert any("Papers failed" in line for line in lines)

    def test_pmid_overview(self):
        data = cast(
            PipelineRunData,
            {
                "pipeline_config": {"mode": "pmid_list", "pmid_file": "ids.txt"},
                "papers": {"processed": 1, "total": 1},
            },
        )

        assert any("ids.txt" in line for line in _overview_lines(data))

    def test_paper_table_error_and_zero_gene_statuses(self):
        console = MagicMock()
        papers = cast(
            list,
            [
                {
                    "pmid": "1",
                    "source": "abstract",
                    "gene_count": 0,
                    "error": "timeout",
                },
                {
                    "pmid": "2",
                    "source": "abstract",
                    "gene_count": 0,
                    "error": None,
                },
            ],
        )

        _print_papers_table(console, papers, None)

        console.print.assert_called_once()

    @pytest.mark.parametrize(
        ("confidence", "expected"),
        [(0.95, "green"), (0.8, "yellow"), (0.2, "red")],
    )
    def test_confidence_style(self, confidence, expected):
        assert _confidence_style(confidence) == expected

    def test_validation_panel_warns_about_truncation(self):
        console = MagicMock()
        data = cast(
            PipelineRunData,
            {
                "genes": {},
                "papers": {},
                "token_usage": {"truncated_responses": 2},
            },
        )

        _print_validation_panel(console, data)

        assert "Truncated responses" in str(console.print.call_args.args[0].renderable)

    def test_database_panel_is_hidden_for_local_modes(self):
        console = MagicMock()
        data = cast(PipelineRunData, {"pipeline_config": {"mode": "local_pdf"}})

        _print_database_panel(console, data)

        console.print.assert_not_called()


# ---------------------------------------------------------------------------
# build_local_pdf_run_data
# ---------------------------------------------------------------------------


class TestBuildLocalPdfRunData:
    def _build(self, **kwargs):
        from pathlib import Path

        defaults: dict[str, Any] = {
            "metrics": PipelineMetrics(
                papers_processed=3,
                fulltext_retrieved=3,
                abstract_only=0,
                genes_extracted=5,
                genes_validated=4,
                genes_rejected=1,
                token_usage=TokenUsage(input_tokens=3000, output_tokens=1000),
            ),
            "results": [],
            "batch_warnings": [],
            "config": PipelineConfig(),
            "pdf_dir": Path("/tmp/test_pdfs"),
            "skip_validation": False,
        }
        defaults.update(kwargs)
        return build_local_pdf_run_data(**defaults)

    def test_structure_no_search_no_database(self):
        data = self._build()
        assert "timestamp" in data
        assert "pipeline_config" in data
        assert "papers" in data
        assert "genes" in data
        assert "token_usage" in data
        assert "search" not in data
        assert "database" not in data

    def test_mode_is_local_pdf(self):
        data = self._build()
        assert data["pipeline_config"]["mode"] == "local_pdf"

    def test_llm_provider_included(self):
        data = self._build()
        assert data["pipeline_config"]["llm_provider"] == "anthropic"

    def test_pdf_directory_included(self):
        from pathlib import Path

        data = self._build(pdf_dir=Path("/data/pdfs"))
        assert data["pipeline_config"]["pdf_directory"] == "/data/pdfs"

    def test_skip_validation_flag(self):
        data = self._build(skip_validation=True)
        assert data["pipeline_config"]["skip_validation"] is True

    def test_cost_included(self):
        data = self._build()
        assert "estimated_cost_usd" in data["token_usage"]

    def test_json_serializable(self):
        data = self._build()
        serialized = json.dumps(data, default=str)
        assert isinstance(serialized, str)


# ---------------------------------------------------------------------------
# build_pmid_run_data
# ---------------------------------------------------------------------------


class TestBuildPmidRunData:
    def _build(self, **kwargs):
        from pathlib import Path

        defaults: dict[str, Any] = {
            "metrics": PipelineMetrics(
                papers_processed=2,
                fulltext_retrieved=1,
                abstract_only=1,
                genes_extracted=3,
                genes_validated=2,
                genes_rejected=1,
                token_usage=TokenUsage(input_tokens=2000, output_tokens=800),
            ),
            "results": [],
            "batch_warnings": [],
            "config": PipelineConfig(),
            "pmid_file": Path("/tmp/pmids.txt"),
            "skip_validation": False,
        }
        defaults.update(kwargs)
        return build_pmid_run_data(**defaults)

    def test_structure(self):
        data = self._build()
        assert "timestamp" in data
        assert "pipeline_config" in data
        assert "papers" in data
        assert "genes" in data
        assert "token_usage" in data
        assert "search" not in data
        assert "database" not in data

    def test_mode_is_pmid_list(self):
        data = self._build()
        assert data["pipeline_config"]["mode"] == "pmid_list"

    def test_llm_provider_included(self):
        data = self._build()
        assert data["pipeline_config"]["llm_provider"] == "anthropic"

    def test_pmid_file_included(self):
        from pathlib import Path

        data = self._build(pmid_file=Path("/data/my_pmids.txt"))
        assert data["pipeline_config"]["pmid_file"] == "/data/my_pmids.txt"

    def test_json_serializable(self):
        data = self._build()
        serialized = json.dumps(data, default=str)
        assert isinstance(serialized, str)


# ---------------------------------------------------------------------------
# Rejected genes in serialization and display
# ---------------------------------------------------------------------------


class MockRejectedGene:
    """Lightweight stand-in for main.RejectedGene (avoids importing main.py)."""

    def __init__(self, gene: GeneEntry, reasons: list[str]):
        self.gene = gene
        self.reasons = reasons


class TestRejectedGeneSerialization:
    def test_summaries_include_rejected_genes(self):
        gene = GeneEntry(
            gene_symbol="FAKEGENE",
            confidence=0.3,
            pmid="999",
            source_quote="FAKEGENE showed a weak, unreplicated association with WMH.",
        )
        rejected = MockRejectedGene(
            gene=gene, reasons=["Low confidence", "NCBI lookup failed"]
        )
        result = MockPaperResult(pmid="999", rejected_genes=[rejected])
        summaries = _paper_results_to_summaries([result])
        assert len(summaries[0]["rejected_genes"]) == 1
        rg = summaries[0]["rejected_genes"][0]
        assert rg["gene"]["gene_symbol"] == "FAKEGENE"
        assert rg["reasons"] == ["Low confidence", "NCBI lookup failed"]

    def test_summaries_empty_rejected_by_default(self):
        result = MockPaperResult(pmid="111")
        summaries = _paper_results_to_summaries([result])
        assert summaries[0]["rejected_genes"] == []

    def test_json_serializable_with_rejected(self):
        gene = GeneEntry(
            gene_symbol="BAD1",
            confidence=0.2,
            pmid="888",
            source_quote="BAD1 was tested but showed no association with cSVD.",
        )
        rejected = MockRejectedGene(gene=gene, reasons=["Invalid symbol"])
        result = MockPaperResult(pmid="888", rejected_genes=[rejected])
        summaries = _paper_results_to_summaries([result])
        serialized = json.dumps(summaries, default=str)
        assert "BAD1" in serialized


class TestPrintRichSummaryWithRejected:
    def test_smoke_with_rejected_genes(self):
        data = cast(
            PipelineRunData,
            {
                "pipeline_config": {"model": "claude-opus-4-7", "dry_run": False},
                "search": {},
                "papers": {},
                "genes": {
                    "extracted": 2,
                    "validated": 1,
                    "rejected": 1,
                    "acceptance_rate": 0.5,
                },
                "token_usage": {"total_tokens": 0},
                "batch_validation_warnings": [],
                "papers_detail": [
                    {
                        "pmid": "123",
                        "source": "pmc",
                        "gene_count": 1,
                        "error": None,
                        "genes": [
                            {
                                "gene_symbol": "NOTCH3",
                                "protein_name": "Notch 3",
                                "pmid": "123",
                                "confidence": 0.95,
                                "gwas_trait": [],
                                "mendelian_randomization": False,
                                "omics_evidence": [],
                            }
                        ],
                        "rejected_genes": [
                            {
                                "gene": {
                                    "gene_symbol": "BADGENE",
                                    "protein_name": None,
                                    "pmid": "123",
                                    "confidence": 0.2,
                                },
                                "reasons": ["NCBI lookup failed"],
                            }
                        ],
                    }
                ],
            },
        )
        # Should not raise
        print_rich_summary(data)


class TestBatchRunsCostHalf:
    def test_estimate_cost_applies_the_batch_discount(self) -> None:
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        client = AnthropicClient()
        streamed = client.estimate_cost(usage)
        batched = client.estimate_cost(usage, batched=True)
        assert batched == streamed / 2

    def test_run_data_records_the_batch_mode(self) -> None:
        metrics = PipelineMetrics()
        metrics.token_usage.input_tokens = 1_000_000
        streamed = build_run_data(
            metrics=metrics, results=[], gene_result=None, batch_warnings=[],
            config=PipelineConfig(), days_back=7, dry_run=False,
            total_pmids_found=0, new_pmids_count=0,
        )
        batched = build_run_data(
            metrics=metrics, results=[], gene_result=None, batch_warnings=[],
            config=PipelineConfig(), days_back=7, dry_run=False,
            total_pmids_found=0, new_pmids_count=0, use_batch_api=True,
        )
        assert batched["pipeline_config"]["batch"] is True
        assert streamed["pipeline_config"]["batch"] is False
        assert (
            batched["token_usage"]["estimated_cost_usd"]
            == streamed["token_usage"]["estimated_cost_usd"] / 2
        )
