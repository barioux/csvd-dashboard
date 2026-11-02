"""Tests for pipeline.uniprot_fetch — UniProt protein information fetching."""

from unittest.mock import AsyncMock

import httpx

from pipeline.uniprot_fetch import (
    SyncResult,
    UniProtInfo,
    _clean_go_term,
    _fetch_uniprot_accession_status,
    _parse_search_rows,
    clear_uniprot_cache,
    close_uniprot_client,
    fetch_uniprot_batch,
    fetch_uniprot_go_info,
    fetch_uniprot_info,
    sync_uniprot_info,
)

# ---------------------------------------------------------------------------
# _clean_go_term
# ---------------------------------------------------------------------------


class TestCleanGoTerm:
    def test_none_returns_none(self):
        assert _clean_go_term(None) is None

    def test_empty_string_returns_none(self):
        assert _clean_go_term("") is None

    def test_removes_go_id(self):
        result = _clean_go_term("apoptotic process [GO:0006915]")
        assert result == "apoptotic process"

    def test_multiple_go_ids(self):
        result = _clean_go_term("process A [GO:0000001]; process B [GO:0000002]")
        assert result is not None
        assert "[GO:" not in result
        assert "process A" in result
        assert "process B" in result

    def test_cleans_double_semicolons(self):
        result = _clean_go_term("term1 [GO:0000001];; term2")
        assert result is not None
        assert ";;" not in result

    def test_cleans_extra_whitespace(self):
        result = _clean_go_term("  term   with   spaces  ")
        assert result == "term with spaces"

    def test_whitespace_only_returns_none(self):
        assert _clean_go_term("   ") is None


# ---------------------------------------------------------------------------
# clear_uniprot_cache
# ---------------------------------------------------------------------------


class TestClearCache:
    def test_clears_cache(self):
        import pipeline.uniprot_fetch as mod

        mod._uniprot_cache["TEST"] = UniProtInfo(
            "TEST", "P00000", None, None, None, None, None
        )
        assert "TEST" in mod._uniprot_cache
        clear_uniprot_cache()
        assert mod._uniprot_cache == {}


# ---------------------------------------------------------------------------
# _fetch_uniprot_accession_status
# ---------------------------------------------------------------------------


class TestFetchUniProtAccessionStatus:
    async def test_exact_match_found(self, mocker):
        tsv = (
            "Entry\tGene Names (primary)\tGene Names (synonym)\tProtein names\n"
            "P12345\tNOTCH3\t\tNotch receptor 3\n"
        )
        resp = httpx.Response(200, text=tsv)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp)
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get",
            return_value=mock_client,
        )

        accession, protein, cacheable = await _fetch_uniprot_accession_status("NOTCH3")
        assert accession == "P12345"
        assert protein == "Notch receptor 3"
        assert cacheable is True

    async def test_falls_back_to_synonym_search(self, mocker):
        no_results = httpx.Response(200, text="Entry\tGene Names (primary)\n")
        synonym_tsv = (
            "Entry\tGene Names (primary)\tGene Names (synonym)\tProtein names\n"
            "Q99999\tNOTCH3\tALIAS\tNotch 3\n"
        )
        synonym_resp = httpx.Response(200, text=synonym_tsv)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[no_results, synonym_resp])
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get",
            return_value=mock_client,
        )

        accession, protein, cacheable = await _fetch_uniprot_accession_status("NOTCH3")
        assert accession == "Q99999"
        assert cacheable is True

    async def test_rows_naming_no_match_fall_through_to_the_second_query(
        self, mocker
    ):
        """``gene_exact:`` answering about other genes is not an answer.

        It used to return on the first query that produced any row at all, so
        the broader ``gene:`` query never ran once the exact one had matched
        some other entry's tokens.
        """
        no_match = httpx.Response(
            200,
            text=(
                "Entry\tReviewed\tGene Names (primary)\t"
                "Gene Names (synonym)\tProtein names\n"
                "P11111\treviewed\tOTHER\t\tOther protein\n"
            ),
        )
        match = httpx.Response(
            200,
            text=(
                "Entry\tReviewed\tGene Names (primary)\t"
                "Gene Names (synonym)\tProtein names\n"
                "P22222\treviewed\tNOTCH3\t\tNotch 3\n"
            ),
        )
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[no_match, match])
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get",
            return_value=mock_client,
        )

        accession, protein, cacheable = await _fetch_uniprot_accession_status("NOTCH3")

        assert (accession, protein, cacheable) == ("P22222", "Notch 3", True)
        assert mock_client.get.await_count == 2

    async def test_neither_query_naming_the_symbol_is_a_cacheable_miss(
        self, mocker
    ):
        other_gene = httpx.Response(
            200,
            text=(
                "Entry\tReviewed\tGene Names (primary)\t"
                "Gene Names (synonym)\tProtein names\n"
                "P11111\treviewed\tOTHER\t\tOther protein\n"
            ),
        )
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=other_gene)
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get",
            return_value=mock_client,
        )

        assert await _fetch_uniprot_accession_status("NOTCH3") == (None, None, True)

    async def test_the_matching_row_is_taken_whatever_its_position(self, mocker):
        """The first row is not the answer; the row naming the gene is."""
        tsv = (
            "Entry\tGene Names (primary)\tGene Names (synonym)\tProtein names\n"
            "P11111\tOTHER\t\tOther protein\n"
            "P22222\tNOTCH3\t\tNotch 3\n"
        )
        resp = httpx.Response(200, text=tsv)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp)
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get",
            return_value=mock_client,
        )

        # NOTCH3 is in second row, so exact match returns it
        accession, protein, cacheable = await _fetch_uniprot_accession_status("NOTCH3")
        assert accession == "P22222"
        assert cacheable is True

    async def test_no_results_returns_none_none(self, mocker):
        no_results = httpx.Response(200, text="Entry\tGene Names (primary)\n")
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=no_results)
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get",
            return_value=mock_client,
        )

        accession, protein, cacheable = await _fetch_uniprot_accession_status(
            "FAKEGENE"
        )
        assert accession is None
        assert protein is None
        assert cacheable is True

    async def test_non_200_returns_none_none(self, mocker):
        resp = httpx.Response(500)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp)
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get",
            return_value=mock_client,
        )

        accession, protein, cacheable = await _fetch_uniprot_accession_status("NOTCH3")
        assert accession is None
        assert protein is None
        assert cacheable is False

    async def test_timeout_returns_none_none(self, mocker):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get",
            return_value=mock_client,
        )

        accession, protein, cacheable = await _fetch_uniprot_accession_status("NOTCH3")
        assert accession is None
        assert protein is None
        assert cacheable is False

    async def test_tsv_parsing_error_returns_none_none(self, mocker):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.RequestError("connection reset"))
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get",
            return_value=mock_client,
        )

        accession, protein, cacheable = await _fetch_uniprot_accession_status("NOTCH3")
        assert accession is None
        assert protein is None
        assert cacheable is False

    async def test_value_error_while_parsing_returns_transient_miss(self, mocker):
        response = httpx.Response(200, text="header\nrow")
        client = AsyncMock()
        client.get.return_value = response
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get", return_value=client
        )
        mocker.patch(
            "pipeline.uniprot_fetch._parse_search_rows",
            side_effect=ValueError("bad TSV"),
        )

        assert await _fetch_uniprot_accession_status("NOTCH3") == (
            None,
            None,
            False,
        )


_HEADER = (
    "Entry\tReviewed\tGene Names (primary)\t"
    "Gene Names (synonym)\tProtein names"
)


class TestParseSearchRows:
    def test_short_rows_are_skipped(self):
        lines = [
            "Entry\tGene Names (primary)\tGene Names (synonym)\tProtein names",
            "too-short",
        ]

        assert _parse_search_rows(lines, "NOTCH3") == (None, None)

    def test_a_row_naming_the_symbol_nowhere_is_not_this_genes_protein(self):
        """UniProt token-matches the gene-names field even under gene_exact:.

        Taking the first row on trust published another protein's accession,
        name and URL under the curated symbol -- and sync_uniprot_info files
        anything carrying an accession as a success.
        """
        lines = [
            _HEADER,
            "P1\treviewed\tOTHER1\t\tProtein 1",
            "P2\treviewed\tOTHER2\t\tProtein 2",
        ]

        assert _parse_search_rows(lines, "NOTCH3") == (None, None)

    def test_the_primary_gene_match_wins_outright(self):
        lines = [
            _HEADER,
            "P1\tunreviewed\tOTHER1\tNOTCH3\tProtein 1",
            "P2\treviewed\tNOTCH3\t\tNotch 3",
        ]

        assert _parse_search_rows(lines, "NOTCH3") == ("P2", "Notch 3")

    def test_the_primary_match_wins_over_an_earlier_reviewed_synonym(self):
        """UniProt can list another gene's reviewed entry first when it keeps
        the symbol as an old synonym -- live, a cystatin whose own entry came
        second. Returning that synonym row early published the other gene's
        accession, protein name and GO terms as this gene's."""
        lines = [
            _HEADER,
            "P1\treviewed\tABCB\tABC6 ABCF\tProtein B",
            "P2\treviewed\tABC6\t\tProtein 6",
        ]

        assert _parse_search_rows(lines, "ABC6") == ("P2", "Protein 6")

    def test_an_obsolete_symbol_resolves_through_the_synonym_column(self):
        """C6orf195 is published from LINC01600's entry, which lists it."""
        lines = [
            _HEADER,
            "Q96MT4\tunreviewed\tLINC01600\tC6orf195\t"
            "Uncharacterized protein encoded by LINC01600",
        ]

        assert _parse_search_rows(lines, "C6orf195") == (
            "Q96MT4",
            "Uncharacterized protein encoded by LINC01600",
        )

    def test_a_reviewed_synonym_match_beats_an_unreviewed_one(self):
        lines = [
            _HEADER,
            "P1\tunreviewed\tOTHER1\tNOTCH3 FOO\tProtein 1",
            "P2\treviewed\tOTHER2\tBAR NOTCH3\tProtein 2",
        ]

        assert _parse_search_rows(lines, "NOTCH3") == ("P2", "Protein 2")

    def test_without_a_review_column_the_first_synonym_match_stands(self):
        lines = [
            "Entry\tGene Names (primary)\tGene Names (synonym)\tProtein names",
            "P1\tOTHER1\tNOTCH3\tProtein 1",
            "P2\tOTHER2\tNOTCH3\tProtein 2",
        ]

        assert _parse_search_rows(lines, "NOTCH3") == ("P1", "Protein 1")


# ---------------------------------------------------------------------------
# fetch_uniprot_go_info
# ---------------------------------------------------------------------------


class TestFetchUniProtGoInfo:
    async def test_successful_fetch(self, mocker):
        tsv = (
            "Gene Ontology (biological process)\t"
            "Gene Ontology (molecular function)\t"
            "Gene Ontology (cellular component)\n"
            "apoptotic process [GO:0006915]\t"
            "receptor activity [GO:0004872]\t"
            "membrane [GO:0016020]\n"
        )
        resp = httpx.Response(200, text=tsv)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp)
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get",
            return_value=mock_client,
        )

        result = await fetch_uniprot_go_info("P12345")
        assert result is not None
        assert result["biological_process"] == "apoptotic process"
        assert result["molecular_function"] == "receptor activity"
        assert result["cellular_component"] == "membrane"

    async def test_non_200_is_no_answer(self, mocker):
        """A failed call is not an entry with no GO terms.

        The empty mapping is what a 200 with no GO columns means; a 404, a
        5xx or a transport failure has to stay distinguishable from it, or
        the caller stores empty GO columns as a 30-day success.
        """
        resp = httpx.Response(404)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp)
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get",
            return_value=mock_client,
        )

        assert await fetch_uniprot_go_info("P12345") is None

    async def test_fewer_columns(self, mocker):
        tsv = "go_p\nprocess only\n"
        resp = httpx.Response(200, text=tsv)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp)
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get",
            return_value=mock_client,
        )

        result = await fetch_uniprot_go_info("P12345")
        assert result is not None
        assert result["biological_process"] == "process only"
        assert result["molecular_function"] is None
        assert result["cellular_component"] is None

    async def test_timeout_is_no_answer(self, mocker):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get",
            return_value=mock_client,
        )

        assert await fetch_uniprot_go_info("P12345") is None

    async def test_header_only_returns_all_none(self, mocker):
        """A 200 that carries no GO columns is the empty answer, not a failure."""
        client = AsyncMock()
        client.get.return_value = httpx.Response(200, text="go_p\n")
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get", return_value=client
        )

        result = await fetch_uniprot_go_info("P1")
        assert result is not None
        assert all(v is None for v in result.values())

    async def test_request_error_is_no_answer(self, mocker):
        client = AsyncMock()
        client.get.side_effect = httpx.RequestError("network")
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get", return_value=client
        )

        assert await fetch_uniprot_go_info("P1") is None

    async def test_parse_error_is_no_answer(self, mocker):
        client = AsyncMock()
        client.get.return_value = httpx.Response(200, text="header\nvalue")
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get", return_value=client
        )
        mocker.patch(
            "pipeline.uniprot_fetch._clean_go_term", side_effect=ValueError("bad")
        )

        assert await fetch_uniprot_go_info("P1") is None


async def test_close_uniprot_client(mocker):
    close = mocker.patch(
        "pipeline.uniprot_fetch._client_manager.close", AsyncMock()
    )

    await close_uniprot_client()

    close.assert_awaited_once_with()


# ---------------------------------------------------------------------------
# fetch_uniprot_info (cached wrapper)
# ---------------------------------------------------------------------------


class TestFetchUniProtInfo:
    async def test_cache_hit(self):
        import pipeline.uniprot_fetch as mod

        cached = UniProtInfo("NOTCH3", "P12345", "Notch 3", None, None, None, "url")
        mod._uniprot_cache["NOTCH3"] = cached

        result = await fetch_uniprot_info("NOTCH3")
        assert result is cached

    async def test_cache_miss_fetches_and_caches(self, mocker):
        mocker.patch(
            "pipeline.uniprot_fetch._fetch_uniprot_accession_status",
            return_value=("P12345", "Notch 3", True),
        )
        mocker.patch(
            "pipeline.uniprot_fetch.fetch_uniprot_go_info",
            return_value={
                "biological_process": "bp",
                "molecular_function": "mf",
                "cellular_component": "cc",
            },
        )

        result = await fetch_uniprot_info("NOTCH3")
        assert result is not None
        assert result.accession == "P12345"
        assert result.url == "https://www.uniprot.org/uniprotkb/P12345/entry"

        import pipeline.uniprot_fetch as mod

        assert "NOTCH3" in mod._uniprot_cache

    async def test_a_go_transport_failure_is_a_transient_miss(self, mocker):
        """An accession with no GO answer must not become a 30-day row.

        The accession call succeeded, but the row would be written with three
        empty GO columns and filed as a success; the next sync would not
        revisit it until DB_CACHE_TTL_DAYS had passed.
        """
        mocker.patch(
            "pipeline.uniprot_fetch._fetch_uniprot_accession_status",
            return_value=("P12345", "Notch 3", True),
        )
        mocker.patch(
            "pipeline.uniprot_fetch.fetch_uniprot_go_info", return_value=None
        )

        result = await fetch_uniprot_info("NOTCH3")

        assert result is not None
        assert result.accession is None
        assert result.cacheable_miss is False

    async def test_no_accession_returns_empty_info(self, mocker):
        mocker.patch(
            "pipeline.uniprot_fetch._fetch_uniprot_accession_status",
            return_value=(None, None, True),
        )

        result = await fetch_uniprot_info("FAKE")
        assert result is not None
        assert result.accession is None
        assert result.url is None
        assert result.protein_name is None

    async def test_concurrent_calls_fetch_once(self, mocker):
        """Concurrent callers for the same symbol must share one upstream call.

        Regression guard for the thundering-herd race: before the fix, the
        second cache double-check happened before acquiring the UniProt
        semaphore, so N concurrent callers for one gene issued N UniProt
        requests instead of 1.
        """
        import asyncio

        import pipeline.uniprot_fetch as mod

        clear_uniprot_cache()
        accession_calls = 0

        async def slow_accession(symbol: str) -> tuple[str, str, bool]:
            nonlocal accession_calls
            accession_calls += 1
            await asyncio.sleep(0)
            return "P12345", "Notch 3", True

        mocker.patch(
            "pipeline.uniprot_fetch._fetch_uniprot_accession_status",
            side_effect=slow_accession,
        )
        mocker.patch(
            "pipeline.uniprot_fetch.fetch_uniprot_go_info",
            return_value={
                "biological_process": None,
                "molecular_function": None,
                "cellular_component": None,
            },
        )

        results = await asyncio.gather(
            *(fetch_uniprot_info("NOTCH3") for _ in range(10))
        )
        assert all(r is not None and r.accession == "P12345" for r in results)
        assert accession_calls == 1
        assert "NOTCH3" in mod._uniprot_cache


# ---------------------------------------------------------------------------
# fetch_uniprot_batch
# ---------------------------------------------------------------------------


class TestFetchUniProtBatch:
    async def test_empty_list(self):
        result = await fetch_uniprot_batch([])
        assert result == []

    async def test_a_missing_answer_is_a_transient_placeholder(self, mocker):
        """No answer at all is not a confirmed "not in UniProt"."""
        mocker.patch("pipeline.uniprot_fetch.fetch_uniprot_info", return_value=None)

        (placeholder,) = await fetch_uniprot_batch(["A"])

        assert placeholder.accession is None
        assert placeholder.cacheable_miss is False

    async def test_progress_callback(self, mocker):
        info = UniProtInfo("A", "P1", None, None, None, None, None)
        mocker.patch(
            "pipeline.uniprot_fetch.fetch_uniprot_info",
            return_value=info,
        )
        calls = []
        await fetch_uniprot_batch(
            ["A", "B"],
            progress_callback=lambda cur, tot: calls.append((cur, tot)),
        )
        assert len(calls) == 2
        assert calls[-1] == (2, 2)


# ---------------------------------------------------------------------------
# sync_uniprot_info
# ---------------------------------------------------------------------------


class TestSyncUniProtInfo:
    async def test_all_cached(self, mocker):
        mocker.patch(
            "pipeline.database.get_cached_uniprot_info",
            return_value={"NOTCH3": {}, "HTRA1": {}},
        )

        result = await sync_uniprot_info(["NOTCH3", "HTRA1"])
        assert isinstance(result, SyncResult)
        assert result.fetched == 0
        assert result.cached == 2

    async def test_fetches_missing_stores_results(self, mocker):
        mocker.patch(
            "pipeline.database.get_cached_uniprot_info",
            return_value={"NOTCH3": {}},
        )
        successful = UniProtInfo("HTRA1", "P99999", "prot", None, None, None, "url")
        failed = UniProtInfo("FAKE", None, None, None, None, None, None)
        mocker.patch(
            "pipeline.uniprot_fetch.fetch_uniprot_batch",
            return_value=[successful, failed],
        )
        mock_upsert = mocker.patch(
            "pipeline.database.upsert_uniprot_batch",
            return_value=1,
        )

        result = await sync_uniprot_info(["NOTCH3", "HTRA1", "FAKE"])
        assert result.cached == 1
        assert result.fetched == 1
        assert result.failed == 1
        assert any("FAKE" in e for e in result.errors)
        # Both successful and failed are upserted
        assert mock_upsert.call_count == 2

    async def test_a_go_transport_failure_writes_no_row(self, mocker):
        """End to end: accession found, GO call times out, nothing stored."""
        clear_uniprot_cache()
        mocker.patch("pipeline.database.get_cached_uniprot_info", return_value={})
        mocker.patch(
            "pipeline.uniprot_fetch._fetch_uniprot_accession_status",
            return_value=("Q92743", "HTRA1", True),
        )
        client = AsyncMock()
        client.get.side_effect = httpx.TimeoutException("timeout")
        mocker.patch(
            "pipeline.uniprot_fetch._client_manager.get", return_value=client
        )
        mock_upsert = mocker.patch(
            "pipeline.database.upsert_uniprot_batch", return_value=0
        )

        result = await sync_uniprot_info(["HTRA1"])

        mock_upsert.assert_not_called()
        assert result.fetched == 0
        assert result.failed == 1

    async def test_transient_failures_not_stored(self, mocker):
        mocker.patch(
            "pipeline.database.get_cached_uniprot_info",
            return_value={},
        )
        transient = UniProtInfo(
            "HTRA1", None, None, None, None, None, None, cacheable_miss=False
        )
        mocker.patch(
            "pipeline.uniprot_fetch.fetch_uniprot_batch",
            return_value=[transient],
        )
        mock_upsert = mocker.patch(
            "pipeline.database.upsert_uniprot_batch",
            return_value=0,
        )

        result = await sync_uniprot_info(["HTRA1"])
        assert result.failed == 1
        mock_upsert.assert_not_called()
