import { test, expect, type Page } from "@playwright/test";
import { S } from "../utils/selectors";
import { SCREENED, STAGE1, STAGE2 } from "../../src/lib/chartAggregation";

/**
 * Explorer — las barras del modal se pintan aunque NO lleguen frames.
 *
 * El bug: abrir el modal, cambiar de pestaña, y el área del gráfico en blanco.
 * Ejes, rejilla, leyenda y hasta el `<path>` de la línea acumulada estaban ahí;
 * las marcas de datos, no. En el DOM: `<g class="recharts-bar-rectangle">` SIN
 * `<path>` dentro, y la línea con `stroke-dasharray: 0px, 1115.33px`.
 *
 * La causa raíz no es la pestaña: es que en Recharts la EXISTENCIA de la marca
 * depende de que su animación de entrada haya corrido. La barra se dibuja
 * interpolando su alto de 0 a su valor, y `<Rectangle>` devuelve `null` cuando
 * el alto es 0 — así que a t=0 el grupo queda hueco. Esa interpolación la mueve
 * react-smooth con `requestAnimationFrame` (`react-smooth/setRafTimeout.js`), y
 * rAF NO se dispara en un tab de fondo, ocluido o con el hilo principal
 * saturado. Sin frames, la animación se queda clavada en t=0 y no hay marcas.
 * El contenedor mide bien (`.recharts-surface` a 1120px) y React sigue pintando
 * lo demás — su scheduler usa MessageChannel, no rAF: por eso los ejes sí salen.
 *
 * El modal viejo tenía UN gráfico siempre montado: animaba una vez, al abrir, y
 * no volvía a exponerse. El contenedor con pestañas re-monta un gráfico en CADA
 * cambio de pestaña, así que reabre esa ventana una y otra vez.
 *
 * La cura es quitarle a la marca su dependencia del frame
 * (`isAnimationActive={false}`), y este test lo pinea del único modo que no es
 * teatro: MATANDO rAF. Con frames normales el bug no reproduce ni en headless
 * (CHART-10 de `charts.spec.ts` pasaba en verde con el código roto). Sin frames,
 * un gráfico que dependa de la animación no pinta NADA.
 *
 * Y se mide GEOMETRÍA, no presencia: contar `.recharts-bar-rectangle` da verde
 * contra un panel vacío, porque los grupos siempre están.
 */
const ROWS = [
  { account_id: "a1", account_name: "Centro Madrid", country: "ES", city: "Madrid",
    data: { [SCREENED]: 100, [STAGE1]: 50, [STAGE2]: 20 } },
  { account_id: "a2", account_name: "Centro Barcelona", country: "ES", city: "Barcelona",
    data: { [SCREENED]: 60, [STAGE1]: 30 } },
  { account_id: "a3", account_name: "Centro Milano", country: "IT", city: "Milano",
    data: { [SCREENED]: 40 } },
  { account_id: "a4", account_name: "Centro Roma", country: "IT", city: "Roma", data: {} },
  { account_id: "a5", account_name: "Centro Paris", country: "FR", city: "Paris", data: {} },
  // Cribó a CERO y lo reportó: no dibuja barra (alto 0) ni con la animación sana.
  { account_id: "a6", account_name: "Centro Lisboa", country: "PT", city: "Lisboa",
    data: { [SCREENED]: 0 } },
];

/** Marcas de datos con caja real. `painted` es lo que el usuario VE. */
type BarGeometry = { groups: number; painted: number };

async function readBarGeometry(page: Page): Promise<BarGeometry> {
  return page.evaluate(() => {
    const groups = Array.from(document.querySelectorAll(".recharts-bar-rectangle"));
    const painted = groups
      .map((g) => g.querySelector<SVGGraphicsElement>("path, rect"))
      .filter((shape): shape is SVGGraphicsElement => shape !== null)
      .map((shape) => shape.getBBox())
      .filter((box) => box.width > 0 && box.height > 0);
    return { groups: groups.length, painted: painted.length };
  });
}

/** Cada pestaña con las barras de valor > 0 que su fixture debe pintar. */
const TABS: Array<{ tab: string; label: string; positiveMarks: number }> = [
  // ES=160, IT=40. El 0 de PT (Lisboa) no dibuja barra: alto 0.
  { tab: S.CHART_TAB_COUNTRIES, label: "Countries", positiveMarks: 2 },
  // Madrid 100, Barcelona 60, Milano 40 (Lisboa 0).
  { tab: S.CHART_TAB_RANKING, label: "Ranking", positiveMarks: 3 },
  { tab: S.CHART_TAB_DISTRIBUTION, label: "Distribution", positiveMarks: 3 },
  // Solo Madrid reporta las tres etapas: 100 → 50 → 20.
  { tab: S.CHART_TAB_FUNNEL, label: "Funnel", positiveMarks: 3 },
  // Constructor fila-a-fila: una barra por centro que reporta cribados.
  { tab: S.CHART_TAB_CUSTOM, label: "Custom", positiveMarks: 3 },
];

test.describe("Explorer — el gráfico pinta sin depender de la animación", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/auth/me", (route) =>
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
    await page.addInitScript((screened) => {
      localStorage.setItem("explorer:visibleColumns", JSON.stringify(["sf.Account.Name", screened]));
    }, SCREENED);

    // El tab sin frames. Se instala ANTES del goto: basta con que rAF no entregue
    // frames para dejar clavada la animación de react-smooth en su primer paso.
    await page.addInitScript(() => {
      window.requestAnimationFrame = (() => 0) as unknown as typeof window.requestAnimationFrame;
    });

    await page.goto("/explorer");
    await expect(page.locator(S.EXPLORER_BTN_CHART)).toBeEnabled();
  });

  test("CHART-11: las cinco pestañas pintan sus barras aunque no lleguen frames", async ({ page }) => {
    await page.locator(S.EXPLORER_BTN_CHART).click();
    const modal = page.locator(S.CHART_MODAL);
    await expect(modal).toBeVisible();

    for (const { tab, label, positiveMarks } of TABS) {
      await modal.locator(tab).click();
      await expect(modal.locator(tab)).toHaveAttribute("aria-selected", "true");

      // `poll` NO es una concesión a la animación — está apagada. Es que
      // `ResponsiveContainer` no pinta nada hasta que su ResizeObserver le dice
      // cuánto mide, y ese primer render vacío es un estado legítimo de un par de
      // ticks. Lo que el poll NO puede rescatar es una animación congelada: sin
      // frames se queda en t=0 para siempre, y con el bug esto sigue en 0 tras el
      // timeout entero (verificado revirtiendo el fix).
      await expect
        .poll(async () => (await readBarGeometry(page)).painted, {
          message: `pestaña ${label}: barras con caja real`,
          timeout: 5_000,
        })
        .toBe(positiveMarks);

      // Y el grupo hueco es justo lo que un test de presencia cuenta como verde
      // mientras el usuario mira un panel vacío: por eso lo de arriba mide cajas.
      const { groups } = await readBarGeometry(page);
      expect(groups, `pestaña ${label}: grupos de barra`).toBeGreaterThanOrEqual(positiveMarks);
    }

    // Y la vuelta a Países — el gesto que destapó el bug: la primera pestaña se
    // pintaba al abrir y se quedaba en blanco al volver de otra.
    await modal.locator(S.CHART_TAB_COUNTRIES).click();
    await expect
      .poll(async () => (await readBarGeometry(page)).painted, { timeout: 5_000 })
      .toBe(2);
  });

  test("CHART-12: la línea acumulada del Pareto se dibuja entera sin frames", async ({ page }) => {
    await page.locator(S.EXPLORER_BTN_CHART).click();
    const modal = page.locator(S.CHART_MODAL);
    await modal.locator(S.CHART_TAB_DISTRIBUTION).click();

    const line = modal.locator(".recharts-line-curve").first();
    await expect(line).toHaveAttribute("d", /^M/);

    // El síntoma exacto: el path existe, con su `d` bien calculado, pero la
    // animación de trazado se queda en "0px, <largo>" — cero píxeles dibujados.
    // Sin animación no hay dasharray que interpolar: "none".
    const dashArray = await line.evaluate((el) => window.getComputedStyle(el).strokeDasharray);
    expect(dashArray).not.toMatch(/^0px/);
    expect(dashArray === "none" || Number.parseFloat(dashArray) > 0).toBe(true);
  });
});
