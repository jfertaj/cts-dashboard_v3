import { describe, it, expect } from "vitest";
import { siteCount } from "./chartText";

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
