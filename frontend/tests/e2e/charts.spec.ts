import { test, expect, type Page } from "@playwright/test";
import { S } from "../utils/selectors";
import { SCREENED, STAGE1, STAGE2 } from "../../src/lib/chartAggregation";

/**
 * Explorer — modal de gráficos con pestañas.
 *
 * El punto del rediseño: la mayoría de los centros NO reportan las métricas de
 * reclutamiento. Las vistas EXCLUYEN al que no reporta en vez de contarlo como
 * cero, y la línea de cobertura es lo único que impide leer mal el gráfico. Por
 * eso el fixture es deliberadamente desigual: 4 de 6 centros reportan cribados,
 * 2 reportan Stage 1 y solo 1 reporta Stage 2. Si alguien "arregla" la
 * agregación rellenando los huecos con ceros, la cobertura deja de moverse al
 * cambiar de métrica y CHART-2 se pone rojo.
 *
 * Y hay DOS clases de centro que un parser descuidado confunde: Lisboa reporta
 * un CERO de verdad (cribó a nadie y lo dijo) y Roma/Paris no reportan nada. La
 * regla entera del rediseño es que no son lo mismo, así que el cero legítimo
 * tiene que estar en el fixture: sin él, una sobre-corrección que tirase los
 * ceros reales pasaría CHART-5 en verde.
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
  // Cribó a CERO personas y lo reportó: es un dato, no un hueco.
  { account_id: "a6", account_name: "Centro Lisboa",    country: "PT", city: "Lisboa",
    data: { [SCREENED]: 0 } },
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

    // 4 de los 6 centros del fixture reportan cribados — Lisboa reporta un 0, que
    // es reportar. Los otros 2 quedan excluidos, no contados como cero.
    await expect(coverage).toContainText("4 de 6 centros reportan");
    await expect(coverage).toContainText("2 sin dato, excluidos");
    const screenedText = await coverage.innerText();

    // Stage 2 solo lo reporta 1 centro: la cobertura tiene que reflejarlo.
    await modal.locator(S.CHART_METRIC_SELECT).selectOption(STAGE2);
    await expect(coverage).toContainText("1 de 6 centros reportan");
    await expect(coverage).toContainText("5 sin dato, excluidos");

    // Y Stage 1, 2 centros. La línea se mueve con la métrica: si se quedara
    // clavada, el usuario leería el gráfico creyendo que cubre los 6 centros.
    await modal.locator(S.CHART_METRIC_SELECT).selectOption(STAGE1);
    await expect(coverage).toContainText("2 de 6 centros reportan");
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
    // volviera a normalizar null -> 0, aquí habría 6 porciones y 6 entradas de
    // leyenda en vez de 4. Verificado mutando el código: con el parseo viejo esta
    // aserción da 6.
    //
    // Y son 4, no 3: Lisboa reporta un CERO legítimo y ES una porción (aunque su
    // ángulo sea nulo, existe en el DOM y en la leyenda). Los dos que no reportan
    // nada — Roma y Paris — no están. Esa es la distinción que el fixture pinea:
    // tirar los ceros reales junto con los huecos daría 3 aquí.
    await modal.getByRole("button", { name: "Pie", exact: true }).click();
    await expect(modal.locator(S.CHART_PIE_SECTORS)).toHaveCount(4);
    await expect(modal.locator(S.CHART_LEGEND_ITEMS)).toHaveCount(4);

    const legend = await modal.locator(S.CHART_LEGEND_ITEMS).allInnerTexts();
    expect(legend).toContain("Centro Lisboa");   // reportó 0 → se pinta
    expect(legend).not.toContain("Centro Roma"); // no reportó → hueco, no porción
    expect(legend).not.toContain("Centro Paris");

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

  test("CHART-8: los gráficos agregan las filas que la tabla filtra, no el resultado entero", async ({ page }) => {
    // El Explorer filtra en CLIENTE encima del resultado del servidor: la caja de
    // búsqueda global y los filtros por columna estrechan la tabla sin volver a
    // pedir nada. Si el modal recibe el array de respaldo completo, agrega centros
    // que el usuario acaba de filtrar FUERA y no puede ver, y la cobertura anuncia
    // una población que no es la de la tabla.
    //
    // "IT" solo aparece en la columna country (ES/IT/FR/PT) — ningún nombre ni
    // ciudad del fixture contiene "it". Deja Milano (reporta 40 cribados) y Roma
    // (no reporta): 2 filas, cobertura 1 de 2.
    await page.locator(S.EXPLORER_GLOBAL_SEARCH).fill("IT");
    await expect(page.locator(S.EXPLORER_RESULTS_COUNT)).toContainText("2 results");

    // El badge del botón anuncia la población que el modal va a graficar: si
    // dijera 6 estaría prometiendo el resultado entero.
    await expect(page.locator(S.EXPLORER_BTN_CHART)).toContainText("2");

    const modal = await openChartModal(page);
    const coverage = modal.locator(S.CHART_COVERAGE);

    // Con el bug: "4 de 6 centros reportan" — los 6 del resultado del servidor.
    await expect(coverage).toContainText("1 de 2 centros reportan");
    await expect(coverage).toContainText("1 sin dato, excluidos");
    await expect(coverage).not.toContainText("de 6 centros");
  });

  test("CHART-9: el constructor Personalizado también grafica solo las filas filtradas", async ({ page }) => {
    // Misma regla para la quinta pestaña: buildChartDataset se alimenta del mismo
    // conjunto. Con el bug pintaba una barra por cada centro del resultado entero.
    await page.locator(S.EXPLORER_GLOBAL_SEARCH).fill("IT");
    await expect(page.locator(S.EXPLORER_RESULTS_COUNT)).toContainText("2 results");

    const modal = await openChartModal(page);
    await modal.locator(S.CHART_TAB_CUSTOM).click();
    await modal.getByRole("button", { name: "Pie", exact: true }).click();

    // De los 2 centros filtrados solo Milano reporta cribados → 1 porción.
    // Con el bug: 4 (Madrid, Barcelona, Milano y el cero legítimo de Lisboa).
    await expect(modal.locator(S.CHART_PIE_SECTORS)).toHaveCount(1);
    const legend = await modal.locator(S.CHART_LEGEND_ITEMS).allInnerTexts();
    expect(legend).toEqual(["Centro Milano"]);
  });

  /**
   * CHART-10 — el gráfico de cada pestaña PINTA, no solo existe.
   *
   * La trampa que dejó pasar el bug de las pestañas en blanco: los `<g
   * class="recharts-bar-rectangle">` SIEMPRE están en el DOM (uno por dato),
   * estén pintados o no. Contarlos da verde mientras el usuario mira un panel
   * vacío. La barra la pinta un `<path>` HIJO de ese grupo, y Recharts NO lo
   * renderiza cuando su alto (o ancho) es 0 — que es justo el estado en el que
   * se queda una animación de entrada congelada en t=0.
   *
   * Por eso esto mide GEOMETRÍA (getBBox del hijo), no presencia. Y por eso
   * `positiveMarks` es el número de barras con valor > 0 del fixture, no el
   * número de grupos: el CERO legítimo de Lisboa no dibuja barra ni con la
   * animación sana. Confundir ambos números convertiría el test en teatro en la
   * otra dirección.
   */
  type BarGeometry = { groups: number; painted: number; heights: number[]; widths: number[] };

  async function readBarGeometry(page: Page): Promise<BarGeometry> {
    return page.evaluate(() => {
      const groups = Array.from(document.querySelectorAll(".recharts-bar-rectangle"));
      const boxes = groups
        .map((g) => g.querySelector<SVGGraphicsElement>("path, rect"))
        .filter((shape): shape is SVGGraphicsElement => shape !== null)
        .map((shape) => shape.getBBox())
        .filter((box) => box.width > 0 && box.height > 0);
      return {
        groups: groups.length,
        painted: boxes.length,
        heights: boxes.map((b) => b.height),
        widths: boxes.map((b) => b.width),
      };
    });
  }

  /** Cada pestaña, con las barras de valor > 0 que su fixture debe pintar. */
  const PAINTED_BARS: Array<{ tab: string; label: string; positiveMarks: number }> = [
    // ES=160, IT=40 y PT=0 (el cero de Lisboa no dibuja barra).
    { tab: S.CHART_TAB_COUNTRIES, label: "Países", positiveMarks: 2 },
    // Madrid 100, Barcelona 60, Milano 40 (Lisboa 0).
    { tab: S.CHART_TAB_RANKING, label: "Ranking", positiveMarks: 3 },
    { tab: S.CHART_TAB_DISTRIBUTION, label: "Distribución", positiveMarks: 3 },
    // Solo Madrid reporta las tres etapas: 100 → 50 → 20.
    { tab: S.CHART_TAB_FUNNEL, label: "Embudo", positiveMarks: 3 },
    // Constructor fila-a-fila: una barra por centro que reporta cribados.
    { tab: S.CHART_TAB_CUSTOM, label: "Personalizado", positiveMarks: 3 },
  ];

  test("CHART-10: las barras de las cinco pestañas se pintan con geometría real", async ({ page }) => {
    const modal = await openChartModal(page);

    for (const { tab, label, positiveMarks } of PAINTED_BARS) {
      await modal.locator(tab).click();
      await expect(modal.locator(tab)).toHaveAttribute("aria-selected", "true");

      // `poll` y no una lectura seca: una animación de entrada SANA tarda ~1.5 s
      // en llegar a su tamaño final. Si esto falla es porque las barras nunca
      // aparecen, no porque el test corriese demasiado pronto.
      await expect
        .poll(async () => (await readBarGeometry(page)).painted, {
          message: `pestaña ${label}: barras con geometría real`,
          timeout: 10_000,
        })
        .toBe(positiveMarks);
    }
  });

  test("CHART-10b: volver a Países repinta sus barras, y a escala", async ({ page }) => {
    const modal = await openChartModal(page);

    // La ida y vuelta es el gesto que destapó el bug: Países se pintaba al abrir
    // y se quedaba en blanco al volver de otra pestaña.
    await modal.locator(S.CHART_TAB_DISTRIBUTION).click();
    await modal.locator(S.CHART_TAB_COUNTRIES).click();
    await expect(modal.locator(S.CHART_TAB_COUNTRIES)).toHaveAttribute("aria-selected", "true");

    await expect
      .poll(async () => (await readBarGeometry(page)).painted, { timeout: 10_000 })
      .toBe(2);

    // No basta con que haya barras: tienen que medir lo que el dato dice. ES vale
    // 160 y IT 40, así que la barra de ES mide 4x la de IT. Un montaje con el
    // contenedor a 0x0 podría devolver marcas no vacías pero a una escala que no
    // es la del dato — esta razón lo pilla.
    const { heights, widths } = await readBarGeometry(page);
    const [tallest, shortest] = [...heights].sort((a, b) => b - a);
    expect(tallest / shortest).toBeGreaterThan(3.6);
    expect(tallest / shortest).toBeLessThan(4.4);
    // Y el gráfico ocupa el ancho del modal, no un contenedor colapsado.
    expect(Math.min(...widths)).toBeGreaterThan(10);
  });

  /**
   * CHART-11/12 — el Embudo.
   *
   * En el fixture solo Madrid reporta las tres etapas: 100 → 50 → 20. Con el eje
   * ABSOLUTO viejo esas tres barras medían 100 : 50 : 20 y, con los números de
   * producción (22.000 → 512 → 380), las dos últimas eran slivers de un píxel.
   * Ahora la barra mide la RETENCIÓN de la etapa anterior: 100 % : 50 % : 40 %.
   */
  test("CHART-11: el embudo dice la conversión entre etapas en texto, no solo en barras", async ({ page }) => {
    const modal = await openChartModal(page);
    await modal.locator(S.CHART_TAB_FUNNEL).click();

    // Los absolutos siguen ahí: un embudo sin "cuántos" no sirve clínicamente.
    const counts = await modal.locator(S.CHART_FUNNEL_COUNT).allInnerTexts();
    expect(counts).toEqual(["100", "50", "20"]);

    const conversions = await modal.locator(S.CHART_FUNNEL_CONVERSION).allInnerTexts();
    expect(conversions[0]).toBe("punto de partida");
    expect(conversions[1]).toContain("50 % de la etapa anterior");
    // 20/50 = 40 % de la anterior, pero 20/100 = 20 % de la cohorte inicial. Las
    // DOS cifras importan y son distintas: confundirlas es leer mal el embudo.
    expect(conversions[2]).toContain("40 % de la etapa anterior");
    expect(conversions[2]).toContain("20 % de los cribados");

    // Y la cobertura sigue en primera línea: el embudo suma 1 de los 6 centros.
    const coverage = modal.locator(S.CHART_COVERAGE);
    await expect(coverage).toContainText("1 centros que reportan las tres métricas");
    await expect(coverage).toContainText("5 centros excluidos");
  });

  test("CHART-12: las barras del embudo van en escala de retención, no de absolutos", async ({ page }) => {
    const modal = await openChartModal(page);
    await modal.locator(S.CHART_TAB_FUNNEL).click();

    // Barras HORIZONTALES: lo que codifica el valor es el ancho.
    await expect.poll(async () => (await readBarGeometry(page)).painted, { timeout: 10_000 }).toBe(3);
    const { widths } = await readBarGeometry(page);
    const [first, second, third] = widths;

    // Retención: 100 % → 50 % (50/100) → 40 % (20/50). En la escala ABSOLUTA
    // vieja la tercera barra medía 20/100 = un quinto de la primera y la relación
    // Stage 1 → Stage 2 no se veía. Aquí mide 40/100: esta razón es lo que
    // distingue los dos diseños, y con el eje viejo este test se pone rojo.
    expect(second / first).toBeCloseTo(0.5, 1);
    expect(third / first).toBeCloseTo(0.4, 1);
    // La tercera barra es 4/5 de la segunda: la caída Stage 1 → Stage 2 es LEGIBLE.
    expect(third / second).toBeGreaterThan(0.7);

    // Cada barra lleva pegado su absoluto y su conversión, por si el ojo falla.
    const labels = await modal.locator(".recharts-label-list text").allTextContents(); // SVG: innerText no existe
    expect(labels).toEqual(["100 (100 %)", "50 (50 %)", "20 (40 %)"]);
  });

  test("CHART-10c: la línea acumulada del Pareto se dibuja entera", async ({ page }) => {
    const modal = await openChartModal(page);
    await modal.locator(S.CHART_TAB_DISTRIBUTION).click();

    // El síntoma exacto del bug en la línea: el `<path>` existe, con su `d` bien
    // calculado, pero `stroke-dasharray: 0px, <largo>` — cero píxeles dibujados.
    // Aseverar que el path existe habría dado verde con la línea invisible.
    // Verde = "none" (sin animación) o un primer tramo con largo real.
    const isDrawn = async (): Promise<boolean> => {
      const dashArray = await modal
        .locator(".recharts-line-curve")
        .first()
        .evaluate((el) => window.getComputedStyle(el).strokeDasharray);
      return dashArray === "none" || Number.parseFloat(dashArray) > 0;
    };

    await expect.poll(isDrawn, { timeout: 10_000 }).toBe(true);
  });
});

/**
 * CHART-13 — la razón de ser del rediseño: "no reportó" y "reportó cero" no son
 * lo mismo, y el embudo tiene que enseñarlos distintos.
 *
 * Fixture propio: Oviedo cribó a 50 personas y no siguió a NINGUNA. Sus dos
 * ceros son datos. Berlín no reporta nada: no entra en el embudo (lo excluye la
 * agregación, y la línea de cobertura lo dice).
 */
test.describe("Explorer — el embudo y el cero legítimo", () => {
  const ZERO_ROWS = [
    { account_id: "z1", account_name: "Centro Oviedo", country: "ES", city: "Oviedo",
      data: { [SCREENED]: 50, [STAGE1]: 0, [STAGE2]: 0 } },
    { account_id: "z2", account_name: "Centro Berlin", country: "DE", city: "Berlin", data: {} },
  ];

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
        body: JSON.stringify({ fields: [{ key: SCREENED, label: "Screened", type: "number", source: "sf" }] }),
      })
    );
    await page.route("**/api/explorer/search", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ points: [], rows: ZERO_ROWS, total: ZERO_ROWS.length }),
      })
    );
    await page.goto("/explorer");
    await expect(page.locator(S.EXPLORER_BTN_CHART)).toBeEnabled();
  });

  test("CHART-13: una etapa a cero se pinta como 0, no como un hueco", async ({ page }) => {
    const modal = await openChartModal(page);
    await modal.locator(S.CHART_TAB_FUNNEL).click();

    // El cero está escrito, con todas las letras. Un embudo que se comiera las
    // etapas vacías dejaría al usuario creyendo que "no hay dato" cuando lo que
    // hay es una caída total — la lectura opuesta.
    const counts = await modal.locator(S.CHART_FUNNEL_COUNT).allInnerTexts();
    expect(counts).toEqual(["50", "0", "0"]);

    const conversions = await modal.locator(S.CHART_FUNNEL_CONVERSION).allInnerTexts();
    // 0 de 50 seguidos: un 0 % medido de verdad.
    expect(conversions[1]).toContain("0 % de la etapa anterior");
    // Y de una etapa vacía no se puede calcular fracción: no se inventa un 0 %.
    expect(conversions[2]).toContain("sin base");

    // Etiqueta pegada a la barra de longitud cero: existe aunque la barra no.
    const labels = await modal.locator(".recharts-label-list text").allTextContents(); // SVG: innerText no existe
    expect(labels).toEqual(["50 (100 %)", "0 (0 %)", "0 (sin base)"]);

    // Berlín no reporta: queda FUERA del embudo y la cobertura lo declara. Es la
    // otra mitad de la distinción — el hueco no se cuenta como un cero.
    const coverage = modal.locator(S.CHART_COVERAGE);
    await expect(coverage).toContainText("1 centros que reportan las tres métricas");
    await expect(coverage).toContainText("1 centros excluidos");
  });

  test("CHART-13b: sin ningún centro completo el embudo no dibuja nada y lo dice", async ({ page }) => {
    // "Nadie reporta las tres" no puede parecerse a "todos reportan cero".
    await page.route("**/api/explorer/search", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ points: [], rows: [ZERO_ROWS[1]], total: 1 }),
      })
    );
    await page.reload();
    await expect(page.locator(S.EXPLORER_BTN_CHART)).toBeEnabled();

    const modal = await openChartModal(page);
    await modal.locator(S.CHART_TAB_FUNNEL).click();

    await expect(modal.locator(S.CHART_EMPTY)).toBeVisible();
    await expect(modal.locator(S.CHART_FUNNEL_STEPS)).toHaveCount(0);
  });
});
