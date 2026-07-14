import { test, expect, type Page } from "@playwright/test";
import { S } from "../utils/selectors";

/**
 * Explorer — "Ask Moby" tiene que entregar las filas que el usuario VE, no el
 * array de respaldo entero.
 *
 * La tabla filtra en CLIENTE encima del resultado del servidor (búsqueda
 * global + filtros por columna): el contador de resultados, el export TSV y
 * los gráficos ya salen de `table.getFilteredRowModel()`. "Ask Moby" hacía
 * `activeRows = nearbyActive ? fullNearbyRows : fullRows` — el array de
 * respaldo SIN filtrar — y se lo pasaba a Moby via sessionStorage +
 * `cts:explorer:ask-ai`. Si el usuario acota la tabla a 2 filas y pregunta
 * "cuántas hay", Moby razonaba sobre las 6 originales, incluidas las 4 que el
 * usuario acaba de sacar de la vista y no puede ver.
 *
 * Mismo fixture que CHART-8 (charts.spec.ts): "IT" sólo aparece en la columna
 * country (ES/IT/FR/PT) — deja Milano y Roma, 2 de los 6 centros.
 */
const ROWS = [
  { account_id: "a1", account_name: "Centro Madrid",    country: "ES", city: "Madrid",
    data: {} },
  { account_id: "a2", account_name: "Centro Barcelona", country: "ES", city: "Barcelona",
    data: {} },
  { account_id: "a3", account_name: "Centro Milano",    country: "IT", city: "Milano",
    data: {} },
  { account_id: "a4", account_name: "Centro Roma",      country: "IT", city: "Roma",
    data: {} },
  { account_id: "a5", account_name: "Centro Paris",     country: "FR", city: "Paris",
    data: {} },
  { account_id: "a6", account_name: "Centro Lisboa",    country: "PT", city: "Lisboa",
    data: {} },
];

interface AskMobyEventDetail {
  table: { columns: Array<{ key: string; label: string }>; rows: unknown[] };
  filters: unknown;
  rowCount: number;
}

/** Engancha un listener ANTES de la interacción para no perder el evento. */
async function armAskMobyListener(page: Page): Promise<void> {
  await page.evaluate(() => {
    (window as unknown as { __askMobyDetail: AskMobyEventDetail | null }).__askMobyDetail = null;
    window.addEventListener("cts:explorer:ask-ai", (e: Event) => {
      (window as unknown as { __askMobyDetail: unknown }).__askMobyDetail = (e as CustomEvent).detail;
    });
  });
}

test.describe("Explorer — Ask Moby entrega las filas filtradas", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/salesforce/me", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: true }) })
    );
    await page.route("**/api/salesforce/map/bootstrap", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );
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
    await page.addInitScript(() => {
      localStorage.setItem("explorer:visibleColumns", JSON.stringify(["sf.Account.Name"]));
    });
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

  test("ASK-MOBY-1: narrowing the table then Ask Moby ships only the narrowed rows and count", async ({ page }) => {
    await page.locator(S.EXPLORER_GLOBAL_SEARCH).fill("IT");
    await expect(page.locator(S.EXPLORER_RESULTS_COUNT)).toContainText("2 results");

    await armAskMobyListener(page);
    await page.locator(S.EXPLORER_BTN_ASK_MOBY).click();

    const stored = await page.evaluate(() => sessionStorage.getItem("moby_last_table_v1"));
    expect(stored).not.toBeNull();
    const table = JSON.parse(stored as string) as { rows: Array<{ account_name: string }> };

    // Con el bug: 6 filas (el array de respaldo entero). Sólo Milano y Roma
    // deben viajar — los 4 centros que el usuario acaba de filtrar fuera NO.
    expect(table.rows).toHaveLength(2);
    const names = table.rows.map((r) => r.account_name).sort();
    expect(names).toEqual(["Centro Milano", "Centro Roma"]);

    // El `rowCount` del detail del evento es la misma mentira en miniatura si
    // no coincide con las filas que de verdad viajan.
    const detail = await page.evaluate(
      () => (window as unknown as { __askMobyDetail: AskMobyEventDetail }).__askMobyDetail
    );
    expect(detail.rowCount).toBe(2);
    expect(detail.table.rows).toHaveLength(2);
  });

  test("ASK-MOBY-2: without narrowing, Ask Moby still ships every visible row", async ({ page }) => {
    await expect(page.locator(S.EXPLORER_RESULTS_COUNT)).toContainText("6 results");

    await armAskMobyListener(page);
    await page.locator(S.EXPLORER_BTN_ASK_MOBY).click();

    const detail = await page.evaluate(
      () => (window as unknown as { __askMobyDetail: AskMobyEventDetail }).__askMobyDetail
    );
    expect(detail.rowCount).toBe(6);
    expect(detail.table.rows).toHaveLength(6);
  });
});
