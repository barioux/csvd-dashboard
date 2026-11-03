import { expect, type Page, test } from "@playwright/test";

import { EXPECTED, TRAIT_COUNT } from "../fixtures/expected-data.ts";
import { tooltip } from "../helpers.ts";

/**
 * The karyogram is drawn in-app by islands/Phenogram.tsx. Pill backgrounds are
 * fitted from getBBox() after the fonts settle, so anything that reads a box
 * polls rather than asserting once. The interactive layer is an HTML list of
 * buttons over the SVG, so the tooltip assertions are the same ones the tables
 * use. Every count is derived from the committed data; the tests that name a
 * cSVD gene or trait are in csvd/phenogram.spec.ts.
 */

const FIGURE = "svg.phenogram-figure";
const P = EXPECTED.phenogram;

async function figureSettled(page: Page) {
  await page.goto("/phenogram");
  await expect(page.locator(FIGURE)).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
}

/** The key is a closed `<details>` above the plate; open it before reading it. */
async function openKey(page: Page) {
  await page.locator("#phenogram-key summary").click();
}

test("draws every placeable gene on its chromosome, with both legends", async ({ page }) => {
  await figureSettled(page);
  const figure = page.locator(FIGURE);
  await expect(figure).toHaveAttribute("viewBox", "0 0 1732 1640");
  await expect(figure.locator("g.phenogram-chromosome")).toHaveCount(
    P.chromosomes,
  );
  await expect(figure.locator("g.phenogram-block")).toHaveCount(P.blocks);
  await expect(page.locator(".phenogram-genes button")).toHaveCount(P.blocks);
  await expect(page.locator(".phenogram-legend-family")).toHaveCount(
    P.families,
  );
  await expect(page.locator(".phenogram-legend-traits .phenogram-pill"))
    .toHaveCount(TRAIT_COUNT);
  // The gene list is interactive on the figure; nothing hidden carries a link.
  await expect(page.locator(".visually-hidden a")).toHaveCount(0);
  await expect(page.locator(".phenogram-unplaced")).toHaveCount(
    EXPECTED.geneRows > P.blocks ? 1 : 0,
  );
});

test("pill backgrounds fit their text and no two label blocks overlap", async ({ page }) => {
  await figureSettled(page);
  await expect.poll(() =>
    page.evaluate(() => {
      const misfit: string[] = [];
      const overlapping: string[] = [];
      const apart = (a: DOMRect, b: DOMRect) =>
        a.right < b.left || a.left > b.right || a.bottom < b.top ||
        a.top > b.bottom;

      for (
        const pill of document.querySelectorAll(
          "svg.phenogram-figure g.phenogram-pill-mark",
        )
      ) {
        const text = pill.querySelector<SVGTextElement>("text[data-pill]")!;
        const rect = pill.querySelector<SVGRectElement>("rect.pill-bg")!;
        const t = text.getBBox();
        const r = rect.getBBox();
        if (r.x > t.x || r.x + r.width < t.x + t.width) {
          misfit.push(text.dataset.pill ?? "?");
        }
      }

      const blocks = [
        ...document.querySelectorAll("svg.phenogram-figure g.phenogram-block"),
      ].map((g) => ({
        gene: g.getAttribute("data-gene") ?? "?",
        rect: g.querySelector("text.phenogram-symbol")!.getBoundingClientRect(),
        chromosome: g.getAttribute("data-chromosome"),
      }));
      for (let i = 0; i < blocks.length; i++) {
        for (let j = i + 1; j < blocks.length; j++) {
          if (blocks[i].chromosome !== blocks[j].chromosome) continue;
          if (!apart(blocks[i].rect, blocks[j].rect)) {
            overlapping.push(`${blocks[i].gene}/${blocks[j].gene}`);
          }
        }
      }
      return { misfit, overlapping, blocks: blocks.length };
    })
  ).toEqual({ misfit: [], overlapping: [], blocks: P.blocks });
});

test("hovering a gene opens its tooltip and the keyboard reaches its panel", async ({ page }) => {
  test.skip(P.blocks === 0, "no gene drawn on the karyogram");
  await figureSettled(page);
  const first = page.locator(".phenogram-genes button").first();
  await expect(first).toHaveText(new RegExp(`^${P.firstGene}, `));
  await first.hover();
  await expect(tooltip(page)).toBeVisible();
  await expect(tooltip(page)).toContainText("Location");
  await expect(tooltip(page)).toContainText("GWAS phenotypes");
  await page.mouse.move(5, 5);
  await expect(tooltip(page)).toHaveCount(0);

  await first.focus();
  await page.keyboard.press("Enter");
  await expect(tooltip(page)).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(tooltip(page)).toHaveCount(0);
  await expect(first).toBeFocused();
});

test("gene buttons follow chromosome order and name the evidence", async ({ page }) => {
  test.skip(P.blocks === 0, "no gene drawn on the karyogram");
  await figureSettled(page);
  const names = await page.locator(".phenogram-genes button").evaluateAll(
    (buttons) => buttons.map((b) => b.textContent?.trim() ?? ""),
  );
  expect(names).toHaveLength(P.blocks);
  for (const name of names) {
    expect(name).toMatch(/^.+, .+\. GWAS phenotypes: .+\./);
  }
});

test("the phenotype key explains every trait, with a definition where the standard gives one", async ({ page }) => {
  await figureSettled(page);
  await openKey(page);
  const key = page.locator(".phenogram-legend-traits");
  const pills = key.getByRole("button");
  await expect(pills).toHaveCount(TRAIT_COUNT);
  test.skip(TRAIT_COUNT === 0, "no trait in the vocabulary");
  await pills.first().hover();
  await expect(tooltip(page)).toBeVisible();
  await expect(tooltip(page).locator(".tooltip-row").first()).not.toBeEmpty();
  await page.mouse.move(5, 5);
  await expect(tooltip(page)).toHaveCount(0);
});

test("the figure's data colours do not follow the theme", async ({ page }) => {
  test.skip(P.blocks === 0, "no pill or chromosome band to sample");
  await figureSettled(page);
  const pill = page.locator(`${FIGURE} rect.pill-bg`).first();
  const band = page.locator(`${FIGURE} g.phenogram-chromosome rect`).nth(2);
  const before = {
    pill: await pill.getAttribute("fill"),
    band: await band.getAttribute("fill"),
    page: await page.evaluate(() =>
      getComputedStyle(document.body).backgroundColor
    ),
  };
  const plate = page.locator(".phenogram-canvas");
  const plateBefore = await plate.evaluate((el) =>
    getComputedStyle(el).backgroundColor
  );

  await page.getByRole("button", { name: /Switch to (dark|light) theme/ })
    .click();
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme",
    /dark|light/,
  );
  await expect.poll(() =>
    page.evaluate(() => getComputedStyle(document.body).backgroundColor)
  ).not.toBe(before.page);

  if (before.pill !== null) {
    await expect(pill).toHaveAttribute("fill", before.pill);
  }
  await expect(band).toHaveAttribute("fill", before.band ?? "");
  expect(
    await plate.evaluate((el) => getComputedStyle(el).backgroundColor),
  ).toBe(plateBefore);
});

test("the phenogram names its affordance", async ({ page }) => {
  await page.goto("/phenogram");
  const tips = page.locator(".tip-row .tip-box");
  await expect(tips).toHaveCount(1);
  await expect(tips.first()).toContainText("Hover over or activate a gene");
  await expect(tips.first()).toContainText("focus the item and press Enter");
});

test("a keyboard user can skip the figure to its end", async ({ page }) => {
  await page.goto("/phenogram");
  const skip = page.getByRole("link", { name: "Skip the figure" });
  await skip.focus();
  await expect(skip).toBeInViewport();
  await skip.press("Enter");
  await expect(page.locator("#phenogram-end")).toBeInViewport();
  await expect(page.locator("#phenogram-end")).toBeFocused();
});

test("the key sits above the plate and opens on demand", async ({ page }) => {
  await page.goto("/phenogram");
  const key = page.locator("#phenogram-key");
  await expect(key).not.toHaveAttribute("open", "");
  const keyBox = await key.boundingBox();
  const plateBox = await page.locator(".phenogram-layout > .blueprint-frame")
    .boundingBox();
  expect(keyBox!.y + keyBox!.height).toBeLessThanOrEqual(plateBox!.y);
  await key.locator("summary").click();
  await expect(key.locator(".phenogram-legend")).toBeVisible();
});
