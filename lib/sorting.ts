/** Helpers for values whose display format is not naturally sortable. */

const MONTH_YEAR = /^(0?[1-9]|1[0-2])\/(\d{4})$/;

/**
 * The trial table's M/YYYY completion dates, parsed once for both the sort
 * key below and the display form in lib/constants.ts (`formatMonthYear`).
 * One regex, one home: a divergence would silently split what sorts as a
 * date from what displays as one.
 */
export function parseMonthYear(
  value: string,
): { month: number; year: number } | null {
  const match = MONTH_YEAR.exec(value.trim());
  if (!match) return null;
  return { month: Number(match[1]), year: Number(match[2]) };
}

/** Converts the trial table's M/YYYY values to a chronological numeric key. */
export function completionDateKey(value: string): number | null {
  const parsed = parseMonthYear(value);
  if (!parsed) return null;
  return parsed.year * 12 + parsed.month - 1;
}

/**
 * Compares two completion-date display strings chronologically.
 *
 * Placing sentinels last is the column's job now: its accessor reads
 * `undefined` for a non-date value and `sortUndefined: "last"` keeps it there
 * in both sort directions (TanStack negates this comparator itself for
 * descending), so this only ever needs to order two dates. Two values that
 * both fail to parse still compare by text, for callers outside that column.
 */
export function compareCompletionDates(a: string, b: string): number {
  const aKey = completionDateKey(a);
  const bKey = completionDateKey(b);

  if (aKey !== null && bKey !== null) return aKey - bKey;
  if (aKey !== null) return -1;
  if (bKey !== null) return 1;
  return a.localeCompare(b, "en", { numeric: true, sensitivity: "base" });
}

/**
 * The chromosome a cytogenetic location sits on: "7q31.1" -> "7".
 *
 * Returns null for anything that is not a recognised human chromosome, so a
 * sentinel or a malformed value is counted as unplaced rather than silently
 * bucketed onto chromosome 1.
 */
export function chromosomeOf(location: string): string | null {
  // Anchor the complete band. A prefix-only match turns malformed values such
  // as "1garbage" or "Xylophone" into real chromosomes and quietly inflates
  // their density buckets. NCBI writes a gene across a band edge as a range
  // on one chromosome ("10q24.33-q25.1"), so a second band may follow.
  const match =
    /^\s*(\d+|[XY])(?:[pq]\d+(?:\.\d+)?(?:-[pq]\d+(?:\.\d+)?)?)?\s*$/i
      .exec(location);
  if (!match) return null;

  const raw = match[1].toUpperCase();
  if (raw === "X" || raw === "Y") return raw;

  const number = Number(raw);
  return number >= 1 && number <= 22 ? String(number) : null;
}

/** Chromosome buckets in karyotype order. */
export const CHROMOSOMES: readonly string[] = [
  ...Array.from({ length: 22 }, (_, i) => String(i + 1)),
  "X",
  "Y",
];

/**
 * Karyotype rank for a cytogenetic band: 1-22, then X, Y, then anything
 * unplaced.
 *
 * `sortFn_alphanumeric` compares the leading chunk of each value and puts a
 * string chunk before a numeric one, so "Xq22.1" sorted *above* chromosome 1
 * -- which stayed invisible until a run added the table's first X-linked
 * gene. An unrecognised value ranks last rather than first,
 * so a sentinel cannot head the column.
 */
export function chromosomeRank(location: string): number {
  const name = chromosomeOf(location);
  return name === null ? CHROMOSOMES.length : CHROMOSOMES.indexOf(name);
}

/**
 * Compare two cytogenetic bands in karyotype order, then within a chromosome
 * by the band string, so "1p36.13" precedes "1q42.13".
 */
export function compareChromosomalLocation(a: string, b: string): number {
  const rank = chromosomeRank(a) - chromosomeRank(b);
  return rank !== 0 ? rank : a.localeCompare(b);
}
