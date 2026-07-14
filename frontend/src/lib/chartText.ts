/**
 * Texto compartido por las vistas del modal de gráficos.
 *
 * Toda la UI de esta app está en INGLÉS (los comentarios, no). Las líneas de
 * cobertura son la parte del rediseño que más se lee — "81 of 215 sites report
 * X · 134 with no data, excluded" — y un "1 sites" ahí canta lo suficiente como
 * para que el lector desconfíe del número que va al lado.
 *
 * Un "1 row(s)" es la misma dejadez con paréntesis: si el conteo se escribe a
 * mano en cada sitio, tarde o temprano uno se olvida. Se escribe UNA vez.
 */
function pluralize(n: number, singular: string, plural: string): string {
  return `${n} ${n === 1 ? singular : plural}`;
}

export function siteCount(n: number): string {
  return pluralize(n, "site", "sites");
}

export function rowCount(n: number): string {
  return pluralize(n, "row", "rows");
}
