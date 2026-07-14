import { describe, it, expect } from "vitest";
import {
  toFunnelSteps, funnelAxisMax, formatCount, formatPct, conversionText,
} from "./FunnelView";

/**
 * La aritmética del embudo, aparte del render.
 *
 * El contrato es doble y hay que probar las dos mitades:
 *  - GEOMETRÍA: la barra mide el ABSOLUTO. El embudo encoge cuando el dato
 *    encoge. Codificar la retención (100 % → 2,3 % → 74 %) pintaba la tercera
 *    barra 30 veces más larga que la segunda cuando en realidad hay MENOS gente
 *    en ella: un embudo que aparenta crecer. Ese encoding está prohibido.
 *  - TEXTO: las dos conversiones (de la etapa anterior y de la cohorte inicial).
 *    Es lo único que hace legible el salto Stage 1 → Stage 2 cuando su barra es
 *    un sliver de un píxel. Si el texto miente, el rediseño no sirve de nada.
 */
describe("toFunnelSteps", () => {
  const REAL = [
    { stage: "Screened", value: 22000 },
    { stage: "Stage 1 followed", value: 512 },
    { stage: "Stage 2 followed", value: 380 },
  ];

  it("la barra mide el absoluto: el embudo encoge porque el dato encoge", () => {
    // La propiedad que define un embudo. Con los números de producción las
    // etapas 2 y 3 son slivers — y eso ES el hallazgo, no un fallo del gráfico.
    const values = toFunnelSteps(REAL).map((s) => s.value);
    expect(values).toEqual([22000, 512, 380]);
    expect(values[1]).toBeLessThan(values[0]);
    expect(values[2]).toBeLessThan(values[1]);
  });

  it("la primera etapa es el punto de partida: 100 % de sí misma", () => {
    const [first] = toFunnelSteps(REAL);
    expect(first.retentionPct).toBe(100);
    expect(first.ofFirstPct).toBe(100);
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

  it("un cero legítimo es un 0 %, no un hueco", () => {
    const steps = toFunnelSteps([
      { stage: "Screened", value: 50 },
      { stage: "Stage 1 followed", value: 0 },
      { stage: "Stage 2 followed", value: 0 },
    ]);
    expect(steps[1].value).toBe(0);
    expect(steps[1].retentionPct).toBe(0);
  });

  it("sin base (etapa anterior a 0) la retención es null, nunca NaN ni Infinity", () => {
    // 0/0 = NaN y 3/0 = Infinity: los dos pintarían basura en el eje.
    const steps = toFunnelSteps([
      { stage: "Screened", value: 0 },
      { stage: "Stage 1 followed", value: 0 },
      { stage: "Stage 2 followed", value: 3 },
    ]);
    expect(steps[0].retentionPct).toBeNull();
    expect(steps[1].retentionPct).toBeNull();
    expect(steps[2].retentionPct).toBeNull();
  });

  it("una etapa que crece por encima de la anterior lo dice: pasa de 100 %", () => {
    // Dato corrupto pero real: recortarlo a 100 % lo escondería.
    const steps = toFunnelSteps([
      { stage: "Screened", value: 10 },
      { stage: "Stage 1 followed", value: 15 },
      { stage: "Stage 2 followed", value: 15 },
    ]);
    expect(steps[1].retentionPct).toBe(150);
    // Y su barra es más larga que la de la etapa anterior, porque el dato lo es.
    expect(steps[1].value).toBeGreaterThan(steps[0].value);
  });
});

describe("funnelAxisMax", () => {
  it("el eje llega hasta la etapa mayor: las demás se miden contra ella", () => {
    // Un eje compartido y absoluto es lo que hace comparables las tres barras.
    expect(funnelAxisMax(toFunnelSteps([
      { stage: "Screened", value: 22000 },
      { stage: "Stage 1 followed", value: 512 },
      { stage: "Stage 2 followed", value: 380 },
    ]))).toBe(22000);
  });

  it("con un dato corrupto el eje lo acomoda en vez de recortarlo", () => {
    expect(funnelAxisMax(toFunnelSteps([
      { stage: "Screened", value: 10 },
      { stage: "Stage 1 followed", value: 15 },
      { stage: "Stage 2 followed", value: 15 },
    ]))).toBe(15);
  });

  it("con todo a cero el eje no colapsa a [0, 0]", () => {
    // Recharts con dominio [0, 0] no sabe dónde poner nada. El suelo de 1 es
    // sólo para el eje: las barras siguen midiendo 0, que es la verdad.
    expect(funnelAxisMax(toFunnelSteps([
      { stage: "Screened", value: 0 },
      { stage: "Stage 1 followed", value: 0 },
      { stage: "Stage 2 followed", value: 0 },
    ]))).toBe(1);
  });
});

describe("conversionText", () => {
  const steps = toFunnelSteps([
    { stage: "Screened", value: 100 },
    { stage: "Stage 1 followed", value: 50 },
    { stage: "Stage 2 followed", value: 20 },
  ]);

  it("la primera etapa no retiene de nadie: es el punto de partida", () => {
    expect(conversionText(steps[0], true)).toBe("starting point");
  });

  it("las demás dicen qué retienen de la anterior Y qué queda de la cohorte", () => {
    expect(conversionText(steps[2], false)).toBe(
      "40% of the previous stage · 20% of those screened"
    );
  });

  it("sin base lo dice con palabras, no con un porcentaje inventado", () => {
    const zeroBase = toFunnelSteps([
      { stage: "Screened", value: 50 },
      { stage: "Stage 1 followed", value: 0 },
      { stage: "Stage 2 followed", value: 0 },
    ]);
    // Stage 1 cribó 50 y siguió a 0: eso es un 0 % de verdad.
    expect(conversionText(zeroBase[1], false)).toBe(
      "0% of the previous stage · 0% of those screened"
    );
    // Stage 2 arranca de una etapa vacía: no hay fracción que calcular.
    expect(conversionText(zeroBase[2], false)).toBe(
      "no baseline: the previous stage is 0 · 0% of those screened"
    );
  });
});

describe("formato en-US", () => {
  it("los recuentos llevan separador de millar", () => {
    expect(formatCount(22000)).toBe("22,000");
    expect(formatCount(0)).toBe("0");
  });

  it("los porcentajes llevan punto decimal y como mucho un decimal", () => {
    expect(formatPct(2.3272)).toBe("2.3%");
    expect(formatPct(100)).toBe("100%");
    expect(formatPct(0)).toBe("0%");
  });

  it("sin base no hay porcentaje que enseñar", () => {
    expect(formatPct(null)).toBe("no baseline");
  });
});
