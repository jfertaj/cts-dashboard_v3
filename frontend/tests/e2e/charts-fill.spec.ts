import { test, expect, type Page } from "@playwright/test";
import { S } from "../utils/selectors";
import { SCREENED, STAGE1, STAGE2, ASSIGNMENTS } from "../../src/lib/chartAggregation";

/**
 * Explorer — el ChartModal contra un backend que se comporta como el de VERDAD.
 *
 * Por qué existe este fichero aparte de `charts.spec.ts`: aquel fixture le sirve
 * al frontend filas que YA traen los valores de las métricas en `row.data`. El
 * backend real NO hace eso. `POST /api/explorer/search` devuelve esas claves a
 * `null` en las 215 filas, y los valores sólo llegan después, perezosamente, por
 * `POST /api/explorer/columns/fill`. Con un fixture que regala los valores, un
 * frontend que jamás pide las métricas al servidor pasa los tests en verde —
 * que es exactamente lo que ocurrió: 63 tests unitarios, 118 E2E y tres
 * revisores no vieron que Países anunciaba "0 de 215 centros reportan" contra
 * producción cuando la verdad eran 81.
 *
 * Así que aquí el contrato del fixture es el del backend real:
 *   - la búsqueda en bloque devuelve las métricas a `null`, SIEMPRE;
 *   - el fill devuelve un valor sólo para las columnas que el frontend PIDE.
 * Un valor sólo puede aparecer en pantalla si el código lo ha pedido.
 */

/** La verdad del "servidor". Sólo se revela a quien pide la columna en el fill. */
const TRUTH: Record<string, Record<string, number>> = {
  a1: { [SCREENED]: 100, [STAGE1]: 50, [STAGE2]: 20, [ASSIGNMENTS]: 3 },
  a2: { [SCREENED]: 60, [STAGE1]: 30, [ASSIGNMENTS]: 1 },
  a3: { [SCREENED]: 40, [ASSIGNMENTS]: 0 },
  a4: { [ASSIGNMENTS]: 0 },
  a5: { [ASSIGNMENTS]: 0 },
  // Cribó a CERO personas y lo reportó: es un dato, no un hueco.
  a6: { [SCREENED]: 0, [ASSIGNMENTS]: 0 },
};

const SITES = [
  { account_id: "a1", account_name: "Centro Madrid", country: "ES", city: "Madrid" },
  { account_id: "a2", account_name: "Centro Barcelona", country: "ES", city: "Barcelona" },
  { account_id: "a3", account_name: "Centro Milano", country: "IT", city: "Milano" },
  { account_id: "a4", account_name: "Centro Roma", country: "IT", city: "Roma" },
  { account_id: "a5", account_name: "Centro Paris", country: "FR", city: "Paris" },
  { account_id: "a6", account_name: "Centro Lisboa", country: "PT", city: "Lisboa" },
];

/** Las claves de métrica existen en la fila, pero vienen vacías — como en prod. */
const BULK_ROWS = SITES.map((s) => ({
  ...s,
  data: { [SCREENED]: null, [STAGE1]: null, [STAGE2]: null, [ASSIGNMENTS]: null },
}));

async function openChartModal(page: Page) {
  await page.locator(S.EXPLORER_BTN_CHART).click();
  const modal = page.locator(S.CHART_MODAL);
  await expect(modal).toBeVisible();
  return modal;
}

test.describe("Explorer — el chart pide sus métricas al servidor (fixture realista)", () => {
  /** Columnas que el frontend llegó a pedir a /columns/fill durante el test. */
  let filledColumns: Set<string>;

  test.beforeEach(async ({ page }) => {
    filledColumns = new Set<string>();

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
            { key: SCREENED, label: "Screened", type: "number", source: "sf" },
            { key: STAGE1, label: "Stage 1", type: "number", source: "sf" },
            { key: STAGE2, label: "Stage 2", type: "number", source: "sf" },
            { key: ASSIGNMENTS, label: "Assignments (count)", type: "number", source: "extra" },
          ],
        }),
      })
    );

    // Columnas por defecto: SÓLO Account Name. Ninguna métrica es visible, que
    // es la situación en la que el bug mentía.
    await page.addInitScript(() => {
      localStorage.setItem("explorer:visibleColumns", JSON.stringify(["sf.Account.Name"]));
    });

    await page.route("**/api/explorer/search", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ points: [], rows: BULK_ROWS, total: BULK_ROWS.length }),
      })
    );

    // El fill es la ÚNICA puerta por la que puede entrar un valor de métrica, y
    // sólo entrega las columnas que se le piden explícitamente.
    await page.route("**/api/explorer/columns/fill", async (route) => {
      const body = route.request().postDataJSON() as { account_ids?: string[]; columns?: string[] };
      const cols = body?.columns ?? [];
      const ids = body?.account_ids ?? [];
      cols.forEach((c) => filledColumns.add(c));

      const rows = ids.map((id) => {
        const truth = TRUTH[id] ?? {};
        const data: Record<string, number | null> = {};
        for (const col of cols) data[col] = col in truth ? truth[col] : null;
        return { account_id: id, data };
      });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ rows }),
      });
    });

    await page.goto("/explorer");
    await expect(page.locator(S.EXPLORER_BTN_CHART)).toBeEnabled();
  });

  test("CHART-10: con las columnas por defecto, Países reporta la cobertura REAL, no 0", async ({ page }) => {
    const modal = await openChartModal(page);
    const coverage = modal.locator(S.CHART_COVERAGE);

    // 4 de los 6 centros reportan cribados (Lisboa reporta un 0, que es reportar).
    // Con el bug: "0 de 6 centros reportan" — nadie le pidió la columna al servidor.
    await expect(coverage).toContainText("4 de 6 centros reportan");
    await expect(coverage).not.toContainText("0 de 6 centros reportan");

    // Y la métrica llegó porque el código la PIDIÓ, no porque el fixture la regalara.
    expect(filledColumns.has(SCREENED)).toBe(true);
  });

  test("CHART-11: las 4 métricas se piden aunque ninguna sea columna visible", async ({ page }) => {
    const modal = await openChartModal(page);

    // Stage 2 sólo lo reporta Madrid: si el fill no lo hubiera pedido, el Embudo
    // no tendría un solo centro completo y no podría renderizarse jamás.
    await modal.locator(S.CHART_METRIC_SELECT).selectOption(STAGE2);
    await expect(modal.locator(S.CHART_COVERAGE)).toContainText("1 de 6 centros reportan");

    await modal.locator(S.CHART_METRIC_SELECT).selectOption(STAGE1);
    await expect(modal.locator(S.CHART_COVERAGE)).toContainText("2 de 6 centros reportan");

    for (const metric of [SCREENED, STAGE1, STAGE2, ASSIGNMENTS]) {
      expect(filledColumns.has(metric), `el fill nunca pidió ${metric}`).toBe(true);
    }
  });

  test("CHART-12: el Embudo agrega el único centro que reporta las tres etapas", async ({ page }) => {
    const modal = await openChartModal(page);
    await modal.locator(S.CHART_TAB_FUNNEL).click();

    // Sólo Madrid (100/50/20) reporta las tres. Con el bug, ningún centro las
    // reportaba y el Embudo era una pantalla vacía permanente.
    // (El Embudo no lleva selector de métrica, así que su línea de cobertura
    // tiene redacción propia — no es la de MetricPicker.)
    const coverage = modal.locator(S.CHART_COVERAGE);
    await expect(coverage).toContainText("sobre 1 centros que reportan las tres métricas");
    await expect(coverage).toContainText("5 centros excluidos");
    await expect(modal.locator(S.CHART_EMPTY)).toHaveCount(0);
  });
});
