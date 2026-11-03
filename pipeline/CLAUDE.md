# Pipeline

Guidance for `pipeline/` and `tests/pipeline/`. The repo-wide contracts — the
JSON wire format, the filtering rules, the islands and the two figures — stay in
the root `CLAUDE.md`.

## Extraction

**The extraction model is pinned in code, not configured.** `EXTRACTION_MODEL`
in `pipeline/config.py` is `claude-opus-5-5`, and `PipelineConfig.llm_model` is
a read-only property returning it — there is no `PIPELINE_LLM_MODEL`. Extraction
output is compared across runs, against the recorded cassettes in
`tests/pipeline/cassettes/`, and against a gold standard, so the model is part
of the method rather than a deployment knob; a per-machine override would make
two runs of the same code incomparable with nothing saying so. Changing it is a
code change with a re-recorded harness.

**The prompt version is pinned the same way, by refusal rather than by
fallback.** `PIPELINE_PROMPT_VERSION` still selects one, but `__post_init__`
refuses a name `prompts.py` does not carry, and `build_extraction_prompt` raises
instead of falling back to the default. The fallback was safe for the prompt and
false for every record of the run: `report_metadata`, `pipeline_runs.report`,
`data/pipeline_run.json` and the checkpoint fingerprint all publish
`config.prompt_version` verbatim, so a typo'd `PIPELINE_PROMPT_VERSION` named a
version that never existed as the method behind the rows it was extracting — on
the public About page — and correcting the typo afterwards changed the
fingerprint, discarding a checkpoint whose papers had been extracted with the
very same prompt. A run reports the prompt it ran.

`thinking_config` is always `{"type": "adaptive", "display": "summarized"}` —
`budget_tokens` is a 400 on 4.7 and later, and Opus 5.5 rejects `disabled` at
every effort — and `estimate_cost` returns a number, never `None`.

**`output_config` always carries the effort, `high` included.** It used to be
omitted at `high` as wire noise, because `high` was Claude Opus 5's API default.
Claude Opus 5.5's default is `medium`, so the same omission would run every
default-config paper a level below the `effort` the run report publishes and the
checkpoint fingerprints, with nothing failing.

A safety refusal is a normal 200 with `stop_reason: "refusal"`, not an error.
`anthropic_client.py` checks for it _before_ reading any content: reading it
would fail JSON parsing and spend the validation-retry budget reporting a
malformed response. The server-side `fallbacks` parameter is unavailable on the
Message Batches API, so `results_by_custom_id` in `batch_extraction.py` runs the
same two checks -- refusal, then `max_tokens` -- on every batch entry before
reading its content. It did not, so a refused entry was logged as a prose-only
answer and a truncated one could never raise the token-ceiling warning; the
batch path also accumulates each entry's usage and records each as an Anthropic
call, which is what put a `--batch` run's tokens, cost and services row in the
report at all. It shares the reasoning accounting too — the thinking count the
API reports in `usage.output_tokens_details`, which `accumulate_usage` takes
(`_account_reasoning` estimates from the character ratio only for a response
that reports none), and `_record_thinking_trace` writing the trace to the event
log — because a `--batch` run otherwise published `thinkingTokens: 0` beside the
batch's real output count and left an auditor no trace to read for any paper the
batch extracted.

**A refusal names its classifier, and nothing retries it on another model.**
Both paths log the `stop_details.category` (`_refusal_category`): Opus 5.5 runs
a biology classifier Opus 5 did not, and on a corpus of genetics papers `bio` is
the category a false positive would carry. The streaming path deliberately has
no `fallbacks` opt-in either, although the API offers one there: a fallback
would extract the refused paper with a different model under a run report that
names this one, which is what pinning the model exists to prevent.

**A failed entry is logged with the API's own reason.** A non-succeeded result
was logged as its bare type (`Batch entry 37063705: errored`), and the `error`
payload the API attaches — `result.error.error`, type and message — was dropped.
A request-level rejection applies to _every_ entry, so that read as an
unexplained "errored" over every paper in the batch with the cause nowhere, and
the operator's only move was to re-submit and pay for the same rejection.
`_error_detail` reads it through `getattr`, because an expired or canceled
result genuinely carries none. The per-paper string the run report publishes is
still the generic "no result returned by the batch": `results_by_custom_id`
signals a failed entry by _absence_ from its result map, so carrying the cause
that far would mean changing what it returns.

**The batch path runs the provenance check too.** `report_provenance` lives in
`pipeline/citations.py` and both callers hand it the same entries: the streaming
path from `_stream_and_parse`, the batch path from `results_by_custom_id`, which
`submit_and_collect` gives the text each document block carried so
`locate_quote`'s offsets mean what the API's do. Until it did, `--batch` parsed
the tool blocks and returned, so `require_verified_quotes` dropped nothing there
and the run report published 0/0 quotes checked over every gene the batch found.

**An overload is retried, not reported.** A 529 arrives as `OverloadedError`
after the SDK's own retries; an SSE `error` event mid-stream --
`overloaded_error` eight minutes into a paper is the common one -- is raised by
the SDK through `_make_status_error` with the _stream's_ response, the 200 that
opened it, so it is a bare `APIStatusError` whose `status_code` is 200.
`_is_retryable_status_error` routes both into the connection-retry path; they
used to be fatal, and the second was recorded as a clean 200 call. Every failing
branch of `extract` also releases the TPM reservation now, not only the two that
retry: a burst of five failed papers used to leave 100,000 tokens reserved for
the rest of the minute with no log line. A connection attempt that fails is
recorded through `record_service_failure`, as a call _and_ an error --
`status=None` reads as success to the recorder, so four failed attempts used to
publish as four clean calls beside three retries.

**The schema admits the prompt's own spellings.** The tool's `gwas_trait` enum
is `CANONICAL_TRAITS`: the tracked keys, the `untracked` terms, and the
prompt-sourced `synonyms` from `disease/vocabulary.json`. The prompt's frozen
canonical sentence asks for `cerebral-microbleeds` while the tracked key is
`CMB`, and with the spelling refused a model that obeyed the prompt failed the
paper after two paid calls -- which instruction it followed decided whether the
paper was lost. `_build_combined_gene_data` folds `TRAIT_SYNONYMS` by exact key
before the tracked filter, so the spelling is stored as `CMB` rather than
dropped as untracked. `tests/pipeline/test_prompt_vocabulary.py` reconciles the
prompt against the enum as well as against the vocabulary.

**The enum is part of the method, and it is the half that constrains the
model.** The frozen canonical sentence in `prompts.py` is what the model is
_asked_ for; the tool schema's `gwas_trait` enum is what it is _allowed_ to say,
and it is generated from `disease/vocabulary.json` at import time. So an edit to
a `traits[*].key` changes what a run may report with no prompt edit and no
re-recorded cassette — the drift the frozen sentence is documented to prevent,
arriving through the other door. It is not hypothetical: the enum admits `CMB`,
`NODDI` and `lacunar stroke`, none of which the sentence names, and the recorded
cassette for PMID 33773637 has the model emitting `lacunar stroke` 14 times
where the sentence asks for `lacunes`. Both spellings are tracked traits, so
both reach `data/table1.json` as separate values. The reconciliation therefore
runs **both** ways: prompt ⊆ enum, and enum ⊆ prompt terms plus a named
allow-list in the test, one entry per term with the reason it is admitted
unasked. Add the entry with its reason; do not widen the test.

**The SDK streams over `httpx2`, not `httpx`.** The pipeline pins `httpx` 0.28
for its own clients, and `anthropic` 2.x is built on the separate `httpx2`
package; the two exception hierarchies are unrelated. `client.send()` wraps a
failure to _open_ a request in `APIConnectionError`, but with `stream=True` it
returns once the headers arrive and the body is read by `get_final_message()`,
where a disconnect or read timeout surfaces as a raw `httpx2` exception. The
connection-retry branch in `AnthropicClient.extract` therefore catches
`httpx2.TransportError`; naming `httpx.ReadError` there matched nothing the SDK
can raise, and the one failure a ten-minute stream is exposed to fell through
with zero retries.

### Effort stays at `high`, and the margin is one gene

`PIPELINE_LLM_EFFORT` defaults to `"high"`, and stays there. All three arms were
recorded over the same ten golden fixtures under a decision rule fixed before
the numbers were seen: _lower the default to the cheapest arm within 2 points of
`high` on pooled recall and on verbatim-quote rate_:

| effort   | pooled recall   | genes | verbatim | output tokens | cost  | wall   |
| -------- | --------------- | ----- | -------- | ------------- | ----- | ------ |
| low      | 36/46 = 78%     | 113   | 99%      | 31,167        | $1.71 | 6m22s  |
| medium   | 41/46 = 89%     | 135   | 100%     | 46,812        | $2.10 | 9m42s  |
| **high** | **42/46 = 91%** | 138   | 99%      | 90,450        | $3.19 | 17m35s |

`medium` fails by 2.2 points, which is one gene (`ICA1L` on PMID 36180795, the
GIGASTROKE paper the prompt is not aimed at) -- finer than n=46 can resolve, so
a re-run could land either side. **`medium` is the lever** if cost or latency
ever binds: 34% cheaper, 55% of the wall-clock, quote fidelity if anything
better. `low` is not: 13 points, and it stops producing prose.

**That sweep was measured on Claude Opus 5, and the pipeline now runs Opus
5.5.** Effort names do not carry across models: Anthropic reports 5.5 at
`medium` matching or exceeding 5 at `high` on its coding and knowledge-work
evaluations, and 5.5 thinking more per turn than 5 at the same level (prompting
guide, "Calibrate effort"). Neither is a measurement of this extraction task.
`high` was kept as the default through the model change so that one step changes
one variable; re-run the three arms on 5.5 before reading the table above as a
statement about it. The recall and quote figures below are Opus 5.5's: the
golden cassettes were re-recorded on it at `high` on 2026-11-02, where the same
eight calls cost $2.48 against Opus 5's $3.72, on 77,177 output tokens against
101,007.

### The prompt is v7: a template plus `disease/prompt.md`, and v4's guards were measured rather than assumed

`pipeline/prompts.py` holds the methodology as a template with
`{{ section.id }}` slots and `disease/prompt.md` holds the disease prose, one
`## id` per slot. `render_prompt` refuses an unknown slot, an unreferenced
section and a residual `{{`, and
`tests/pipeline/csvd/test_csvd_prompt_assembly.py` pins the cSVD rendering
byte-identical to the v6 literals (sha256 `70908abc…`), which is why the recall
baseline and the golden cassettes carried over without re-recording.
`prompt_sha256()` hashes the rendered system prompt and instructions together,
because the version name stopped being enough the moment half the prompt moved
into a data file.

`pipeline/prompts.py` holds one prompt; its module docstring records why v4's
stricter exclusion guards were measured and ruled out rather than assumed, and
`tests/pipeline/test_prompts.py` pins it. The 43-vs-17 gap on PMID 37069360 is a
**curation** difference, not a prompt defect, which is why the confidence floor
is split by insert-versus-update instead (see "What is not proven").

### Provenance

`source_quote` is checked two ways by `pipeline/citations.py`, and the two do
not cover the same ground. `verify_quote` matches it against a span the **API**
cited; `locate_quote` finds it in the **document** itself. Enabling that
required two changes the API forces together: the schema had to leave
`output_config.format` (citations beside it are a 400) and the paper had to
travel in a real `document` block, because `start_char_index`/`end_char_index`
index into exactly the bytes sent there — an XML wrapper would shift every
offset by its own length.

**The tool is deliberately not `strict`.** On Claude Opus 5, `strict: true`
corrupts every free-text string in a tool's input -- `source_quote` above all,
the one field this work exists to make trustworthy -- so the schema guarantee
moved from the decoder to validate-and-retry. The measurement (9/9 corrupt with
the flag, 4/4 clean without, 2026-08-31) is in the `extraction_tool` docstring
in `pipeline/config.py` and pinned by `tests/pipeline/test_config.py`; do not
flip it back on the strength of how it reads. It has not been repeated on Opus
5.5, so it stays off there until it is.

**The two numbers this produced, and they are not the same number.** Across the
124 genes in the golden cassettes, re-recorded 2026-11-02 on Claude Opus 5.5
over the same fixture text as the Opus 5 recording before it:

| Measure                                | Rate              |
| -------------------------------------- | ----------------- |
| quotes verbatim in the paper           | **122/124 = 98%** |
| quotes matched to an API citation span | **98/124 = 79%**  |
| quotes corrupted                       | 0/124             |

The two unverified quotes are one sentence, quoted for both `VCAN` and
`SH3PXD2A` on PMID 33293549: the paper has it after a semicolon as "four WMH
risk loci individually showed…", and the model capitalised it into a sentence of
its own. That is a transcription edit the check is right to refuse, not invented
prose. Opus 5.5 still writes its evidence as cited `text` blocks before the tool
call, the same block shape as Opus 5 — the change that sends text _between_ tool
calls to `thinking` blocks did not reach this single-call response.

The gap between them is not a quality problem. The API returns spans only where
the model wrote prose, because the instruction asks for supporting evidence in
prose rather than one sentence per gene, so the citation rate tracks prose
volume rather than quote fidelity: papers with few genes verify near 100% and
the gene-dense ones drag it down.

**Both numbers move on every re-record, and neither move is a code change.** The
Opus 5 recordings measured 137/138 = 99%, 130/134 = 97% and 140/140 verbatim,
and 45/138 = 33%, 87/134 = 65% and 61/140 = 44% cited; the last Opus 5
cassettes, counted by the same replay as the Opus 5.5 ones, gave 139/139 and
74/139 = 53%. The citation rate is the one that swings, because the model writes
a different amount of prose each time; Opus 5.5 wrote more of it (about 20,100
characters before the tool call against 13,100) and attached 56 citations
against 43. The verbatim rate reached 100% on 2026-09-11 because the refetched
fixtures no longer carry the artifact every earlier failure was: the model
reproducing a sentence with a superscript reference marker spliced into it
(`( Fig. 111 2 )` for the document's `( Fig. 2 )`, all on PMID 35511193), which
the parser's row joining and whitespace collapse removed from the text the model
reads. That is a retrieval-and-transcription artifact worth knowing about, not
invented prose, and it is exactly what the verbatim check exists to surface.

**So the gate turns on the verbatim check, not the citation match.**
`require_verified_quotes` (`PIPELINE_REQUIRE_VERIFIED_QUOTES`) filters on
`locate_quote`: gating on citations would discard between a third and two thirds
of the genes for want of prose, while gating on the document has dropped at most
two genes in 124, and those two for one capitalised letter. The citation match
is still computed and logged, because it is the stronger claim where it exists —
the API attests the model used that span — but it cannot be the gate at 33–79%
coverage.

It still defaults to `False`. This lands as measurement: a quote that fails is a
finding worth seeing before it is a gene worth losing, and each failure is
logged by gene symbol. The first recording's one failure is instructive —
`CENPF` on PMID 37069360, where the quote is a genomic-table row the model
reassembled from cells (`9,033/38,008 8.20 × 10 −11 …`) rather than a sentence.
That is a retrieval artifact worth knowing about, not a hallucination.

Raising the _citation_ rate would mean asking for one supporting sentence per
gene, which buys a signal already available for free from the document and costs
output tokens on every run forever. Measured and not done.

Whitespace is collapsed before comparing, and that is load-bearing rather than
cosmetic; the docstring of `pipeline/citations.py` records why, what it
recovered, and the containment option measured and not taken.

**The reasoning trace is retained as an audit artifact, never as evidence.**
`_record_thinking_trace` in `anthropic_client.py` writes each response's
extended-thinking text to the SQLite event log, capped and labelled with its
attempt number and whether that attempt was the accepted one. It is never
published -- no `data/*.json` key, no `lib/types.ts` field -- because an
extended-thinking block is not a verified causal account of how the model
reached its answer; `source_quote` remains the cited evidence. The
single-sentence rule `locate_quote` enforces (`is_sentence_like`,
`_MIN_QUOTE_WORDS`) is documented in `pipeline/citations.py`.

## Run reporting

The pipeline builds two reports from one run. `pipeline/report.py` still builds
`PipelineRunData` for the three sinks it always had -- the JSON log file, the
rich console summary, and the notification plus SQLite event log.
`pipeline/run_report.py` builds `PipelineRunReport` beside it, from the same
inputs, and that one is published: it is stored in `pipeline_runs.report` and
exported to `data/pipeline_run.json` for the About page's widget. Nothing in the
second recomputes a metric the first computed.

- **`PipelineRunReport` is the schema on both sides of the database.** The
  export re-validates the stored document through it rather than passing it
  through, so a row written by an older pipeline is either brought up to the
  current shape or refused — the dashboard never receives a document its
  TypeScript does not describe. Field order in the model _is_ wire order, and
  the export is byte-gated, so a reordering is a visible diff.
- **The column is `JSON`, not `JSONB`.** jsonb normalises key order and this
  repo gates every exported file on bytes. A write-once audit record that is
  never queried by value has no use for jsonb's indexing either.
- **Detail lists are capped at 200 (`DETAIL_CAP`) and carry `shown` beside
  `total`.** Counts above them are never capped. A 500-paper run would otherwise
  turn a reviewed file into an unreviewable diff, and a truncated list that did
  not say so would read as the whole record.

- **The migration is `009`, and the number is load-bearing.** It was authored as
  `008` while another branch's `008_add_gene_annotations` was in flight. Both
  declared `down_revision = "007"` and the two files never touched, so git
  merged them cleanly and CI, which has no database, saw nothing.
  `alembic_version` then held the ambiguous string `"008"`,
  `alembic upgrade head` considered itself finished, and `pipeline_runs` never
  gained `status`, `duration_seconds` or `report`. Every layer below swallows
  that: alembic warns and continues, `read_pipeline_run` catches the read error,
  and the About page renders its fallback card -- so the widget simply never
  appears and nothing says why. `tests/pipeline/test_alembic_migrations.py`
  walks the revision graph and fails on a duplicate id, a second head or a
  dangling `down_revision`.

### One step vocabulary, and it is timed now

`pipeline/steps.py` holds `PIPELINE_STEPS` -- the six keys the progress file
writes, each with the label the dashboard shows -- and `StepRecorder`, which
times them off the same `progress.report(n)` index.

**A run is only `completed` when nothing warned.** The widget exists to make
losses visible, and a green badge over 24 rejected genes would be the silence it
replaces. Warnings are raised as one row carrying a count, never one row per
occurrence, and from three steps rather than one:

| Step                | Warns on                                                                                                                                                                                                                                              |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `processing_papers` | papers with no retrievable text, papers that failed outright, papers cut short at `max_paper_text_chars`, genes below the validation floor, genes dropped for an unverifiable quote, truncated model responses, services that errored or were retried |
| `batch_validation`  | anything `batch_validate` flagged (over-extraction, say)                                                                                                                                                                                              |
| `merging_database`  | new genes the insert floor refused                                                                                                                                                                                                                    |

Two of those are easy to lose. Genes removed for an unverifiable quote
(`require_verified_quotes`) are dropped _inside_ extraction, so `extracted`
never saw them and the validation gate never scored them; `_quote_rejections`
turns them into `RejectedGene(reasons=["Quote not found in the paper"])` and
`_record_processing_actions` raises `quote_unverified` naming them. They are
deliberately _not_ added to `genes_rejected`, which is the validation floor's
number. A paper cut short at `max_paper_text_chars` (400,000 characters; the
Docling PDF path in practice) still goes into `pubmed_refs` as processed with
full text, so no later run revisits it; `paper_text_truncated` carries that
through `ExtractionOutcome`, `PaperResult` and the checkpoint to a
`paper_truncated` warning naming the PMIDs. Warning titles agree with their
counts through `_count` / `_were`, and a failure carries no `subject`.

**A failed run persists its report too.** `_record_failed_run` writes the
`pipeline_runs` row -- `status: "failed"`, every classified `RunError`, the
report built through `build_run_data` exactly as a completed run's is -- from
whatever the run had in hand: the search counts once step 1 has answered, every
finished paper as `_paper_finished_hook` appends it to `processed_so_far`
(collected whether or not the run is checkpointing), and the `MergeResult` the
instant `merge_gene_entries` returns, before `record_processed_pmids_batch`
opens its own transaction. So a cancellation inside step 3 or a failure after
the merge publishes real papers, real counts and the genes the insert floor
refused, and the funnel adds up. It is best-effort by construction -- the
database is often the thing that just failed -- and so are `_record_and_notify`
(the event-log write sits in its own `try`) and `_publish_export`, which catches
`Exception` because asyncpg, `DB_*` and pydantic errors all escaped a narrower
clause without the "behind the database" diagnostic.

**A failed run does not date the data.** `read_pipeline_status`, behind
`data/pipeline_status.json` and the About page's "up-to-date as of", skips
`status = 'failed'` rows; `read_pipeline_run` still returns them, so the widget
shows the failure while the date stays on the last run that finished as a whole.
That is **not** the offline modes' reasoning: `--local-pdfs` and `--pmids` write
no row because they publish nothing, whereas a failed run may well have changed
the data -- the merge commits before the run is anywhere near done, and
`--export` ships those rows.

### Failures are classified, not dumped

`pipeline/run_errors.py`'s `classify()` is the one place an exception becomes a
reader-facing reason; the matching rules and their reasons are commented there.
One trap worth repeating: **a bare `"429"` is deliberately not a rate-limit
phrase** -- PMIDs are eight digits, so `"Error processing PMID 34291234"`
matches it, and phrases are whole-word matches on the message alone, never on
the class name.

### API telemetry

`pipeline/api_telemetry.py` records which external services a run talked to,
through `event_hooks` installed once on `AsyncHttpClientManager`, and the
module's docstrings carry what was measured (why a 404 is not an error, a
followed redirect is one call, timing comes from a monotonic stamp, and why the
Anthropic row undercounts the SDK's own retries -- `build_async_client` keeps
`anthropic.DEFAULT_MAX_RETRIES` -- and is left that way). An unregistered host
records under its own hostname rather than being dropped.

**What stays out of `SERVICES`, and why.** The fallback above exists for one
real case, not as a waiting room: `download_and_parse_pdf` follows whatever
open-access URL Unpaywall reported, so the host set is the publishing world and
no allow-list could be written for it — a row reading `link.springer.com` is the
answer, not a gap. Four more absences are decisions:

- **ClinVar** calls the same `esearch.fcgi` and `esummary.fcgi` on the same
  `eutils.ncbi.nlm.nih.gov` as gene validation, gene info and citations. Only
  the query parameters differ, and the recorder never sees those. An entry for
  it would have to precede `ncbi_eutils` and would then swallow every other
  E-utilities call rather than distinguishing its own, so ClinVar is reported as
  "NCBI E-utilities" — one service at the URL, one row.
- **Notification egress** (`notifications.py`). A row could never reach the
  report of the run that sent it: `_record_and_notify` runs _after_
  `_finalize_run` has sealed the recorder, so it would land in the next run's
  inventory. Apprise also owns a transport per plugin, so the only available row
  would carry a fabricated endpoint and a status synthesised from a boolean, and
  per-URL detail would mean parsing credential-bearing URLs. Making a failed
  notification visible is a real gap, but the fix is a warning on the report and
  an ordering change, not a service row.
- **Docling's model weights.** Fetched by `huggingface_hub` inside Docling's own
  loader, inside `asyncio.to_thread` — three layers at which this repo owns no
  transport. `PIPELINE_PDF_ARTIFACTS_PATH` exists so they are not fetched during
  a run at all.
- **`scripts/`.** `SERVICES` describes what a pipeline _run_ talked to;
  `scripts/fetch_cytobands.py` is its own process and builds no run report for a
  row to reach.

`tests/pipeline/test_api_inventory_wiring.py` keeps that list honest from both
ends. One walk reads every URL literal under `pipeline/` out of the AST and
fails on a host `resolve_service` cannot name — held out only through
`_UNREGISTERED_BY_DESIGN`, which is declared empty. The other fails on any
module but `http_client.py` constructing its own `httpx.AsyncClient`, which is
the way a provider drops out of the inventory that no URL can show:
`export/geocode.py` called ClinicalTrials.gov — a registered host — on a client
of its own, so every host it touched was named and not one of its calls was
recorded.

### The refreshes record their own runs

`build_run_report` is reachable only from inside `run_pipeline`, so for a long
while **no published report could contain a ClinVar, Orphadata, Open Targets,
UniProt or ClinicalTrials.gov row at all**: the three sync modes go through
`_run_summary_pipeline`, which returned a summary dict and wrote no
`pipeline_runs` row, and everything their clients recorded was dropped when the
process ended. `pipeline/sync_report.py` and migration 011's `sync_runs` table
are what publish it.

- **`_run_summary_pipeline` is the single instrumentation point**, because all
  three modes already pass through it and it already knows the mode name.
  `--clinical-trials` was fixed by the same edit without being mentioned in it.
- **`reset_recorder()` there is load-bearing and not obvious.** The recorder is
  module-level and reset only at the top of `run_pipeline`, so without it a
  `--pubmed --sync-annotations` invocation would publish the PubMed run's rows
  as the refresh's own. It is safe because `_finalize_run` snapshots the
  recorder _inside_ `run_pipeline`, before the dispatcher reaches the syncs —
  which is also why a refresh can never pollute the run report in the other
  direction.
- **The record is stored in its own table, and that is not tidiness.**
  `PipelineRunReport` requires three fields and defaults the rest, so a refresh
  document written to `pipeline_runs.report` would _validate_ in
  `read_pipeline_run` rather than be rejected, and the About page would publish
  a zero-filled PubMed run over the real one — no exception, no log line. And
  `read_pipeline_status` dates the whole dataset from that table's newest
  non-failed row, so the refresh would also re-date the dashboard. Sharing the
  table would put a `WHERE run_mode = 'standard'` clause in two readers that
  each swallow their own errors; a separate table removes the mistake instead of
  fencing it.
- **A refresh derives its own status.** `derive_status([])` returns `completed`,
  so reusing it on a step-less document would publish a green badge over a
  refresh that failed outright. `derive_sync_status` reads the summary's own
  status and the per-source failure counts instead; a per-source failure is
  amber, not red, because the sync carried on and wrote what it could.

  **That amber branch was unreachable until `aborted` existed.** Every sync
  appends one error string for each gene, ORPHAcode or study it could not fetch,
  and both `_result_summary` and `derive_sync_status` read `failed` off a
  non-empty `errors` list — so ClinVar timing out on 1 of 63 genes published a
  red badge over a refresh that wrote the other 62, exited the process 1 and
  logged "Exporting despite failures". The two facts are now separate: `errors`
  is per item and turns the badge amber, while `SyncResult.aborted` (and
  `ExternalDataSyncResult` / `AnnotationSyncResult`) is set only where a sync
  **stops** — the 3600 s timeout, a CTG search that raised, a CTG upsert that
  wrote none of its records — and is the only thing `_result_summary` reports as
  `failed`. An exception escaping the sync is the other red path, through
  `_failure_summary`. A summary with per-item errors and no abort reports
  `status: "warnings"`, which is neither the exit code's `failed` nor a silent
  `ok`.
- A `skipped` clinical-trials summary (`ct_enabled=False`) records **no row**:
  it made no calls and changed nothing, which is the reasoning that also keeps
  the offline run modes out of `pipeline_runs`. The write is otherwise
  best-effort and swallowed on failure, as `_record_failed_run` is — the
  database is often the thing that just failed, and raising there would replace
  the refresh's real error on its way up.

### Two accounting decisions this surfaced

- **Insert-floor rejections are counted, and counted once.** A held gene passes
  validation, so it is in `papers_detail[*].genes`; it is refused at the merge,
  so it is in `held`. It was published in _both_ `acceptedGenes` and
  `rejectedGenes` — two answers to the same question — and
  `rejectedAtInsertFloor` was rendered nowhere, so nothing on the page
  reconciled them. Accepted now means what reached the database, and the holds
  are their own stat. `_rejected_records` also lists them **first**: the list
  caps at `DETAIL_CAP`, holds are always few and validation rejections are many,
  so appending them last truncated the whole category away on a large run while
  still publishing its count.
- **`papers.noTextAvailable` is its own count, not part of `failed`.** A paper
  with no retrievable text is a stable fact rather than a transient failure, so
  all three paths now record it as _processed_ with `source: "none"` — which is
  what stops the pipeline retrying that paper on every future run. The offline
  PMID path used to call it an error, so `papers.failed` meant two different
  things depending on which mode had run; it now agrees with the other two, and
  `noTextAvailable` counts the condition.

  That rule rests on retrieval telling the two apart, and two places did not.
  `pdf_retrieval.fetch_abstract` is the last resort, so unlike the sources
  before it, which swallow their own failures and fall through, it raises
  `RetrievalError` on a non-200 or a transport failure: a 429 on the abstract
  call used to read as "no text", and the paper went into `pubmed_refs` for
  good. `validation` raises `NcbiUnavailableError` the same way -- a lookup NCBI
  did not answer is not a gene it does not know, and `None` for both meant one
  timeout on `NOTCH3` was cached as "not found" for the rest of the run.
  `_validate_genes` re-raises it so the _paper_ fails and is retried, rather
  than being recorded as processed with the gene missing; the batch path catches
  it per paper.

The offline `--pmids` and `--local-pdfs` modes write no `pipeline_runs` row, and
that is correct rather than an oversight: they publish no data, so recording one
would point the About page's "up-to-date as of" date at a run that changed
nothing. `RUN_MODES` in `pipeline/steps.py` is the vocabulary the column
accepts, declared as a constant because the encoding contract test reads it — it
used to parse `run_mode: One of '…'` out of a docstring, and rewording that
sentence silently broke the check.

### Quote provenance reaches the report

`_report_provenance` computed how many quotes were verbatim in the paper and how
many matched an API citation span, logged both, and discarded them — so the
dashboard could show a gene's quote without being able to say how many checked
out. `citations.ProvenanceTally` accumulates them for the run.

It is a module-level tally reset per run, like the API recorder, rather than a
return value threaded through `extract_from_paper` and `_extract_and_validate`:
those are the hottest and most carefully tested signatures here, and this is a
measurement none of them acts on.

**The two numbers are published as two numbers.** `quotesVerbatim` asks whether
the sentence is in the paper, of every gene. `quotesCited` asks whether the API
attested to that span — the stronger claim, but bounded by how much prose the
model wrote rather than by quote quality, which is why it sits at 98/124 while
verbatim sits at 122/124. One averaged rate would report a quality figure the
second number cannot carry.

**A failed paper's quotes are un-counted, by the rule its genes already
follow.** `report_provenance` runs inside extraction, before NCBI validation, so
the counts are in the tally by the time `_validate_genes` re-raises an
`NcbiUnavailableError` and the paper is recorded as failed. `genes_extracted` is
incremented only once validation held, and folding the tally on the same terms
was missed: a run of two papers where the second yielded 5 genes and then failed
published `genes.extracted: 2` beside `quotesChecked: 7`, so the About page read
"7 of 7 quotes found in the paper" under a funnel of 2 genes — and the run that
retried the paper counted the same 7 again. `ProvenanceTally.discard(pmid)`
subtracts the paper's `by_paper` share, and both concurrent paths call it in the
branch that already folds only the token usage (`run_one` in
`process_papers_concurrently`, `validate_one` in
`_process_new_pmids_via_batch`). A subtraction rather than a fold-on-success
because the tally is also filled from paths that never enter `paper_scope` — the
offline `--pmids` and `--local-pdfs` modes — where nothing would ever fold it
in.

## Retrieval

**The query's recall is measured by a harness that knows no disease.**
`pipeline/query_eval.py` reads `disease/recall_gold.csv` (`pmid,note`, one paper
the query is expected to retrieve per row), unions it with the PMIDs
`data/table1.json` cites and, when the gitignored gold-standard CSV is present,
the golden suite's papers, and intersects `SVD_QUERY` with a `[uid]` disjunction
over the set — exact, and never near the 9,999-record `esearch` cap because the
result can never exceed the gold set. `scripts.measure_recall` runs it (see the
root `CLAUDE.md`); the baseline it writes carries the query's sha256, so it says
which query it measured, and the replay test skips, with a reason, when the gold
file is empty or no baseline is recorded — which is where a fork starts. The
cSVD-specific recall suite, with its five pinned misses and the per-branch
figures, is `tests/pipeline/csvd/test_query_recall.py`.

The traps in what reaches the model -- esearch paging, PMC's "does not allow
downloading" notice, DOIs read from the wrong list, JATS tables and
floats-groups, back-matter headings, Unpaywall preprints, Docling
`PARTIAL_SUCCESS` -- and what each looks like in the run log are in the
`debug-paper-retrieval` skill (`.claude/skills/debug-paper-retrieval/SKILL.md`).

## PDF fallback

`pipeline/pdf_parse.py` is the only module that imports Docling, and every
runtime import inside it is local — not an optionality guard (Docling is a core
dependency) but a startup-cost one, keeping torch out of the import graph the
argcomplete fast path at the top of `main.py` exists to protect. Its annotations
name Docling's classes through `TYPE_CHECKING`, which deferred annotations never
evaluate.

- **OCR is a fallback, not a mode.** It is on by default (`PIPELINE_PDF_OCR`),
  and `PDF_AWARE_LAYOUT_REGIONS` plus `PDF_FIRST` priority mean it never alters
  a born-digital paper's output -- it is the whole reason a scanned one yields
  anything. The measurements are the module docstring of
  `pipeline/pdf_parse.py`.
- **Threads default to performance cores, not `os.cpu_count()`.**
  `default_pdf_threads()` in `pipeline/config.py` reads
  `hw.perflevel0.logicalcpu` on macOS; its docstring has the numbers. Larger
  Docling batch sizes regress on unified memory too.
- **The engine is pinned, never `OcrAutoOptions()`.** That default probes the
  environment in an order Docling may reorder on upgrade, and on a total miss
  logs `"No OCR engine found"` once and then emits no OCR text at all -- a hole
  no exception marks. `RapidOcrOptions(backend="onnxruntime", lang=["en"])`.
- **CoreML is a loss here** (PP-OCR's dynamic input shapes fall back to CPU
  after paying conversion); `PIPELINE_PDF_OCR_COREML` keeps the knob, off.
- **`page_range` truncates; `max_num_pages` rejects.** Docling raises
  `ConversionError` and returns nothing at all for a document over
  `max_num_pages`, so it is the wrong guard: a long supplement would yield no
  text whatsoever. `page_range=(1, pdf_max_pages)` keeps the body, which is
  where gene symbols are named.
- **A failed Docling import travels all the way out.** `parse_pdf_bytes`
  re-raises `ImportError` from `_convert` deliberately -- Docling is a core
  dependency, so this is a broken install, not a bad PDF -- and both callers
  used to catch bare `Exception` around it, which made the invariant unreachable
  from any run mode. On the network path `download_and_parse_pdf` logged one
  warning per paper and returned `None`, so every Unpaywall paper in the run
  fell through to its abstract and was retired in `pubmed_refs` with
  `source: "abstract"`, under a `completed` report whose low `fulltextRetrieved`
  reads like an ordinary short window. Both callers now re-raise it: on the
  network path `process_paper_safe` records the paper as failed, so it is never
  written to `pubmed_refs` and the next run retries it; on `--local-pdfs`
  `_process_local_pdf_file` lets it out of the run entirely, because every file
  in the directory would fail the same way and one error apiece would report a
  broken environment as a corpus of unreadable papers. An `ImportError` raised
  later, inside `export_to_html`, is still handled as a bad-PDF failure --
  Docling imported fine there.

**On Python 3.15 the startup imports are lazy, and the tests see them
resolved.** `__lazy_modules__` in `main.py` and `config.py` (PEP 810; 3.14
ignores the name) took `import pipeline.main` from 994 ms to 165 ms, most of it
the SDK `config.py` imported for `transform_schema` alone. A module only needs
listing to be lazy, but one used at import time — a module-level constant, a
dataclass default — loads eagerly regardless. `_resolve_lazy_imports` in
`tests/pipeline/conftest.py` binds every lazy name before the first test:
`mock.patch` reads a still-lazy name as its proxy and builds a `MagicMock` where
the function is async, which failed four tests on 3.15 before it existed.
Docling's imports stay local rather than lazy, because 3.14 would import a
module-scope one eagerly.

### Machine-fetched annotations

`--sync-annotations` fetches disease, ontology and identity annotations for the
curated genes from ClinVar, Orphadata and Open Targets into `gene_annotations`
and never writes `genes`. Running or debugging it -- the ClinVar-then-Orphadata
dependency order, the delete-before-insert trap and the 30-day status rows that
stand in the way of a re-sync, `_LOOKUP_ALIASES` for `COL4A1/2` and `C6orf195`
-- is the `sync-annotations` skill (`.claude/skills/sync-annotations/SKILL.md`).

**NCBI limits requests per second, and a semaphore does not.**
`clinvar_rate_limit` bounds concurrency; E-utilities bounds rate — 10 a second
with an API key, counted across every endpoint. Ten in flight over calls that
return in 30 ms is roughly 300 a second, and the first full 63-gene run put
every request out inside half a second and was answered 429 on 53 of them.
`_throttle` spaces request _starts_ instead, and `_send_throttled` retries a 429
three times over a widening delay, because the limit is per API key and another
process on the same key can still bounce a correctly-spaced request. Concurrency
and rate are different quantities; do not "simplify" the pacing back into the
semaphore.

The E-utilities callers share the same rule through `pipeline/ncbi_http.py`:
gene validation, gene info, citations, the PMC and abstract fetches and the
metadata lookup all go through `get_with_retry`, which paces starts on one
module-level clock (10/s with a key, 3/s without) and retries a 429. Only
validation had it; the others each had a semaphore and nothing else, and a burst
of papers put every one of their requests out at once.
`PIPELINE_MAX_RATE_LIMIT_RETRIES` counts _retries_ there, as it does in the
Anthropic client: N retries is N+1 requests and zero is still one. The loop used
to count attempts, so NCBI got one request fewer than Anthropic for the same
setting, and a setting of 1 gave it no retry at all.

**`--sync-annotations` is deliberately a separate entry point from
`--sync-external-data`.** The existing sync feeds the NCBI/UniProt/PubMed caches
every consumer already reads; folding four more APIs into it would make every
existing run several times longer for data nothing reads yet.
`deno task geocode` and `deno task cytobands` are the house precedent for a
deliberate, separately-invoked refresh. It is a full mode rather than a bare
call — a `run_*` wrapper returning a summary dict plus a `_run_summary_pipeline`
dispatch — because that list is what drives the process exit code and the run
record, **and it is named in `online_modes`**: `_prepare_cli_args` turns on
`--pubmed` whenever no mode is selected, so a flag missing from that tuple does
not merely fail to run, it silently runs the LLM extraction instead.

### A discovered gene needs a band, and the cache must admit it lacks one

Extraction is not asked for a chromosomal location and nothing else in the
schema carried one, so the sixteen genes the year-long run inserted published as
`(unknown)` and reached no chromosome of the phenogram. NCBI states
`maplocation` ("17q25.1") in the same esummary `_fetch_gene_summary` already
read `description` and `otheraliases` out of. Migration 012 caches it, and
`fill_missing_chromosomal_locations` copies it into `genes` **only where the
column is empty**, so curated rows keep the source spreadsheet's spelling.

**A cached row written before a column existed is not a complete answer.** The
fix filled 3 of 16 on its first run: the other 13 had `ncbi_gene_info` rows
whose `map_location` was NULL, and a cache hit skips the fetch, so they would
have stayed unplaceable for as long as the row stayed fresh.
`get_cached_ncbi_genes` now withholds a NULL `map_location` the way it withholds
a stale row -- NULL means "written before migration 012", an empty string means
"fetched, NCBI states no band" -- so each row heals exactly once rather than
re-fetching forever. Any column added to a lookup cache later needs the same
treatment.

### ClinicalTrials.gov rows must read like the curated ones

`--clinical-trials` writes beside the curated Table 2 rows under the same filter
vocabulary. The column-spelling rules (`M/YYYY` dates, comparator arms,
`sponsor_type`), the registry-id upsert key, the `overallStatus` denylist, the
order of `deno task data` and `deno task geocode`, and the pinned downstream
counts a removed trial moves are in the `sync-clinical-trials` skill
(`.claude/skills/sync-clinical-trials/SKILL.md`).

### The discovery is gated on a stated cSVD condition

`query.cond` expands into CT.gov's concept graph and nothing narrowed it again:
the only filter was `DRUG_INTERVENTION_TYPES`. Ten terms return 1,423 studies
whose most common stated condition is **Fabry disease** (215), beside cancer,
ANCA vasculitis, Parkinson's and MS -- 594 rows in a curator's queue, Ebola
vaccine trials among them. `is_disease_study` keeps a study only if a condition
it _states_ names a cSVD entity. `_CONDITIONS` is that vocabulary and
deliberately excludes the systemic diseases that _cause_ cSVD, because the
curated table has never held a Fabry or mitochondrial trial. MeSH inverts the
phrase, so "Dementia, Vascular" is matched as a co-occurrence inside **one**
condition string -- across two, "Cardiovascular Diseases" beside "Cognitive
Decline" would qualify. It drops 1,045 of 1,423 and none of the eight curated
NCT trials; validate any change to the vocabulary that way before shipping it.

**`ct_max_retries` is 6, and the difference is most of the registry.** CTG
throttles a paging burst hard enough that four attempts over ~7s give up
mid-term: two live syncs each truncated three of the ten terms at their first
page and fetched 382 studies. Six truncates no term and fetches 1,423. CTG
states no `Retry-After` -- the header path in `_fetch_page_with_retry` has never
fired -- so the curve is what paces this client.

**`trial_name` and `primary_outcome` are curator-owned once `target_population`
is filled in.** Refreshing them API-first replaced curator prose with registry
verbatim on five of the eight curated NCT trials the first time the sync ran. A
discovery inserts on the run that finds it and _refreshes_ on every run after,
so "refreshed" stops meaning "curated" the second time -- the same proxy made
`_unplaceable_phases` report 35 uncurated rows as publishing in Table 2. Gate on
`target_population` -- `TrialUpsertResult.curated_ids` is the set that carries
it -- never on refresh status.

## Database tests

**No test may reach the production database.** `_isolate_credentials` in
`tests/pipeline/conftest.py` clears the `DB_*` variables for exactly that
reason; see "Commands" in the root `CLAUDE.md`.

A test that wants real SQL asks for the `database_env` fixture, which brings up
**`dhi.io/postgres:18-alpine3.23`** as a container named `csvd-pg-pytest`,
migrated to head and destroyed with the session. No volume is mounted; nothing
survives the run.

It is **not** the glibc `dhi.io/postgres:18.6` the `regenerate-data` skill
documents, and that skill records why the two differ. What the glibc pin would
otherwise buy is kept by hand instead: no assertion in
`test_database_integration.py` depends on a text `ORDER BY`, and one added there
would compare musl's collation against production's glibc.

The runtime is **Apple's `container`**, not Docker: the fixture connects to the
container's own address on the `default` network rather than a published port,
so the production server on the host's 5432 is neither shadowed nor reachable.
The `regenerate-data` skill and `_container_address`'s docstring in
`conftest.py` carry the mechanism.

**`CSVD_TEST_DB_URL` comes first, and is what makes these tests run in CI.**
`container` is macOS-only and the `dhi.io` image needs a login CI does not have,
so on `ubuntu-latest` the whole file used to skip and every green PR merged with
no database having applied the migrations. The python job now runs a
`postgres:18` service and points this variable at it
(`postgresql://user:password@host:port/database`); the fixture parses it into
the same `DB_*` settings, migrates that database to head and uses it. A
malformed URL **raises** rather than skips -- setting it is a statement of
intent to run these tests, and a skip would report that intent as coverage.
Point it only at a throwaway database: it is migrated and written to.

Without it the fixture falls back to the container, and **skips** only when the
`container` CLI, its system service or the image is unavailable too.

`tests/pipeline/test_database_integration.py` is what uses it, and it covers
what a mocked connection cannot: that Alembic reaches a single head, that the
report survives the `JSON` column and `read_pipeline_run`'s re-validation, that
an ISO timestamp string becomes a real `timestamptz`, and **what the gene-merge
CTE computes** -- ordinals continuing off the pre-statement `MAX`, `btrim`
collapsing `"1 "` onto `"1"`, a re-run writing nothing, and `source_quote` and
`confidence` filling as one pair. `test_database.py` pins the spelling of those
clauses against the statement asyncpg is handed, which a mutation can satisfy
from inside an SQL comment; dropping `AND existing.pmid = incoming.val` from the
NOT EXISTS keeps all seven of its assertions passing and stops the merge
appending anything to a gene that already has one row. Every other database test
mocks `Database.connection`, which is how a duplicate `008` revision went
unnoticed -- `009_add_run_report` was applied to no database any test could see.

## What is not proven

Retrieval recall and extraction recall are both measured and pinned.
`tests/pipeline/csvd/test_query_recall.py` intersects `SVD_QUERY` with a gold
set of 178 PMIDs, every one the dashboard cites among them (173 retrieved; the
five misses and the reason for each are in `_MISSED`, and the anchor-only and
MeSH-branch figures are pinned so widening a term list in the belief that it
helps recall fails loudly). `tests/pipeline/csvd/test_extraction_golden.py`
replays ten cassettes and asserts recall over the gold genes a paper's retrieved
text actually names, reported apart for the 13 gold genes the prompt itself
names -- **quote the clean 81%, not the pooled 85%**. Its `_RECALL_BASELINE` is
raised when the prompt is widened, never to turn a red run green; three floors
came down with the move to Opus 5.5, to what that re-record measured, and the
module records the genes they lost. Every one of those was a gene Opus 5
reported below the update floor, bar one: scored after the floor, Opus 5 and
Opus 5.5 recover 38/46 and 39/46. What neither proves:

- **Precision, anywhere.** The gold standard is a curated table, not a per-paper
  answer key, so a gene absent from it is unreviewed rather than wrong (the
  recorded cassette holds 43 genes against 17 gold rows on PMID 37069360,
  missing the one not in the retrieved text and FOXF2, which that text names
  only in a GWAS table row); precision is reported, never asserted. The MeSH
  branch of `SVD_QUERY` is likewise a volume argument, and nothing says what
  fraction of the extra papers a year is signal.
- **The Docling PDF path.** No golden fixture takes it -- all 22 gold PMIDs
  resolve through Europe PMC or an abstract.
- **Negative cases.** Every gold row reports at least one gene, and inventing
  one is an editorial act.
- **What survives the gates.** `extract_from_paper` is pre-gate, so the harness
  measures raw extraction, not what clears the confidence floors and NCBI
  validation.

**The confidence floor is two numbers.** `confidence_threshold_update` (0.45)
and `confidence_threshold_insert` (0.65) decide two things with opposite risk
profiles: adding a reference to a gene already in the table is cheap, reversible
and visible in `git diff data/`, while creating a _new_ row is a scientific
claim in a published dataset. The update floor was swept post-hoc over the
committed cassettes; 0.45 is the highest floor that still recovers every
reachable gold gene the model finds, and the old single 0.65 sat on the mode of
the confidence distribution (30 extractions score exactly 0.60) and discarded
seven of them. `test_the_update_floor_is_set_where_gold_recall_saturates` in
`tests/pipeline/csvd/test_extraction_golden.py` prints the whole sweep when it
fails. **The insert floor is deliberately not swept**, and stays at 0.65 as a
curation policy: the gold standard cannot tell an incorrect new gene from an
unreviewed one, so a number tuned against it would look measured without being
so -- the confound that keeps precision unasserted above.

Three consequences of the split are load-bearing. **The permissive floor buys a
citation, and nothing else**: `_citation_only` in `pipeline/data_merger.py`
empties `mendelian_randomization`, the trait list and the omics list on any
update below the insert floor, because all three are OR-accumulated or
append-only and no later run can withdraw them; the PMID, `source_quote` and
`confidence` still land. **The gate runs during validation**, before the merge
learns which genes are new, so the permissive floor stays at the gate and the
strict one is applied at the insert/update split in `data_merger.py`; a gene
held there loses the PMID that proposed it (accepted rather than fixed --
`gene_references` has no row to hang it on), is listed first in `rejectedGenes`
as "Held below the insert floor", and raises a `merging_database` warning. And
**the row publishes the entry the floor was decided on**: `deciding_entry`
supplies both `confidence` and `source_quote`, so the number a reader checks
against the floor and the evidence beside it are one extraction.

## Long windows

The 2026-11-03 `--days-back 730` run under Opus 5.5 was 1,570 papers, ~4 hours
and $116 of extraction (about $0.074 a paper, refusals included, since a
pre-output refusal is billed), and `pipeline/checkpoint.py` is what makes a
crash at hour two survivable. `--test-mode` gives a window's count for free;
multiply before starting, and check the API credit balance covers it with room
to spare -- see the `run-long-window` skill for what an exhausted balance does.
The resume semantics, the `--batch` exception and `scripts.backfill_pubmed` are
in "Commands" in the root `CLAUDE.md`; the checkpoint's own invariants (the
fingerprint, the `flock`, the request-order merge) are in the `run-long-window`
skill (`.claude/skills/run-long-window/SKILL.md`).

`scripts/backfill_pubmed.py` is the other half, optional rather than the
default: cumulative chunks committed as each finishes, at the cost of narrowing
`_run_batch_validation` from the year to one chunk, which is why the checkpoint
and not chunking is the answer to a crash. Its module docstring and the
`run-long-window` skill carry the command and the trade.

**Both preview flags deduplicate.** They used to skip it, which made the preview
wrong in both directions: `--test-mode` reported every paper in the window as
new, and `--dry-run` -- documented as "skip database writes" -- re-extracted
papers already in `pubmed_refs` at full price, so the cheap rehearsal cost
exactly what the real run costs. Reading `pubmed_refs` is not a write. A preview
still has to work with no database at all, so `_PREVIEW_DB_FAILURES` is
tolerated there and re-raised on a live run, where swallowing it would silently
reprocess the whole window. "No database at all" includes no `DB_*` variables --
a fresh clone, or CI -- which raises `DatabaseConfigError` before any socket is
opened, so it is in the tuple beside `OSError` and `PostgresError`.
`--test-mode` is the free way to count what a window would actually process
before paying for it.

**`estimated_tokens_per_call` is a reservation, not a measurement**, and it is
the throughput ceiling: raise it and `tpm_limit` together, or neither. The field
comment in `config.py` records the measurement behind the figure, and
`tests/pipeline/test_config.py` pins the product against the limit.

## Known data limitation

A short `--days-back` window is mostly an abstract-only run: papers added to
PubMed in the last week are largely not yet in Europe PMC's open-access set, so
`fulltextRetrieved` runs low. The window is over the Entrez date (`edat`), the
day the record entered PubMed, not the publication date: a paper indexed a
fortnight after it was published falls in the `edat` window of the run that
follows, while under `pdat` it fell between two windows and was never seen. That
is what the number means, not a fault to fix.

In a short window most full text comes from PMC rather than Europe PMC, because
Europe PMC indexes the OA subset days later. Nine `source: "pmc"` papers with
zero genes in one run were re-processed on suspicion of the "does not allow
downloading" notice and all nine were real articles that name no cSVD gene -- a
`pmc` source with zero genes is not evidence of the stub. The guard's log line
("PMC holds this article but does not serve its full text") is.

A trial's population is decided from its registry, not from the curated row:
`https://clinicaltrials.gov/api/v2/studies/<NCT>` and
`https://www.isrctn.com/api/query/format/default?q=<ISRCTN>` answer fetches;
ANZCTR (403) and ChiCTR (empty page) do not, and their two trials were placed
from their titles.

Alembic stays the only definition of the schema. Atlas was evaluated for that
role and rejected: it gates PostgreSQL triggers and functions behind its Pro
plan, and this schema has five triggers and a plpgsql function, so its diff
engine would plan against an incomplete picture. The maintained Python
alternatives are dead ends (`yoyo-migrations` last released August 2024, `migra`
2022).

`genes.source_quote` (migration 004) and `genes.confidence` (migration 010) are
both published now. There is no named hold-out set; `_publishable` drops only
`id` / `created_at` / `updated_at`. A `genes` column is held out by leaving its
name out of `_GENE_COLUMNS`, which is the projection `_gene_select()` builds, so
an unlisted column is never even read; `clinical_trials` is still read with
`SELECT *`, so holding a column out of **that** table needs a projection first.
A missing quote publishes the `"(not yet extracted)"` sentinel
(`clean_gene_row`, `fill_missing_text`) rather than a null-valued key on a
string-typed field -- the same reasoning as `"(reference needed)"`. A missing
confidence publishes JSON `null`: it is a nullable number, not a sentineled
string, and the two are never conflated in `lib/types.ts`. Both surface in
`islands/GenesView.tsx` as their own columns under an "Extraction Provenance"
header group, following the established pattern for a prose field in this table
(`Trial Name` / `Primary Outcome` in `TrialsView.tsx`: plain text, no tooltip,
wraps) rather than the tooltip-on-hover the plan first proposed -- that would
have needed a truncation affordance this table has never had, and no other
column here hides its value behind an icon.

**The two columns are filled as one pair, or not at all.** A confidence scores
the sentence it was extracted with, so `merge_genes_transactional` guards both
on the same predicate -- the stored quote being absent and this extraction
carrying one -- in the `UPDATE` and in the `ON CONFLICT` branch alike. Filling
them independently (two `COALESCE`s, as they were) would put the next paper's
score beside this paper's sentence the first time a run touched a row that
carries a quote and no confidence: a number describing an extraction the reader
cannot see. The consequence is that such a row is **not** backfilled by a run;
its score has to be read off the log of the run that produced its quote and
written by hand, or left null.

`NOTCH3` and `COL4A1/2` were the two such rows, and on 2026-09-02 they were
resolved differently, which is the rule working. `NOTCH3` was set to 0.9 by
hand, because run 7's stored report carried that score for PMID 42650130 beside
the byte-identical quote; `COL4A1/2` was left null, because the run that quoted
PMID 42437605 had been pruned and re-extracting the paper would have put a
_different_ extraction's number beside that sentence. Neither stands now. On
2026-11-03 the table was reset to its curated 31 August baseline, which carries
no quote and no confidence (`scripts/reset_to_baseline.py` refuses a baseline
row that does), and the 730-day re-extraction under Opus 5.5 filled each pair
from one extraction: `NOTCH3` with PMID 41895274's sentence at 1.0, `COL4A1/2`
with PMID 42607872's at 0.95. No pair in the table was written by hand.
`tests/pipeline/test_database_integration.py` merges the pair both ways round
against a real database.

**A gene the pipeline admits carries two placeholders, and the export publishes
neither as a fact.** `_build_combined_gene_data` stores `chromosomal_location`
as `""` -- nothing in `pipeline/` derives a cytogenetic band, the curators write
it -- and stores the gene symbol in `protein` when no paper named a protein.
`clean_gene_row` fills the first with `"(unknown)"`, because a string-typed,
sentinel-typed column publishing JSON `null` fails `tests/data_contract_test.ts`
and `lib/data/genes.ts` would rewrite it to `"(unknown)"` at runtime anyway,
hiding the gap instead of reporting it; the phenogram then lists the gene as
unplaced rather than guessing a band. It maps the second to `"(unknown)"` too,
on **exact** equality with the gene symbol -- the same test both write paths in
`pipeline/database.py` use to decide the protein is still missing, and exact
because `EphB4` for `EPHB4` and `Epo` for `EPO` are real curated names that a
case-insensitive rule would erase.

**The export's readers tolerate migration lag and nothing else.** Each of
`read_pipeline_status`, `read_pipeline_run`, `read_sync_runs` and
`read_disease_annotations` catches only `asyncpg.UndefinedTableError` /
`UndefinedColumnError` -- the shapes of "this revision does not have it yet" --
and lets every other failure, plus any stored document that fails re-validation,
propagate. `run_export` stages these four values before `publish_atomically`, so
a swallowed error was not a degraded read: it published `null` over
`pipeline_run.json`, `[]` over `pipeline_syncs.json`, or froze
`gene_annotations.json` at its last good read, and `--export` still exited 0. A
raised error aborts before the publish, which leaves every committed file
exactly as it was and is folded into the exit code by `_publish_export`.

**`genes.references` was corrupted upstream for ten rows by a spreadsheet
round-trip, and all ten are recovered** -- each mangled cell has exactly 15
significant digits, float64's guarantee, so only the second PMID's last digit
was lost, and enumerating the ten candidates against NCBI left one cSVD paper
each time. **Do not read a lost digit as a zero**: the naive readings
(`39114920`, `39805840`) are real papers on other subjects. `extract_pmids`
still refuses a mangled cell outright, so a mangled value arriving from anywhere
else shows as `(reference needed)` rather than as fabricated citations.

**`clinical_trials.svd_population` and `svd_population_details` were misaligned
for every row, and are re-curated.** Upstream, the two columns had been sorted
on their own and laid back over rows ordered by drug: the published column was
byte-for-byte its own alphabetical sort, three registry IDs disagreed with
themselves across their own drug rows, and the CADASIL cerebrolysin trial was
published as CAA while the ALN-APP CAA trial read as lacunar stroke. The 16
(population, details) pairs were the right pairs in the wrong rows -- each
reassigns to exactly one trial from the registry's own condition and eligibility
text, with none left over -- so the correction on 2026-09-01 moved values rather
than inventing any, and the per-population totals did not change.
`tests/data_contract_test.ts` now fails if the rows of one registry ID disagree
on any per-trial column, which is the shape this damage takes and the export
cannot see.
