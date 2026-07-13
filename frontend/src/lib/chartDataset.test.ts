import { describe, it, expect } from "vitest";
import { buildChartDataset } from "./chartDataset";
import { COUNT_METRIC, SCREENED, STAGE1 } from "./chartAggregation";
import type { DataRow } from "./rowAccess";

const NAME = "sf.Account.Name";

const rows: DataRow[] = [
  { account_id: "a1", account_name: "Madrid",    country: "ES", data: { [SCREENED]: 100, [STAGE1]: 0 } },
  { account_id: "a2", account_name: "Barcelona", country: "ES", data: { [SCREENED]: "1,200" } },
  { account_id: "a3", account_name: "Milano",    country: "IT", data: {} },
];

describe("buildChartDataset — fila a fila", () => {
  it("deja en null al centro que no reporta la métrica, no en cero", () => {
    const out = buildChartDataset(rows, NAME, [SCREENED]);
    expect(out[2]).toEqual({ [NAME]: "Milano", __count__: 1, [SCREENED]: null });
  });

  it("conserva el cero reportado de verdad", () => {
    const out = buildChartDataset(rows, NAME, [STAGE1]);
    expect(out[0][STAGE1]).toBe(0);
    expect(out[1][STAGE1]).toBeNull();
  });

  it("parsea los separadores de millar", () => {
    const out = buildChartDataset(rows, NAME, [SCREENED]);
    expect(out[1][SCREENED]).toBe(1200);
  });

  it("da 1 a __count__ en cada fila", () => {
    const out = buildChartDataset(rows, NAME, [COUNT_METRIC]);
    expect(out.map((r) => r[COUNT_METRIC])).toEqual([1, 1, 1]);
  });
});

describe("buildChartDataset — agrupado por país", () => {
  it("suma solo a los que reportan", () => {
    const out = buildChartDataset(rows, "country", [SCREENED]);
    expect(out.find((r) => r.country === "ES")?.[SCREENED]).toBe(1300);
  });

  it("deja el país sin ningún reportador en null, no en cero", () => {
    const out = buildChartDataset(rows, "country", [SCREENED]);
    expect(out.find((r) => r.country === "IT")?.[SCREENED]).toBeNull();
  });

  it("__count__ sigue contando TODAS las filas del país, reporten o no", () => {
    const out = buildChartDataset(rows, "country", [SCREENED]);
    expect(out.find((r) => r.country === "ES")?.__count__).toBe(2);
    expect(out.find((r) => r.country === "IT")?.__count__).toBe(1);
  });
});
