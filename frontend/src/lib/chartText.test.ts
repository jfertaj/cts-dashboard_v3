import { describe, it, expect } from "vitest";
import { siteCount, rowCount } from "./chartText";

describe("siteCount", () => {
  it("pluraliza a partir de 2", () => {
    expect(siteCount(0)).toBe("0 sites");
    expect(siteCount(2)).toBe("2 sites");
    expect(siteCount(215)).toBe("215 sites");
  });

  it("no dice \"1 sites\": la línea de cobertura la lee un humano", () => {
    expect(siteCount(1)).toBe("1 site");
  });
});

describe("rowCount", () => {
  // El aviso de negativos del pie decía "1 row(s)". El "(s)" es la versión
  // perezosa del mismo bug que siteCount existe para no cometer.
  it("pluraliza a partir de 2", () => {
    expect(rowCount(0)).toBe("0 rows");
    expect(rowCount(2)).toBe("2 rows");
  });

  it("no dice \"1 rows\" ni \"1 row(s)\"", () => {
    expect(rowCount(1)).toBe("1 row");
  });
});
