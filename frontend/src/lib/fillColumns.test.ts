import { describe, it, expect } from "vitest";
import { buildFillColumns, CHART_METRIC_COLUMNS } from "./fillColumns";
import { SCREENED, STAGE1, STAGE2, ASSIGNMENTS, COUNT_METRIC } from "./chartAggregation";

const UNFILLABLE = new Set<string>(["sf.Account.Name"]);

describe("buildFillColumns", () => {
  it("pide las 4 métricas del ChartModal aunque el usuario no tenga ninguna columna visible", () => {
    // El bug: con las columnas por defecto (Account Name, que ni siquiera es
    // rellenable) reqCols salía VACÍO, no se llamaba a /columns/fill y las
    // métricas quedaban null en las 215 filas — el gráfico anunciaba
    // "0 de 215 centros reportan" cuando la verdad era "nadie las pidió".
    const cols = buildFillColumns(["sf.Account.Name"], UNFILLABLE);

    expect(cols).toContain(SCREENED);
    expect(cols).toContain(STAGE1);
    expect(cols).toContain(STAGE2);
    expect(cols).toContain(ASSIGNMENTS);
  });

  it("conserva las columnas visibles del usuario", () => {
    const cols = buildFillColumns(["qual.2_2__foo", "sf.StageName"], UNFILLABLE);

    expect(cols).toContain("qual.2_2__foo");
    expect(cols).toContain("sf.StageName");
  });

  it("excluye las columnas no rellenables", () => {
    expect(buildFillColumns(["sf.Account.Name"], UNFILLABLE)).not.toContain("sf.Account.Name");
  });

  it("no duplica una métrica que además es columna visible", () => {
    const cols = buildFillColumns([SCREENED], UNFILLABLE);

    expect(cols.filter((c) => c === SCREENED)).toHaveLength(1);
  });

  it("no pide COUNT_METRIC: es una pseudo-métrica sin dato en el servidor", () => {
    // `valueOf` la resuelve con un 1 constante. Pedírsela al backend devolvería
    // null y ensuciaría el payload del fill con una columna que no existe.
    expect(CHART_METRIC_COLUMNS).not.toContain(COUNT_METRIC);
  });
});
