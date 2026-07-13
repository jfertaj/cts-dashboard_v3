import { test, expect } from "@playwright/test";
import { S } from "../utils/selectors";
import { SCREENED } from "../../src/lib/chartAggregation";

/**
 * Explorer — tabla de resultados: tooltip de celda.
 *
 * Account Name es la columna que más se recorta (nombres largos + ancho
 * arrastrable), así que es justo la que más necesita el `title`. El fixture trae
 * un nombre de más de 40 caracteres para que el tooltip deba aparecer.
 */
const LONG_NAME = "Hospital Universitario y Politécnico La Fe de Valencia";

const ROWS = [
  { account_id: "a1", account_name: LONG_NAME, country: "ES", city: "Valencia",
    data: { "sf.Account.Name": LONG_NAME, [SCREENED]: 100 } },
];

test.describe("Explorer — tabla de resultados", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/salesforce/me", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: true }) })
    );
    await page.route("**/api/salesforce/map/bootstrap", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );
    // OJO: `getExplorerFields` hace `pick(raw, "fields", [])`. Un array pelado
    // deja el catálogo vacío y la tabla se queda sin columnas dinámicas — entre
    // ellas Account Name, que es justo la de este test.
    await page.route("**/api/explorer/fields", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          fields: [
            { key: "sf.Account.Name", label: "Account Name", type: "string", source: "sf" },
            { key: "site.country", label: "Country", type: "string", source: "site" },
          ],
        }),
      })
    );
    await page.route("**/api/explorer/search", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ points: [], rows: ROWS, total: ROWS.length }),
      })
    );
    await page.goto("/explorer");
    await expect(page.locator(S.EXPLORER_BTN_CHART)).toBeEnabled();
  });

  test("XTABLE-1: la celda de Account Name larga lleva title con el nombre entero", async ({ page }) => {
    // El title es lo que rescata al usuario cuando arrastra la columna estrecha
    // y el nombre empieza a recortarse.
    const cell = page.locator(S.EXPLORER_TABLE_CELL, { hasText: LONG_NAME }).first();
    await expect(cell).toHaveAttribute("title", LONG_NAME);
  });
});
