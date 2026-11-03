import { expect, test } from "@playwright/test";
import { GENE_TOTAL } from "../../fixtures/expected-data.ts";
import {
  choice,
  expectRowCount,
  expectSummary,
  filterCount,
} from "../../helpers.ts";

/**
 * The genes filters over the committed cSVD rows, by trait name: SVS, WMH,
 * PSMD, PVWMH, CMB, WM-PVS and the relabelled Extreme-cSVD. The rules are
 * tested over derived choices in ../genes-filters.spec.ts; this is the
 * record of what the cSVD data yields, and a fork deletes the whole
 * e2e/tests/csvd/ tree.
 */
const MR = "Mendelian randomization performed";
const GWAS = "GWAS Traits";
const OMICS = "Evidence From Other Omics Studies";

test.beforeEach(async ({ page }) => {
  await page.goto("/genes");
});

test("picking SVS clears Show All and filters to its twenty-nine genes", async ({ page }) => {
  await choice(page, GWAS, "SVS").check();

  await expect(choice(page, GWAS, "Show All")).not.toBeChecked();
  await expect(filterCount(page, GWAS)).toHaveText("1");
  await expectRowCount(page, 29, GENE_TOTAL);
  await expectSummary(page, "GWAS Traits: SVS");
});

test("WMH and PSMD union", async ({ page }) => {
  await choice(page, GWAS, "WMH").check();
  await expectRowCount(page, 45, GENE_TOTAL);

  await choice(page, GWAS, "PSMD").check();
  await expectRowCount(page, 48, GENE_TOTAL);
  await expectSummary(page, "GWAS Traits: WMH, PSMD");
  await expect(filterCount(page, GWAS)).toHaveText("2");
});

/**
 * The R original compared trait strings raw and silently dropped rows stored
 * with a trailing space ("WMH ", "PSMD "). filterGenes normalizes both sides.
 *
 * The committed data no longer carries a padded WMH value -- all 45 are exact
 * -- because `data_contract_test.ts` now fails on edge whitespace and the
 * export trims on the way out, so the padding cannot come back. This is kept
 * as a regression guard on the normalizing itself rather than as a live
 * example of it: it fails if filterGenes ever stops trimming, which is what
 * the R version got wrong.
 */
test("trait matching ignores trailing whitespace in the data", async ({ page }) => {
  await choice(page, GWAS, "WMH").check();
  await expectRowCount(page, 45, GENE_TOTAL);
});

/** The data holds lowercase "proteomics" against a "Proteomics" choice. */
test("omics matching ignores case differences in the data", async ({ page }) => {
  await choice(page, OMICS, "Proteomics").check();
  await expectRowCount(page, 25, GENE_TOTAL);
  await expectSummary(page, "Evidence From Other Omics Studies: Proteomics");
});

test("Mendelian randomisation and Proteomics intersect", async ({ page }) => {
  await choice(page, MR, "No").uncheck();
  await choice(page, OMICS, "Proteomics").check();

  await expectRowCount(page, 22, GENE_TOTAL);
  await expectSummary(
    page,
    "Mendelian randomization performed: Yes | Evidence From Other Omics Studies: Proteomics",
  );
});

test("no Mendelian-randomisation gene carries NODDI", async ({ page }) => {
  // It was MR=Yes + SVS until the 365-day run put two genes in that
  // intersection, then PVWMH until the 2026-11-03 re-extraction gave HTRA1
  // both. NODDI (VCAN alone) is the one trait no MR gene carries now.
  await choice(page, MR, "No").uncheck();
  await choice(page, GWAS, "NODDI").check();

  await expectRowCount(page, 0, GENE_TOTAL);
  await expectSummary(
    page,
    "Mendelian randomization performed: Yes | GWAS Traits: NODDI",
  );
});

test("CMB and WM-PVS are selectable", async ({ page }) => {
  await choice(page, GWAS, "CMB").check();
  // APOE and COL4A1/2 were the first two; the 365-day run added four more.
  await expectRowCount(page, 6, GENE_TOTAL);

  await choice(page, GWAS, "CMB").uncheck();
  await choice(page, GWAS, "WM-PVS").check();
  await expectRowCount(page, 16, GENE_TOTAL);
});

test("the banner prints Extreme-cSVD, not the wire value extreme-cSVD", async ({ page }) => {
  await choice(page, GWAS, "Extreme-cSVD").check();
  await expectSummary(page, "GWAS Traits: Extreme-cSVD");
});
