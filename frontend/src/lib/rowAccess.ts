// frontend/src/lib/rowAccess.ts
// `country` y `city` admiten null porque así los manda el backend (ver
// `ExplorerRow` en lib/api.ts): sin el null, ExplorerRow[] no es asignable a
// DataRow[] y el Explorer no podría pasar sus filas al modal sin castear.
export type DataRow = {
  account_id?: string;
  account_name?: string;
  country?: string | null;
  city?: string | null;
  data?: Record<string, unknown>;
};

/**
 * Lee una celda de una fila del Explorer probando, en orden: la clave exacta,
 * la clave sin prefijo "sf.", las variantes con underscores, y por último los
 * campos planos de la fila (account_name, country, city...).
 * Un valor vacío cuenta como ausente y sigue buscando.
 */
export function readDataCell(row: DataRow, key: string): unknown {
  const d: Record<string, unknown> = row?.data ?? {};

  // 1) clave exacta tal cual viene del backend (p.ej. "sf.C_Number_of_T1D_Patients_currently_U_18__c")
  if (d[key] !== undefined && d[key] !== null && String(d[key]).trim() !== "") return d[key];

  // 2) misma clave sin el prefijo "sf."
  const base = key.replace(/^sf\./, "");
  if (d[base] !== undefined && d[base] !== null && String(d[base]).trim() !== "") return d[base];

  // 3) variantes con underscores (por si acaso)
  const k2 = key.replace(/\./g, "_");
  if (d[k2] !== undefined && d[k2] !== null && String(d[k2]).trim() !== "") return d[k2];

  const k3 = base.replace(/\./g, "_");
  if (d[k3] !== undefined && d[k3] !== null && String(d[k3]).trim() !== "") return d[k3];

  // 4) fallbacks a propiedades de fila "planas" (el backend ya las trae así)
  //    - sf.Account.Name / Account.Name  -> row.account_name
  //    - sf.Account.Id   / Account.Id    -> row.account_id
  //    - country, city (o sus variantes SF) -> row.country / row.city
  const kb = base.toLowerCase();
  if (kb === 'account.name') return row?.account_name ?? undefined;
  if (kb === 'account.id') return row?.account_id ?? undefined;
  if (key === 'sf.Account.Name') return row?.account_name ?? undefined;
  if (key === 'sf.Account.Id')   return row?.account_id ?? undefined;
  if (kb === 'country' || kb === 'account.shippingcountry') return row?.country ?? undefined;
  if (kb === 'city'    || kb === 'account.shippingcity')    return row?.city ?? undefined;

  // ⚠️ Importante: devolver undefined para que TanStack Table pueda aplicar sortUndefined: 'last'
  return undefined;
}
