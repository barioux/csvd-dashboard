# Pipeline tests

These suites are covered by the pipeline notes, imported here so they load when
working under `tests/pipeline/`.

`csvd/` holds the tests that name cSVD content -- the 173/178 query recall and
its cassettes, the golden extraction suite and its gold standard, the prompt's
byte identity with the v6 literals, the manifest's cSVD values -- and a fork
deletes the whole tree (see "The disease seam" in the root `CLAUDE.md`).
Everything outside it holds for any disease and passes on
`deno task data:empty`; a test that reads `[0]` of a committed file takes the
`committed_rows` fixture, which skips with the file's name when it is empty.

Two inputs of `csvd/test_extraction_golden.py` are gitignored because they are
publisher text: the seven full-text fixtures under `csvd/fixtures/papers/` and
the cassettes under `csvd/cassettes/test_extraction_golden/`, which embed them.
`uv run python -m scripts.fetch_paper_fixtures` writes the fixtures from Europe
PMC and checks them against the tracked `csvd/fixtures/papers/manifest.json`;
the cassettes are recorded locally per that module's docstring. Without them the
golden suite skips -- `pytest_collection_modifyitems` in `csvd/conftest.py`
turns the `paper_fixtures` and `golden_cassettes` markers into skips that name
what to run -- so CI never runs it, and a green CI says nothing about extraction
recall.

@../../pipeline/CLAUDE.md
