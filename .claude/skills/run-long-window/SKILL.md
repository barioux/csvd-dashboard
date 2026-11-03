---
name: run-long-window
description: "Use when running the pipeline over a long --days-back window: checkpoint and resume semantics, the --batch exception, backfill_pubmed, and cost estimation."
---

# Running a long window

```bash
uv run python -m pipeline.main --pubmed --days-back 365 --export
```

`--days-back 365` is one run, and `_merge_processed_batch` is step 5 of 6: genes
reach the database and `record_processed_pmids_batch` writes the PMIDs only once
every paper has been extracted. Measured against the runs in `logs/json/`, a
year's window is ~795 papers (the live count for the committed `SVD_QUERY`), ~3
hours and ~$70 of extraction at Claude Opus 5's prices (the pipeline now runs
Opus 5.5, 20% cheaper per token; re-measure before quoting a new figure), and a
crash at hour two used to discard all of it and record nothing.

**`pipeline/checkpoint.py` is what makes that survivable.** Each paper is
appended to `logs/pipeline_checkpoint.jsonl` as it finishes, through the
`on_complete` hook `process_papers_concurrently` calls, and a re-run restores
them rather than paying for them again. Six things about it are load-bearing:

- **Papers accumulate into their own `PipelineMetrics`, not the run's.**
  `process_papers_concurrently` gives each paper a fresh accumulator and folds
  it in with `PipelineMetrics.fold()` when the paper finishes, so the hook
  receives what _that paper_ contributed and the checkpoint can store it. Those
  contributions are stored rather than derived from the `PaperResult`, because
  they are not derivable: token usage appears nowhere in the result, and a paper
  with no text available increments `papers_processed` but neither
  `fulltext_retrieved` nor `abstract_only`. Every count is per paper, including
  `papers_processed`: it used to be added in bulk once every paper had returned,
  so a run that died in step 3 published `processed: 0` beside the full texts,
  genes and tokens of the papers that had finished, and the checkpoint stored
  `papers_processed: 0` for each of them (`_restored_paper` forces it to 1 for
  files written that way). A paper that _fails_ folds only its token usage into
  the run: its genes are counted by the run that retries it, and
  `genes_extracted` is incremented only once validation held, so an NCBI outage
  mid-validation no longer left genes in `extracted` with nothing on the
  validated or rejected side.
- **The fingerprint is the extraction method.** Model, model version, thinking
  mode, effort, prompt version, the two confidence floors,
  `require_verified_quotes` and `max_paper_text_chars` -- everything that
  changes what extraction returns for the same paper. The last two are there
  because the verbatim-quote gate drops genes _inside_ extraction
  (`report_provenance`, before validation), and the truncation limit decides how
  much of the paper the model and that check were given: a restore is not an
  extraction, so a paper checkpointed with the gate off would otherwise be
  folded whole into a run configured to refuse those genes, and replay its
  `dropped_unverified: 0` into the report. A mismatch deletes the file rather
  than resuming from it: restoring a paper extracted under different settings
  would mix methods inside one dataset, which is what pinning `EXTRACTION_MODEL`
  in code exists to prevent. `--days-back` is deliberately _not_ in it, so a
  checkpoint written by a 30-day run is valid for the 60-day run that resumes
  it.
- **Only successes are checkpointed, and the file is pruned -- not cleared --
  after the merge.** A failed paper is never written to `pubmed_refs`, so the
  next run retries it; checkpointing one would restore the failure instead and
  retire the paper without ever extracting it. Once the merge has run,
  `pubmed_refs` is the durable record _for the papers it merged_, and
  `checkpoint.remove` drops exactly those records, unlinking the file only when
  none remain. `_restore_checkpoint` restores only the PMIDs in this run's
  window, so a checkpoint written by a crashed `--days-back 365` holds hundreds
  of papers a nightly `--days-back 7` never publishes; unlinking the whole file
  there, as the merge used to, destroyed that extraction with nothing saying so.
  `--dry-run` neither writes nor prunes it -- it merges nothing, so it has not
  earned the right to delete a real run's saved work. The prune is **best
  effort**, like the append: it runs after the genes and the PMIDs are
  committed, so an OSError there is logged and the records stay rather than
  raising into `except BaseException` and recording a run whose data is in the
  database as failed. What is left behind costs nothing -- `pubmed_refs` holds
  those PMIDs and the next run filters them out.
- **One file, so it is locked.** Every run reads and writes
  `logs/pipeline_checkpoint.jsonl`, and a scheduled nightly run can fire over a
  long re-run -- the case `LOG_FILE`'s PID suffix and `_write_progress`'s
  per-process `.tmp` already anticipate. `append`, `load`, `clear` and `remove`
  each hold an exclusive `flock` on a sibling `.lock` file for the whole of
  their work, so the prune's read-filter-replace cannot rewrite away the papers
  the other run appended in between. The lock is that sibling file and not the
  checkpoint itself, because `remove` unlinks and replaces the checkpoint: a
  lock on an inode that is no longer the file at that path guards nothing.
- **The record carries the paper's quote counts.** `ProvenanceTally` keeps a
  per-paper share under `paper_scope`, a context variable `run_one` sets around
  each paper, and the checkpoint stores it as `provenance`; a restore replays
  it. A before-and-after delta would not do: papers are extracted concurrently
  into one module-level tally, so the delta around one paper carries whatever
  the others recorded meanwhile. Without this a resumed run published
  `quotesChecked` over part of the genes it counted.
- **A resumed run's cost includes the crashed run's spend**, which is also in
  that run's own failed report. `_restore_checkpoint` says so as a step action
  ("Restored N papers … counted in both runs"), so the two reports read
  correctly together; that is why the restore is visible in the widget rather
  than silent.
- **The merge sees the window's order, not the resume's.** Both extraction paths
  end at `_in_request_order(pmids, results)`, which reorders the batch by the
  run's own PMID list. `results` used to be `restored + pending`, so a resumed
  run merged the same two papers in the opposite order — and the merge keeps the
  _first_ occurrence's quote, confidence and protein
  (`_build_combined_gene_data`) and inserts rows in that order, so the crash
  changed the published `sourceQuote`, the published `confidence` and the `id`s
  `data/table1.json` is ordered by. The streaming path was already in request
  order without a restore (`process_papers_concurrently` returns
  `[task.result() …]`), so this is parity rather than a new rule; `--batch`,
  which assembled its results by arrival stage, gains the same determinism.

`--batch` checkpoints too, through the same `_paper_finished_hook`. Its results
arrive together, but they are paid for the moment they do, and validation and
the merge still stand between them and `pubmed_refs`: a crash at either step
used to lose the whole batch and the next run resubmitted every paper. Each
paper is written as its validation finishes, from its own `PipelineMetrics`,
with its share of the quote tally (`results_by_custom_id` runs
`report_provenance` under `paper_scope`). One difference from the streaming path
is deliberate: the batch's tokens are one figure for the whole batch, not per
paper, so a restored batch paper carries none -- the crashed run's own failed
report holds the spend. The batch path used to write nothing and also reached
the unconditional clear without restoring a streaming run's papers.

**The poll window of a `--batch` run is the one unprotected stretch**: the batch
id lives only in the `Submitted batch %s` log line, so a crash inside the
up-to-24h wait loses the submission and a re-run pays again -- see "Commands" in
the root `CLAUDE.md`. `--test-mode` counts what a window would process before
paying for it, and `scripts.backfill_pubmed` walks it in committed chunks so the
database is updated as it goes; both are described under "Long windows" in
`pipeline/CLAUDE.md`.

```bash
uv run python -m pipeline.main --test-mode --days-back 365
uv run python -m scripts.backfill_pubmed --days-back 365 --step 30
```
