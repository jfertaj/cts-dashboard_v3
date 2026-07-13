import { test, expect, type Page } from "@playwright/test";
import { S } from "../utils/selectors";
import { SCREENED, STAGE1, STAGE2 } from "../../src/lib/chartAggregation";

/**
 * Explorer — modal de gráficos con pestañas.
 *
 * El punto del rediseño: la mayoría de los centros NO reportan las métricas de
 * reclutamiento. Las vistas EXCLUYEN al que no reporta en vez de contarlo como
 * cero, y la línea de cobertura es lo único que impide leer mal el gráfico. Por
 * eso el fixture es deliberadamente desigual: 3 de 5 centros reportan cribados,
 * 2 reportan Stage 1 y solo 1 reporta Stage 2. Si alguien "arregla" la
 * agregación rellenando los huecos con ceros, la cobertura deja de moverse al
 * cambiar de métrica y CHART-2 se pone rojo.
 */
const ROWS = [
  { account_id: "a1", account_name: "Centro Madrid",    country: "ES", city: "Madrid",
    data: { [SCREENED]: 100, [STAGE1]: 50, [STAGE2]: 20 } },
  { account_id: "a2", account_name: "Centro Barcelona", country: "ES", city: "Barcelona",
    data: { [SCREENED]: 60, [STAGE1]: 30 } },
  { account_id: "a3", account_name: "Centro Milano",    country: "IT", city: "Milano",
    data: { [SCREENED]: 40 } },
  { account_id: "a4", account_name: "Centro Roma",      country: "IT", city: "Roma",   data: {} },
  { account_id: "a5", account_name: "Centro Paris",     country: "FR", city: "Paris",  data: {} },
];

const COUNT_METRIC_LABEL = "Número de centros";

/** Despliega el modal de gráficos desde la barra de herramientas del Explorer. */
async function openChartModal(page: Page) {
  await page.locator(S.EXPLORER_BTN_CHART).click();
  const modal = page.locator(S.CHART_MODAL);
  await expect(modal).toBeVisible();
  return modal;
}

test.describe("Explorer — modal de gráficos", () => {
  test.beforeEach(async ({ page }) => {
    // Evita el overlay de sesión caducada, que se come los clicks.
    await page.route("**/api/salesforce/me", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: true }) })
    );
    await page.route("**/api/salesforce/map/bootstrap", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );
    // OJO: `getExplorerFields` lee `raw.fields`, no un array pelado. Con el array
    // pelado el catálogo quedaba VACÍO y el constructor de ejes se caía a
    // country/"Count (rows)" — nunca llegaba a pintar una métrica de verdad.
    await page.route("**/api/explorer/fields", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          fields: [
            { key: "sf.Account.Name", label: "Account Name", type: "string", source: "sf" },
            { key: "site.country", label: "Country", type: "string", source: "site" },
            { key: SCREENED, label: "Screened", type: "number", source: "sf" },
          ],
        }),
      })
    );
    // Columnas visibles del usuario: Account Name + cribados. Así el constructor
    // ofrece la métrica y usa el camino fila-a-fila (una barra por centro).
    await page.addInitScript((screened) => {
      localStorage.setItem("explorer:visibleColumns", JSON.stringify(["sf.Account.Name", screened]));
    }, SCREENED);
    await page.route("**/api/explorer/search", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ points: [], rows: ROWS, total: ROWS.length }),
      })
    );
    await page.goto("/explorer");
    // El botón Chart está deshabilitado mientras no haya filas: esperar a que se
    // habilite es esperar a que las filas mockeadas hayan llegado.
    await expect(page.locator(S.EXPLORER_BTN_CHART)).toBeEnabled();
  });

  test("CHART-1: abre en la pestaña Países, no en el constructor de ejes", async ({ page }) => {
    const modal = await openChartModal(page);

    // Las cinco pestañas están siempre visibles: lo que distingue a la activa es
    // aria-selected. Aseverar "es visible" no probaría nada.
    await expect(modal.locator(S.CHART_TAB_COUNTRIES)).toHaveAttribute("aria-selected", "true");
    for (const tab of [S.CHART_TAB_RANKING, S.CHART_TAB_DISTRIBUTION, S.CHART_TAB_FUNNEL, S.CHART_TAB_CUSTOM]) {
      await expect(modal.locator(tab)).toHaveAttribute("aria-selected", "false");
    }
    // El constructor de ejes de siempre NO es lo primero que se ve.
    await expect(modal.locator(S.CHART_CUSTOM_BUILDER)).toHaveCount(0);
  });

  test("CHART-2: la cobertura está presente y CAMBIA al cambiar de métrica", async ({ page }) => {
    const modal = await openChartModal(page);
    const coverage = modal.locator(S.CHART_COVERAGE);

    // 3 de los 5 centros del fixture reportan cribados; los otros 2 quedan
    // excluidos, no contados como cero.
    await expect(coverage).toContainText("3 de 5 centros reportan");
    await expect(coverage).toContainText("2 sin dato, excluidos");
    const screenedText = await coverage.innerText();

    // Stage 2 solo lo reporta 1 centro: la cobertura tiene que reflejarlo.
    await modal.locator(S.CHART_METRIC_SELECT).selectOption(STAGE2);
    await expect(coverage).toContainText("1 de 5 centros reportan");
    await expect(coverage).toContainText("4 sin dato, excluidos");

    // Y Stage 1, 2 centros. La línea se mueve con la métrica: si se quedara
    // clavada, el usuario leería el gráfico creyendo que cubre los 5 centros.
    await modal.locator(S.CHART_METRIC_SELECT).selectOption(STAGE1);
    await expect(coverage).toContainText("2 de 5 centros reportan");
    expect(await coverage.innerText()).not.toBe(screenedText);
  });

  test("CHART-3: Ranking y Distribución NO ofrecen \"Número de centros\"", async ({ page }) => {
    const modal = await openChartModal(page);

    // Rankear centros por "número de centros" da una lista de unos, y un Pareto
    // de esa métrica es una recta: ofrecerla no significa nada.
    for (const tab of [S.CHART_TAB_RANKING, S.CHART_TAB_DISTRIBUTION]) {
      await modal.locator(tab).click();
      const options = await modal.locator(S.CHART_METRIC_SELECT).locator("option").allInnerTexts();
      expect(options.length).toBeGreaterThan(0);
      expect(options).not.toContain(COUNT_METRIC_LABEL);
    }

    // En Países sí significa algo (contar centros por país) y ahí sigue estando:
    // la exclusión de arriba es deliberada, no una opción que falte en todas partes.
    await modal.locator(S.CHART_TAB_COUNTRIES).click();
    const countryOptions = await modal.locator(S.CHART_METRIC_SELECT).locator("option").allInnerTexts();
    expect(countryOptions).toContain(COUNT_METRIC_LABEL);
  });

  test("CHART-4: la pestaña Personalizado sigue llegando al constructor de siempre", async ({ page }) => {
    const modal = await openChartModal(page);

    await modal.locator(S.CHART_TAB_CUSTOM).click();
    await expect(modal.locator(S.CHART_TAB_CUSTOM)).toHaveAttribute("aria-selected", "true");
    await expect(modal.locator(S.CHART_CUSTOM_BUILDER)).toBeVisible();
    // Y es el constructor de ejes: su selector de tipo de gráfico sigue ahí.
    await expect(modal.getByText("Type", { exact: true })).toBeVisible();
  });

  test("CHART-5: Personalizado excluye al que no reporta, no lo cuenta como cero", async ({ page }) => {
    const modal = await openChartModal(page);
    await modal.locator(S.CHART_TAB_CUSTOM).click();

    // La Y por defecto es la única columna numérica visible: cribados, que solo
    // reportan 3 de los 5 centros del fixture.
    await expect(modal.locator(S.CHART_CUSTOM_BUILDER)).toBeVisible();

    // El pie es donde el dato ausente se ve: una porción de tamaño cero es
    // invisible pero cuenta como dato (leyenda, total, %). Si el builder volviera
    // a parsear a mano — Number(String(undefined ?? "")) === 0 — o si el pie
    // volviera a normalizar null -> 0, aquí habría 5 porciones y 5 entradas de
    // leyenda en vez de 3. Verificado mutando el código: con el parseo viejo esta
    // aserción da 5.
    await modal.getByRole("button", { name: "Pie", exact: true }).click();
    await expect(modal.locator(S.CHART_PIE_SECTORS)).toHaveCount(3);
    await expect(modal.locator(S.CHART_LEGEND_ITEMS)).toHaveCount(3);

    // Y la nota dice justo eso: hueco, no cero. (El aviso viejo prometía ceros;
    // dejarlo puesto sería mentir en la otra dirección.)
    const note = modal.locator(S.CHART_CUSTOM_NOTE);
    await expect(note).toBeVisible();
    await expect(note).toContainText("hueco");
    await expect(note).not.toContainText("aparece como cero");

    // Y es exclusiva de esta pestaña: las otras cuatro sí declaran su cobertura.
    await modal.locator(S.CHART_TAB_COUNTRIES).click();
    await expect(modal.locator(S.CHART_CUSTOM_NOTE)).toHaveCount(0);
  });

  test("CHART-6: el modal se pinta por encima del botón flotante de Nearby", async ({ page }) => {
    await openChartModal(page);

    // El pill "Nearby panel" es fixed con z-[9050]; si el modal se queda por
    // debajo, el pill flota sobre él y su drawer lo deja inutilizable.
    const z = await page.locator(S.CHART_OVERLAY).evaluate(
      (el) => window.getComputedStyle(el).zIndex
    );
    expect(Number(z)).toBeGreaterThan(9050);
  });

  test("CHART-7: Legend max sobrevive al cambio de pestaña", async ({ page }) => {
    const modal = await openChartModal(page);

    // CustomView se desmonta al cambiar de pestaña: si su estado vive dentro,
    // la elección del usuario se pierde en silencio en cada ida y vuelta.
    await modal.locator(S.CHART_TAB_CUSTOM).click();
    await modal.locator(S.CHART_LEGEND_MAX).selectOption("20");
    await expect(modal.locator(S.CHART_LEGEND_MAX)).toHaveValue("20");

    await modal.locator(S.CHART_TAB_COUNTRIES).click();
    await modal.locator(S.CHART_TAB_CUSTOM).click();
    await expect(modal.locator(S.CHART_LEGEND_MAX)).toHaveValue("20");
  });
});
