import { describe, it, expect } from "vitest";
import { toFunnelSteps, formatCount, formatPct } from "./FunnelView";

/**
 * La aritmética del embudo, aparte del render: es lo único que el usuario lee
 * cuando las barras absolutas son slivers. Si la conversión entre etapas miente,
 * el rediseño entero no sirve para nada.
 */
describe("toFunnelSteps", () => {
  const REAL = [
    { stage: "Cribados", value: 22000 },
    { stage: "Stage 1 seguidos", value: 512 },
    { stage: "Stage 2 seguidos", value: 380 },
  ];

  it("la primera etapa es el punto de partida: 100 % de sí misma", () => {
    const [first] = toFunnelSteps(REAL);
    expect(first.retentionPct).toBe(100);
    expect(first.ofFirstPct).toBe(100);
    expect(first.barPct).toBe(100);
  });

  it("cada etapa retiene un % de la ANTERIOR, no del total", () => {
    const [, stage1, stage2] = toFunnelSteps(REAL);
    expect(stage1.retentionPct).toBeCloseTo(2.3272, 3); // 512 / 22000
    expect(stage2.retentionPct).toBeCloseTo(74.2187, 3); // 380 / 512
  });

  it("también dice qué fracción de la cohorte inicial sobrevive a cada etapa", () => {
    const [, stage1, stage2] = toFunnelSteps(REAL);
    expect(stage1.ofFirstPct).toBeCloseTo(2.3272, 3);
    expect(stage2.ofFirstPct).toBeCloseTo(1.7272, 3); // 380 / 22000
  });

  it("la barra se dibuja en la escala de retención, que es la legible", () => {
    // En la escala absoluta 380 sobre 22.000 es un sliver invisible; en la de
    // retención es una barra del 74 % — la relación Stage 1 → Stage 2 se lee.
    const steps = toFunnelSteps(REAL);
    expect(steps.map((s) => s.barPct)).toEqual([
      100,
      steps[1].retentionPct,
      steps[2].retentionPct,
    ]);
  });

  it("un cero legítimo es un 0 %, no un hueco", () => {
    const steps = toFunnelSteps([
      { stage: "Cribados", value: 50 },
      { stage: "Stage 1 seguidos", value: 0 },
      { stage: "Stage 2 seguidos", value: 0 },
    ]);
    expect(steps[1].value).toBe(0);
    expect(steps[1].retentionPct).toBe(0);
    expect(steps[1].barPct).toBe(0);
  });

  it("sin base (etapa anterior a 0) la retención es null, nunca NaN ni Infinity", () => {
    // 0/0 = NaN y 3/0 = Infinity: los dos pintarían basura en el eje.
    const steps = toFunnelSteps([
      { stage: "Cribados", value: 0 },
      { stage: "Stage 1 seguidos", value: 0 },
      { stage: "Stage 2 seguidos", value: 3 },
    ]);
    expect(steps[0].retentionPct).toBeNull();
    expect(steps[1].retentionPct).toBeNull();
    expect(steps[2].retentionPct).toBeNull();
    expect(steps.map((s) => s.barPct)).toEqual([0, 0, 0]);
  });

  it("una etapa que crece por encima de la anterior pasa de 100 %, no se recorta", () => {
    // Dato corrupto pero real: recortarlo a 100 % lo escondería.
    const steps = toFunnelSteps([
      { stage: "Cribados", value: 10 },
      { stage: "Stage 1 seguidos", value: 15 },
      { stage: "Stage 2 seguidos", value: 15 },
    ]);
    expect(steps[1].retentionPct).toBe(150);
    expect(steps[1].barPct).toBe(150);
  });
});

describe("formato es-ES", () => {
  it("los recuentos llevan separador de millar", () => {
    expect(formatCount(22000)).toBe("22.000");
    expect(formatCount(0)).toBe("0");
  });

  it("los porcentajes llevan coma decimal y como mucho un decimal", () => {
    expect(formatPct(2.3272)).toBe("2,3 %");
    expect(formatPct(100)).toBe("100 %");
    expect(formatPct(0)).toBe("0 %");
  });

  it("sin base no hay porcentaje que enseñar", () => {
    expect(formatPct(null)).toBe("sin base");
  });
});
