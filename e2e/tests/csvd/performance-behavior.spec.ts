import { expect, test } from "@playwright/test";
import { rows, tooltip } from "../../helpers.ts";

/**
 * The two tooltip-deferral checks over LAMB1's committed NCBI record. The
 * behaviour is tested over a derived gene in ../performance-behavior.spec.ts;
 * a fork deletes the whole e2e/tests/csvd/ tree.
 */
test("a filtered-away tooltip reopens on LAMB1's record", async ({ page }) => {
  await page.goto("/genes");
  await page.waitForSelector('.table-controls[data-hydrated="true"]');
  await page.getByPlaceholder("Search genes").fill("LAMB1");
  await expect(rows(page)).toHaveCount(1);
  await page.locator("tbody .tooltip-box").first().press("Enter");
  await expect(tooltip(page)).toContainText("laminin subunit beta 1");
});

test("a rapid search burst commits LAMB1 on the full 87-row page", async ({ page }) => {
  await page.goto("/genes");
  await page.waitForSelector('.table-controls[data-hydrated="true"]');
  await page.locator(".table-control select").selectOption("100");
  await expect(rows(page)).toHaveCount(87);
  const search = page.getByPlaceholder("Search genes");
  await search.pressSequentially(" LAMB1 ", { delay: 5 });
  await expect(search).toHaveValue(" LAMB1 ");
  await expect(rows(page)).toHaveCount(1);
  await expect(
    page.getByRole("status").filter({ hasText: "Active Filters:" }),
  ).toContainText("Search: “LAMB1”");
});
