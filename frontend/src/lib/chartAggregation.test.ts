import { describe, it, expect } from "vitest";
import {
  toMetricValue, coverageFor, groupByCountry,
  COUNT_METRIC, SCREENED,
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
});
