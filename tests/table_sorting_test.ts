import { assert, assertEquals, assertNotEquals } from "@std/assert";
import { sortFn_basic } from "@tanstack/table-core";
import type { Row, SortFn } from "@tanstack/table-core";

import { SHELL_FEATURES } from "../components/TableShell.tsx";
import { genes, trials } from "../lib/data.ts";
import {
  chromosomeOf,
  chromosomeRank,
  CHROMOSOMES,
  compareChromosomalLocation,
  compareCompletionDates,
  completionDateKey,
} from "../lib/sorting.ts";

/**
 * These tests guard the `sortFns` registry rather than any code in this repo.
 *
 * TanStack Table v9 made the built-in sorting functions opt-in for
 * tree-shaking: `sortFn: 'auto'` picks a name ("alphanumeric" for values with
 * digit runs, "text" otherwise) and then looks that name up in the registry
 * declared on the `features` option. With the registry empty every column
 * falls back to `sortFn_basic`, a raw `>` comparison — which still sorts, so
 * nothing crashes and nothing warns in a production build. Dropping either key
 * below silently reorders every table in the app.
 */

const SORT_FNS = SHELL_FEATURES.sortFns;

/** The minimum a sort function touches: `getValue` for the sorted column. */
const asRow = (value: string) =>
  ({ getValue: () => value }) as unknown as Row<typeof SHELL_FEATURES, never>;

const sortedBy = (fn: SortFn<typeof SHELL_FEATURES, never>, values: string[]) =>
  [...values].sort((a, b) => fn(asRow(a), asRow(b), "column"));

const distinct = (values: string[]) => [...new Set(values)];

Deno.test("the registry holds the names `sortFn: 'auto'` resolves", () => {
  // `column_getAutoSortFn` asks for exactly these two names, and falls back to
  // `sortFn_basic` when the lookup misses.
  assert(SORT_FNS.alphanumeric, "alphanumeric must be registered");
  assert(SORT_FNS.text, "text must be registered");
  assert(SORT_FNS.chromosome, "chromosome must be registered");
});

Deno.test("target sample sizes sort numerically, not lexicographically", () => {
  // The failure this pins down: `sortFn_basic`, the fallback used whenever the
  // registry lookup misses, compares raw strings and puts 1300 before 15.
  const sizes = ["15", "1300", "200", "8"];
  assertEquals(sortedBy(SORT_FNS.alphanumeric, sizes), [
    "8",
    "15",
    "200",
    "1300",
  ]);
  const basic = sortedBy(sortFn_basic, sizes);
  assert(basic.indexOf("1300") < basic.indexOf("15"));
  assertNotEquals(sortedBy(SORT_FNS.alphanumeric, sizes), basic);

  // And over whatever the committed table carries.
  const committed = distinct(trials.map((t) => t.targetSampleSize))
    .filter((size) => /^\d+$/.test(size));
  assertEquals(
    sortedBy(SORT_FNS.alphanumeric, committed),
    [...committed].sort((a, b) => Number(a) - Number(b)),
  );
});

Deno.test("chromosomal locations sort in karyotype order", () => {
  const locations = distinct(genes.map((g) => g.chromosomalLocation));
  const ordered = sortedBy(SORT_FNS.chromosome, locations);
  const ranks = ordered.map(chromosomeRank);

  // Every location ranks. The previous version parsed `^\d+` and dropped the
  // NaNs, which quietly excused "Xq22.1" from the assertion below -- and X was
  // exactly the value `alphanumeric` got wrong, sorting it above chromosome 1.
  assertEquals(ranks.filter(Number.isNaN), [], "every location must rank");
  assertEquals(
    ranks,
    [...ranks].sort((a, b) => a - b),
    "chromosome 2 must not follow chromosome 10, and X must follow 22",
  );
  // The regression itself, stated directly.
  assert(
    chromosomeRank("Xq22.1") > chromosomeRank("22q11.1"),
    "X must sort after chromosome 22",
  );
  assertEquals(
    sortedBy(SORT_FNS.alphanumeric, ["Xq22.1", "1p36.13"])[0],
    "Xq22.1",
    "alphanumeric still puts X first -- which is why the column names " +
      "`chromosome` instead",
  );
});

Deno.test("chromosomeRank sends an unplaceable value to the end", () => {
  // Sorting ascending must not put a sentinel at the top of the column, which
  // is what a rank of -1 or 0 for "not a chromosome" would do.
  const last = CHROMOSOMES.length;
  assertEquals(chromosomeRank("(unknown)"), last);
  assertEquals(chromosomeRank(""), last);
  assertEquals(chromosomeRank("Xylophone"), last);
  assert(chromosomeRank("(unknown)") > chromosomeRank("Yq11.23"));
});

Deno.test("within one chromosome the band decides", () => {
  // The rank ties, so the comparator falls through to the band string.
  assert(compareChromosomalLocation("1p36.13", "1q42.13") < 0);
  assert(compareChromosomalLocation("1q42.13", "1p36.13") > 0);
  assertEquals(compareChromosomalLocation("13q34", "13q34"), 0);
  // And across chromosomes the rank wins even when the band string would not.
  assert(compareChromosomalLocation("2p25.3", "13q34") < 0);
});

Deno.test("the chromosome sort tolerates a missing cell value", () => {
  // `getValue` returns undefined for a row whose column is absent; the
  // comparator coerces it rather than throwing, and it ranks last.
  const missing = { getValue: () => undefined } as unknown as Row<
    typeof SHELL_FEATURES,
    never
  >;
  const present = asRow("1p36.13");
  assert(SORT_FNS.chromosome!(present, missing, "column") < 0);
  assert(SORT_FNS.chromosome!(missing, present, "column") > 0);
});

Deno.test("text sorting ignores case", () => {
  // `sortFn_basic` compares raw strings, so every capitalised protein sorts
  // ahead of every lower-cased one regardless of letter.
  assertEquals(
    sortedBy(SORT_FNS.text, ["apoE", "ARSB", "Apo E"]),
    ["Apo E", "apoE", "ARSB"],
  );
});

Deno.test("estimated completion dates sort by year and then month", () => {
  const values = ["8/2031", "12/2026", "5/2028", "9/2026"];
  assertEquals(values.sort(compareCompletionDates), [
    "9/2026",
    "12/2026",
    "5/2028",
    "8/2031",
  ]);
  assertEquals(completionDateKey("13/2028"), null);
  assertEquals(completionDateKey("Completed, unpublished"), null);
});

Deno.test("estimated completion dates put every sentinel after valid dates", () => {
  assertEquals(compareCompletionDates("1/2027", "unknown"), -1);
  assertEquals(compareCompletionDates("unknown", "1/2027"), 1);
  assertEquals(
    ["unknown 10", "unknown 2"].sort(compareCompletionDates),
    ["unknown 2", "unknown 10"],
  );
  assertEquals(completionDateKey(" 01/2027 "), 2027 * 12);

  // Direction is TanStack's: it negates the comparator and keeps
  // `sortUndefined: "last"` rows last either way, so the helper compares
  // dates only.
  assertEquals(
    ["1/2027", "5/2030"].sort((a, b) => -compareCompletionDates(a, b)),
    ["5/2030", "1/2027"],
  );
});

Deno.test("chromosomeOf reads the chromosome off a cytogenetic band", () => {
  assertEquals(chromosomeOf("7q31.1"), "7");
  assertEquals(chromosomeOf("10q25.3"), "10");
  assertEquals(chromosomeOf(" 2q33.2 "), "2");
  assertEquals(chromosomeOf("Xq28"), "X");
  assertEquals(chromosomeOf("xp11.23"), "X");
  // NCBI's spelling for a gene across a band edge.
  assertEquals(chromosomeOf("10q24.33-q25.1"), "10");
});

Deno.test("chromosomeOf rejects values that are not chromosomes", () => {
  // Bucketing these onto chromosome 1 would invent data the row does not have.
  assertEquals(chromosomeOf("(unknown)"), null);
  assertEquals(chromosomeOf(""), null);
  assertEquals(chromosomeOf("23q11"), null);
  assertEquals(chromosomeOf("99"), null);
  assertEquals(chromosomeOf("1garbage"), null);
  assertEquals(chromosomeOf("Xylophone"), null);
  assertEquals(chromosomeOf("22q1junk"), null);
  assertEquals(chromosomeOf("10q24.33-"), null);
  assertEquals(chromosomeOf("10q24.33-11q25.1"), null);
});

Deno.test("every committed gene lands on a real chromosome", () => {
  const unplaced = genes.filter((g) =>
    chromosomeOf(g.chromosomalLocation) === null
  );
  assertEquals(
    unplaced.map((g) => `${g.gene}: ${g.chromosomalLocation}`),
    [],
    "the density readout would count these as unplaced",
  );
});
