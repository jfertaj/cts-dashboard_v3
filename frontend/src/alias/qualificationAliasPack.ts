// src/alias/qualificationAliasPack.ts
/* 
   Carga y normaliza el pack de alias para site_qual / qual.*
   - Importa el JSON del pack
   - Convierte cada regex (string) en RegExp
   - Expone un objeto tipado y listo para usar en tryFollowUp
*/

import raw from "./qualification_alias_pack.json";

// ——— Tipos ———
export type AliasType = "boolean" | "number" | "category" | "text";

export interface AliasEntryRaw {
  name: string;
  regex: string;            // en JSON viene como string
  type: AliasType;
  prefer: string[];         // claves qual.* sugeridas por orden
}

export interface AliasPackRaw {
  version?: string;
  aliases: AliasEntryRaw[];
}

// Versión normalizada que usaremos en el front:
export interface AliasEntry {
  name: string;
  regex: RegExp;            // ¡ahora RegExp!
  type: AliasType;
  prefer: string[];
}

export interface AliasPack {
  version: string;
  aliases: AliasEntry[];
}

// ——— Helpers ———
function toRegExp(rx: string): RegExp {
  // Permitimos formas “/patrón/i” o “patrón” simple → por defecto “i”
  const m = rx.match(/^\/(.+)\/([a-z]*)$/i);
  if (m) {
    const [, body, flags] = m;
    return new RegExp(body, flags || "i");
  }
  return new RegExp(rx, "i");
}

function normalizePack(input: AliasPackRaw): AliasPack {
  const version = input?.version || "1.0.0";
  const aliases: AliasEntry[] = Array.isArray(input?.aliases)
    ? input.aliases
        .filter(a => a && a.regex && a.prefer && a.prefer.length > 0)
        .map(a => ({
          name: a.name || "(unnamed)",
          regex: toRegExp(a.regex),
          type: (a.type as AliasType) || "text",
          prefer: a.prefer,
        }))
    : [];
  return { version, aliases };
}

// ——— Instancia única (lista para importar) ———
const _raw = raw as unknown as AliasPackRaw;
export const QUAL_ALIAS_PACK: AliasPack = normalizePack(_raw);

// Export opcional: utilidades rápidas
export function findMatchingAliases(query: string): AliasEntry[] {
  const q = query ?? "";
  return QUAL_ALIAS_PACK.aliases.filter(a => a.regex.test(q));
}