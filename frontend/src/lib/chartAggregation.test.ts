import { describe, it, expect } from "vitest";
import {
  toMetricValue, coverageFor, groupByCountry,
  topN, bottomN, distribution, funnel,
  COUNT_METRIC, SCREENED, STAGE1, STAGE2,
} from "./chartAggregation";
import type { DataRow } from "./rowAccess";

let seq = 0;
const site = (country: string, screened?: unknown): DataRow => ({
  account_id: `${country}-${seq++}`,
  account_name: `Centro ${country}`,
  country,
  data: screened === undefined ? {} : { [SCREENED]: screened },
});

describe("toMetricValue", () => {
  it("parsea números y strings numéricas con comas", () => {
    expect(toMetricValue(240)).toBe(240);
    expect(toMetricValue("1,240")).toBe(1240);
  });

  it("trata ausente, vacío, no numérico y negativo como null (nunca 0)", () => {
    expect(toMetricValue(undefined)).toBeNull();
    expect(toMetricValue(null)).toBeNull();
    expect(toMetricValue("")).toBeNull();
    expect(toMetricValue("n/a")).toBeNull();
    expect(toMetricValue(-5)).toBeNull();
  });

  it("conserva el cero legítimo", () => {
    expect(toMetricValue(0)).toBe(0);
  });
});

describe("coverageFor", () => {
  it("cuenta cuántos centros reportan la métrica", () => {
    const rows = [site("ES", 10), site("ES"), site("IT", 0), site("IT")];
    expect(coverageFor(rows, SCREENED)).toEqual({ withData: 2, total: 4, missing: 2 });
  });

  it("__count__ siempre tiene cobertura total", () => {
    const rows = [site("ES"), site("IT")];
    expect(coverageFor(rows, COUNT_METRIC)).toEqual({ withData: 2, total: 2, missing: 0 });
  });
});

describe("groupByCountry", () => {
  it("suma por país y NO deja que un centro sin dato contribuya 0", () => {
    const rows = [site("ES", 10), site("ES", 5), site("ES"), site("IT", 100)];
    const out = groupByCountry(rows, SCREENED);
    expect(out).toEqual([
      { country: "IT", value: 100, sites: 1 },
      { country: "ES", value: 15, sites: 2 }, // sites = 2, NO 3: el que no reporta no cuenta
    ]);
  });

  it("omite por completo los países donde nadie reporta", () => {
    const rows = [site("ES", 10), site("IT"), site("IT")];
    const out = groupByCountry(rows, SCREENED);
    expect(out.map(b => b.country)).toEqual(["ES"]);
  });

  it("con __count__ cuenta centros, incluidos los que no reportan métricas", () => {
    const rows = [site("ES", 10), site("ES"), site("IT")];
    const out = groupByCountry(rows, COUNT_METRIC);
    expect(out).toEqual([
      { country: "ES", value: 2, sites: 2 },
      { country: "IT", value: 1, sites: 1 },
    ]);
  });

  it("ordena de mayor a menor valor", () => {
    const rows = [site("ES", 1), site("IT", 50), site("FR", 10)];
    expect(groupByCountry(rows, SCREENED).map(b => b.country)).toEqual(["IT", "FR", "ES"]);
  });

  it("devuelve array vacío sin filas", () => {
    expect(groupByCountry([], SCREENED)).toEqual([]);
  });

  it("agrupa bajo \"(sin país)\" cuando el país está vacío o ausente", () => {
    const rows: DataRow[] = [
      { account_id: "x-1", account_name: "Centro X", country: "", data: { [SCREENED]: 10 } },
      { account_id: "x-2", account_name: "Centro Y", data: { [SCREENED]: 5 } },
    ];
    const out = groupByCountry(rows, SCREENED);
    expect(out).toEqual([{ country: "(sin país)", value: 15, sites: 2 }]);
  });
});

const named = (name: string, screened?: unknown): DataRow => ({
  account_id: name, account_name: name, country: "ES",
  data: screened === undefined ? {} : { [SCREENED]: screened },
});

describe("topN / bottomN", () => {
  it("topN devuelve los N mayores, de mayor a menor", () => {
    const rows = [named("a", 5), named("b", 50), named("c", 20)];
    expect(topN(rows, SCREENED, 2)).toEqual([
      { accountId: "b", name: "b", value: 50 },
      { accountId: "c", name: "c", value: 20 },
    ]);
  });

  it("bottomN devuelve los N menores, de menor a mayor", () => {
    const rows = [named("a", 5), named("b", 50), named("c", 20)];
    expect(bottomN(rows, SCREENED, 2)).toEqual([
      { accountId: "a", name: "a", value: 5 },
      { accountId: "c", name: "c", value: 20 },
    ]);
  });

  it("los centros sin dato NUNCA aparecen en el bottom como si fueran cero", () => {
    const rows = [named("reporta", 5), named("silencioso")];
    expect(bottomN(rows, SCREENED, 5).map(s => s.name)).toEqual(["reporta"]);
  });

  it("devuelve menos de N si no hay suficientes centros con dato", () => {
    expect(topN([named("a", 1)], SCREENED, 10)).toHaveLength(1);
  });

  it("sin account_id, accountId cae al account_name", () => {
    const rows: DataRow[] = [
      { account_name: "Centro sin id", country: "ES", data: { [SCREENED]: 3 } },
    ];
    expect(topN(rows, SCREENED, 1)).toEqual([
      { accountId: "Centro sin id", name: "Centro sin id", value: 3 },
    ]);
  });

  it("sin account_id ni account_name, accountId es \"\" y el nombre es \"(sin nombre)\"", () => {
    const rows: DataRow[] = [{ country: "ES", data: { [SCREENED]: 3 } }];
    expect(topN(rows, SCREENED, 1)).toEqual([
      { accountId: "", name: "(sin nombre)", value: 3 },
    ]);
  });
});

describe("distribution", () => {
  it("ordena descendente y calcula el % acumulado", () => {
    const rows = [named("a", 50), named("b", 30), named("c", 20)];
    const out = distribution(rows, SCREENED);
    expect(out.bars.map(b => b.name)).toEqual(["a", "b", "c"]);
    expect(out.cumulativePct).toEqual([50, 80, 100]);
  });

  it("cuenta aparte los centros sin dato, sin meterlos en las barras", () => {
    const rows = [named("a", 100), named("mudo1"), named("mudo2")];
    const out = distribution(rows, SCREENED);
    expect(out.bars).toHaveLength(1);
    expect(out.missingSites).toBe(2);
  });

  it("con un solo centro el acumulado es 100%", () => {
    const out = distribution([named("a", 7)], SCREENED);
    expect(out.cumulativePct).toEqual([100]);
  });

  it("no divide por cero cuando el total es 0", () => {
    const out = distribution([named("a", 0)], SCREENED);
    expect(out.cumulativePct).toEqual([0]);
    expect(out.bars).toHaveLength(1);
  });

  it("sin centros con dato devuelve barras vacías", () => {
    const out = distribution([named("mudo")], SCREENED);
    expect(out.bars).toEqual([]);
    expect(out.missingSites).toBe(1);
  });
});

describe("funnel", () => {
  const full = (name: string, s: number, s1: number, s2: number): DataRow => ({
    account_id: name, account_name: name, country: "ES",
    data: { [SCREENED]: s, [STAGE1]: s1, [STAGE2]: s2 },
  });

  it("suma solo los centros que reportan las TRES métricas", () => {
    const rows = [full("completo", 100, 10, 2), named("parcial", 999)];
    const out = funnel(rows);
    expect(out.stages).toEqual([
      { stage: "Cribados", value: 100 },
      { stage: "Stage 1 seguidos", value: 10 },
      { stage: "Stage 2 seguidos", value: 2 },
    ]);
    expect(out.sitesIncluded).toBe(1);
    expect(out.sitesExcluded).toBe(1);
  });

  it("incluye al centro que reporta CEROS legítimos en las etapas tardías", () => {
    // Cribó a 50 personas y no siguió a ninguna: reporta las tres métricas,
    // así que entra. Un filtro por truthiness lo borraría del embudo — y es
    // justo el centro que más interesa ver.
    const rows = [full("cribó pero no siguió", 50, 0, 0)];
    const out = funnel(rows);
    expect(out.sitesIncluded).toBe(1);
    expect(out.sitesExcluded).toBe(0);
    expect(out.stages).toEqual([
      { stage: "Cribados", value: 50 },
      { stage: "Stage 1 seguidos", value: 0 },
      { stage: "Stage 2 seguidos", value: 0 },
    ]);
  });

  it("sin ningún centro completo, las etapas van a 0 y sitesIncluded es 0", () => {
    const out = funnel([named("parcial", 999)]);
    expect(out.sitesIncluded).toBe(0);
    expect(out.sitesExcluded).toBe(1);
    expect(out.stages.every(s => s.value === 0)).toBe(true);
  });
});
