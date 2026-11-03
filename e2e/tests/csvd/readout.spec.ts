import { expect, test } from "@playwright/test";
import { choice } from "../../helpers.ts";

/**
 * The density readouts over the committed cSVD rows, by trait and gene name.
 * The readout's behaviour is tested over derived counts in
 * ../readout.spec.ts; a fork deletes the whole e2e/tests/csvd/ tree.
 */
const fillHeights = (page: import("@playwright/test").Page) =>
  page.locator(".readout-bar-fill").evaluateAll((els) =>
    els.reduce((sum, el) => sum + el.getBoundingClientRect().height, 0)
  );

test("ticking WMH drains the genes readout from 87 to 45", async ({ page }) => {
  await page.goto("/genes");
  const shown = page.locator(".readout-stat-value").first();
  await expect(shown).toHaveText(/87\s*\/\s*87/);
  const full = await fillHeights(page);

  await choice(page, "GWAS Traits", "WMH").check();

  await expect(shown).toHaveText(/45\s*\/\s*87/);
  await expect.poll(() => fillHeights(page)).toBeLessThan(full);
});

test("searching HTRA1 leaves two genes in the readout", async ({ page }) => {
  await page.goto("/genes");
  // HTRA1, and GLA, whose source quote names it.
  await page.getByRole("searchbox", { name: "Search genes" }).fill("HTRA1");
  await expect(page.locator(".readout-stat-value").first())
    .toHaveText(/2\s*\/\s*87/);
});

test("searching CADASIL leaves three of the five trials in the readout", async ({ page }) => {
  await page.goto("/trials");
  await page.getByRole("searchbox", { name: "Search trials" }).fill("CADASIL");

  // Two of the five CADASIL trials (Palm tocotrienols complex,
  // Adrenomedullin) are Completed and hidden by the default status filter.
  await expect(page.locator(".readout-stat-value").first())
    .toHaveText(/3\s*\/\s*102/);
});
