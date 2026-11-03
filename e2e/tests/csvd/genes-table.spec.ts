import { expect, test } from "@playwright/test";
import { GENE_TOTAL } from "../../fixtures/expected-data.ts";
import { banner, rows } from "../../helpers.ts";

/**
 * The genes table over the committed cSVD rows, by gene name. The table's
 * behaviour is tested over derived rows in ../genes-table.spec.ts; a fork
 * deletes the whole e2e/tests/csvd/ tree.
 */
test.beforeEach(async ({ page }) => {
  await page.goto("/genes");
});

test("search narrows the table to HTRA1 and clearing restores it", async ({ page }) => {
  const search = page.getByRole("searchbox", { name: "Search genes" });

  // GLA comes too: the search covers every column, and GLA's source quote
  // names HTRA1 among the other SVD genes.
  await search.fill("  HTRA1  ");
  await expect(search).toHaveValue("  HTRA1  ");
  await expect(rows(page)).toHaveCount(2);
  await expect(rows(page).first().locator("td").first())
    .toHaveText("HTRA1", { useInnerText: true });
  await expect(rows(page).nth(1).locator("td").first())
    .toHaveText("GLA", { useInnerText: true });
  await expect(banner(page)).toHaveText(
    `Active Filters: Search: “HTRA1” — showing 2 of ${GENE_TOTAL} rows`,
  );
  await expect(page.locator(".pagination span").first())
    .toHaveText("Showing 1–2 of 2");

  await search.fill("");
  await expect(rows(page)).toHaveCount(10);
});

test("the pager walks nine pages of ten and lands on the seven-row remainder", async ({ page }) => {
  // 87 rows at 10 per page = 9 pages, two more than the pager's seven
  // buttons.
  await page.getByRole("button", { name: "Page 7", exact: true }).click();
  await page.getByRole("button", { name: "Page 8", exact: true }).click();
  await page.getByRole("button", { name: "Page 9", exact: true }).click();
  await expect(rows(page)).toHaveCount(7);
  await expect(page.locator(".pagination span").first())
    .toHaveText("Showing 81–87 of 87");
});
