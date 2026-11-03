/**
 * hg38 cytobands: the karyogram's geometry, and the band → coordinate lookup
 * that places a gene from its `chromosomalLocation` string.
 *
 * `data/cytobands_hg38.json` is written by `scripts/fetch_cytobands.py` from
 * UCSC `cytoBandIdeo.txt.gz` (`deno task cytobands`) and committed like every
 * other data file; nothing fetches at build or run time.
 */

import cytobandsJson from "../data/cytobands_hg38.json" with { type: "json" };

/** The UCSC `gieStain` values, in the order the encoding's `stains` map lists them. */
export const STAINS = [
  "gneg",
  "gpos25",
  "gpos50",
  "gpos75",
  "gpos100",
  "acen",
  "gvar",
  "stalk",
] as const;

export interface Band {
  name: string;
  start: number;
  end: number;
  stain: string;
}

export interface Chromosome {
  name: string;
  length: number;
  bands: Band[];
}

export interface CytobandTable {
  assembly: string;
  source: string;
  chromosomes: Chromosome[];
}

export const cytobands: CytobandTable = cytobandsJson;

export interface BandHit {
  chromosome: string;
  band: string;
  start: number;
  end: number;
  midpoint: number;
}

/**
 * `7q31.1` → chromosome `7`, band `q31.1`; `10q24.33-q25.1` → the same
 * chromosome, two bands. Anchored: `23q11` and `7q31.1x` fail.
 */
const LOCATION = /^(\d{1,2}|X|Y)([pq]\d+(?:\.\d+)?)(?:-([pq]\d+(?:\.\d+)?))?$/;

/**
 * The region a location names, or null. Exact bands only: a location naming
 * a parent band (`7q31`) or a chromosome the table lacks is unplaced rather
 * than guessed, so a bad string can never land silently on the wrong band.
 * NCBI names a gene that crosses a band edge with a range (`10q24.33-q25.1`);
 * both ends must be exact bands of that one chromosome, and the hit spans
 * them, in either order.
 */
export function placeGene(
  location: string,
  table: CytobandTable = cytobands,
): BandHit | null {
  const match = LOCATION.exec(location.trim());
  if (!match) return null;
  const [, chromosome, first, last] = match;
  const bands = table.chromosomes.find((c) => c.name === chromosome)?.bands ??
    [];
  const ends: Band[] = [];
  for (const name of last === undefined ? [first] : [first, last]) {
    const band = bands.find((b) => b.name === name);
    if (!band) return null;
    ends.push(band);
  }
  const start = Math.min(...ends.map((b) => b.start));
  const end = Math.max(...ends.map((b) => b.end));
  return {
    chromosome,
    band: last === undefined ? first : `${first}-${last}`,
    start,
    end,
    midpoint: (start + end) / 2,
  };
}
