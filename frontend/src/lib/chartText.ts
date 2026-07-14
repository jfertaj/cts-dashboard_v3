/**
 * Texto compartido por las vistas del modal de gráficos.
 *
 * Toda la UI de esta app está en INGLÉS (los comentarios, no). Las líneas de
 * cobertura son la parte del rediseño que más se lee — "81 of 215 sites report
 * X · 134 with no data, excluded" — y un "1 sites" ahí canta lo suficiente como
 * para que el lector desconfíe del número que va al lado.
 */
export function siteCount(n: number): string {
  return `${n} ${n === 1 ? "site" : "sites"}`;
}
