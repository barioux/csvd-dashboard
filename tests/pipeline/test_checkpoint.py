"""Coverage for the extraction checkpoint.

The checkpoint exists so a run that dies after hours of extraction does not
throw the extraction away: `_merge_processed_batch` is step 5 of 6, and until
it runs, nothing a paper cost has been recorded anywhere durable.
"""

import json
import os
import threading
from pathlib import Path

from pipeline import checkpoint
from pipeline.config import PipelineConfig
from pipeline.prompts import prompt_sha256


def _path(tmp_path: Path) -> str:
    return str(tmp_path / "checkpoint.jsonl")


class TestFingerprint:
    def test_it_covers_the_extraction_method(self) -> None:
        # Restoring a paper extracted by a different model, prompt or effort
        # would mix methods inside one dataset -- the thing EXTRACTION_MODEL
        # being pinned in code exists to prevent.
        base = checkpoint.fingerprint(PipelineConfig())
        other = checkpoint.fingerprint(PipelineConfig(llm_effort="low"))

        assert base != other
        assert base["model"] == "claude-opus-5-5"
        assert base["prompt_version"] == "v7"

    def test_it_covers_the_rendered_prompt_bytes(self) -> None:
        # The prompt version name alone stopped being enough once half the
        # prompt moved into disease/prompt.md: an edit there changes the
        # method without changing the version string.
        config = PipelineConfig()
        assert checkpoint.fingerprint(config)["prompt_sha256"] == prompt_sha256(
            config.prompt_version
        )

    def test_the_verbatim_quote_gate_is_part_of_the_method(self) -> None:
        # `report_provenance` drops unverified genes *inside* extraction, so
        # a paper extracted with the gate off returns genes a gated run
        # would never have accepted.
        assert checkpoint.fingerprint(
            PipelineConfig(require_verified_quotes=True)
        ) != checkpoint.fingerprint(PipelineConfig(require_verified_quotes=False))

    def test_the_truncation_limit_is_part_of_the_method(self) -> None:
        # It decides how much of the paper the model saw, and how much of it
        # the quote check had to find the quote in.
        assert checkpoint.fingerprint(
            PipelineConfig(max_paper_text_chars=50_000)
        ) != checkpoint.fingerprint(PipelineConfig(max_paper_text_chars=400_000))

    def test_it_ignores_the_search_window(self) -> None:
        # Papers are keyed by PMID, so a checkpoint written by --days-back 30
        # is valid for the --days-back 60 run that resumes it. That is exactly
        # how the chunked backfill makes progress.
        assert "days_back" not in checkpoint.fingerprint(PipelineConfig())


class TestRoundTrip:
    def test_appended_records_load_back_in_order(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        fp = checkpoint.fingerprint(PipelineConfig())

        checkpoint.append(path, fp, {"pmid": "1", "genes": []})
        checkpoint.append(path, fp, {"pmid": "2", "genes": [{"symbol": "APOE"}]})

        assert checkpoint.load(path, fp) == [
            {"pmid": "1", "genes": []},
            {"pmid": "2", "genes": [{"symbol": "APOE"}]},
        ]

    def test_the_header_is_written_once(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        fp = checkpoint.fingerprint(PipelineConfig())

        checkpoint.append(path, fp, {"pmid": "1"})
        checkpoint.append(path, fp, {"pmid": "2"})

        assert Path(path).read_text().count('"fingerprint"') == 1

    def test_a_missing_file_loads_as_nothing(self, tmp_path: Path) -> None:
        fp = checkpoint.fingerprint(PipelineConfig())
        assert checkpoint.load(_path(tmp_path), fp) == []


class TestRejection:
    def test_a_checkpoint_from_another_method_is_discarded(
        self, tmp_path: Path
    ) -> None:
        path = _path(tmp_path)
        stale = checkpoint.fingerprint(PipelineConfig(llm_effort="low"))
        checkpoint.append(path, stale, {"pmid": "1"})

        assert checkpoint.load(path, checkpoint.fingerprint(PipelineConfig())) == []
        # Discarded, not merely ignored: leaving it in place would make the
        # next run with the old settings silently resume half a dataset.
        assert not Path(path).exists()

    def test_a_checkpoint_from_before_the_quote_gate_is_discarded(
        self, tmp_path: Path
    ) -> None:
        # Run A ran with PIPELINE_REQUIRE_VERIFIED_QUOTES unset and
        # checkpointed genes whose source_quote is not in the paper. Run B
        # turns the gate on. Restoring those papers would publish exactly
        # what the gate exists to refuse, under a report saying none were
        # dropped -- the gate runs inside extraction, and a restore is not
        # an extraction.
        path = _path(tmp_path)
        ungated = checkpoint.fingerprint(
            PipelineConfig(require_verified_quotes=False)
        )
        checkpoint.append(path, ungated, {"result": {"pmid": "37069360"}})

        gated = checkpoint.fingerprint(PipelineConfig(require_verified_quotes=True))
        assert checkpoint.load(path, gated) == []
        assert not Path(path).exists()

    def test_a_truncated_final_record_is_dropped(self, tmp_path: Path) -> None:
        # The process is killed mid-append: the last line is half a JSON
        # object. Everything before it is still good extraction.
        path = _path(tmp_path)
        fp = checkpoint.fingerprint(PipelineConfig())
        checkpoint.append(path, fp, {"pmid": "1"})
        with open(path, "a") as handle:
            handle.write('{"pmid": "2", "gen')

        assert checkpoint.load(path, fp) == [{"pmid": "1"}]

    def test_a_file_that_cannot_be_read_loads_as_nothing(
        self, tmp_path: Path
    ) -> None:
        # A directory where the checkpoint should be: exists(), so the
        # missing-file path does not catch it, and read_text() raises.
        path = tmp_path / "checkpoint.jsonl"
        path.mkdir()

        current = checkpoint.fingerprint(PipelineConfig())
        assert checkpoint.load(str(path), current) == []

    def test_an_unreadable_header_discards_the_file(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        Path(path).write_text("not json at all\n")

        assert checkpoint.load(path, checkpoint.fingerprint(PipelineConfig())) == []


class TestClear:
    def test_clear_removes_the_file(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        checkpoint.append(path, checkpoint.fingerprint(PipelineConfig()), {"pmid": "1"})

        checkpoint.clear(path)

        assert not Path(path).exists()

    def test_clearing_a_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        checkpoint.clear(_path(tmp_path))

    def test_append_creates_the_parent_directory(self, tmp_path: Path) -> None:
        path = str(tmp_path / "nested" / "deeper" / "checkpoint.jsonl")

        checkpoint.append(path, checkpoint.fingerprint(PipelineConfig()), {"pmid": "1"})

        assert json.loads(Path(path).read_text().splitlines()[1]) == {"pmid": "1"}


_FINGERPRINT = checkpoint.fingerprint(PipelineConfig())


class TestRemove:
    """Merged papers leave the file; the rest stay for the run that merges them."""

    def _write(self, path: str, *pmids: str) -> None:
        for pmid in pmids:
            checkpoint.append(path, _FINGERPRINT, {"result": {"pmid": pmid}})

    def test_only_the_named_papers_are_dropped(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        self._write(path, "111", "222", "333")

        checkpoint.remove(path, {"111", "333"})

        records = checkpoint.load(path, _FINGERPRINT)
        assert [r["result"]["pmid"] for r in records] == ["222"]

    def test_the_file_goes_when_nothing_remains(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        self._write(path, "111")

        checkpoint.remove(path, {"111"})

        assert not Path(path).exists()
        assert not Path(path + ".tmp").exists()

    def test_removing_from_a_missing_file_is_not_an_error(self, tmp_path: Path):
        checkpoint.remove(_path(tmp_path), {"111"})

    def test_a_half_written_record_is_dropped_with_the_merged_ones(
        self, tmp_path: Path
    ) -> None:
        path = _path(tmp_path)
        self._write(path, "111", "222")
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write('{"result": {"pmid": "33')

        checkpoint.remove(path, {"111"})

        assert [r["result"]["pmid"] for r in checkpoint.load(path, _FINGERPRINT)] == [
            "222"
        ]

    def test_a_record_naming_no_paper_is_dropped(self, tmp_path: Path) -> None:
        # It can never be merged, so it can never be removed by name, and it
        # would otherwise keep the file alive forever.
        path = _path(tmp_path)
        checkpoint.append(path, _FINGERPRINT, {"metrics": {}})
        self._write(path, "222")

        checkpoint.remove(path, {"222"})

        assert not Path(path).exists()

    def test_a_file_that_cannot_be_read_is_left_alone(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        path = _path(tmp_path)
        self._write(path, "111")

        def refuse(*args, **kwargs):
            raise OSError("io")

        monkeypatch.setattr(Path, "read_text", refuse)

        checkpoint.remove(path, {"111"})

        assert Path(path).exists()

    def test_a_prune_that_cannot_be_written_is_not_an_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The prune runs after `_merge_processed_batch` committed the genes
        # and the PMIDs. An OSError escaping here reached run_pipeline's
        # failure handler, and a run whose data is in the database was
        # recorded -- and published -- as failed.
        path = _path(tmp_path)
        self._write(path, "111", "222")

        def refuse(*args, **kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr(os, "replace", refuse)

        checkpoint.remove(path, {"111"})

        # The merged record stays, which costs nothing: pubmed_refs holds
        # its PMID and the next run filters it out before extraction.
        assert [r["result"]["pmid"] for r in checkpoint.load(path, _FINGERPRINT)] == [
            "111",
            "222",
        ]


class TestConcurrentRuns:
    """One checkpoint file, two runs: a prune may not undo the other's work."""

    def test_a_paper_appended_mid_prune_survives_it(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # A nightly --days-back 7 run merges its papers and prunes while a
        # --days-back 365 re-run is still extracting. The prune rewrites the
        # file from the snapshot it read; without a lock the papers the long
        # run appended in between are gone, and they were paid for.
        path = _path(tmp_path)
        checkpoint.append(path, _FINGERPRINT, {"result": {"pmid": "111"}})
        checkpoint.append(path, _FINGERPRINT, {"result": {"pmid": "222"}})

        long_run = threading.Thread(
            target=checkpoint.append,
            args=(path, _FINGERPRINT, {"result": {"pmid": "333"}}),
        )
        real_replace = os.replace

        def replace_after_the_other_run_appends(src, dst):
            long_run.start()
            long_run.join(timeout=0.5)
            assert long_run.is_alive(), "the append should wait for the prune"
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", replace_after_the_other_run_appends)

        checkpoint.remove(path, {"111"})
        long_run.join(timeout=5)

        assert [r["result"]["pmid"] for r in checkpoint.load(path, _FINGERPRINT)] == [
            "222",
            "333",
        ]
