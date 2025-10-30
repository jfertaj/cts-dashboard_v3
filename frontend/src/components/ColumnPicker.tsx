// src/components/ColumnPicker.tsx
import React, { useEffect, useMemo, useState } from "react";

export type FieldDef = {
  key: string;
  label: string;
  type?: string;
  source?: "sf" | "site" | string;
  /** opcional: nombre de grupo ya resuelto desde backend (para qual.* = Subsection, ej. "2.3 Ethics...") */
  group?: string;
  qual_section?: string;
};

type Preset = { id: string; label: string; keys: string[] };

type ColumnPickerProps = {
  fields: FieldDef[];
  value: string[];                    // columnas visibles (claves completas)
  onChange: (next: string[]) => void;
  presets?: Preset[];
  showPresets?: boolean;
  defaultOpen?: boolean;
  /** Orden de claves para la UI (no cambia el valor final) */
  sortKeys?: (allKeys: string[], presets: Preset[]) => string[];
  /** Agrupa campos. Si no se pasa, intenta usar f.group o f.source. */
  groupBy?: (f: FieldDef) => string | undefined;
  /** Render del encabezado de cada grupo */
  renderGroupHeader?: (groupName: string) => React.ReactNode;
};

function uniq<T>(arr: T[]): T[] { return Array.from(new Set(arr)); }

function orderUnionByPresets(unioned: string[], presets: Preset[]): string[] {
  const rank = new Map<string, number>();
  const HUGE = 1e9;
  presets.forEach((p, pi) => {
    p.keys.forEach((k, ki) => { if (!rank.has(k)) rank.set(k, pi * 10000 + ki); });
  });
  return [...unioned].sort((a, b) => (rank.get(a) ?? HUGE) - (rank.get(b) ?? HUGE));
}

/** Extrae prefijo seccional "3.5.1" como array de números [3,5,1] para ordenar numéricamente */
function sectionParts(name: string): number[] | null {
  const m = name.trim().match(/^(\d+(?:\.\d+)*)(?:\s|$)/);
  if (!m) return null;
  return m[1].split(".").map((x) => parseInt(x, 10));
}

/** Comparador por secciones numéricas; si no hay numeración, cae a alfabético */
function compareGroupNames(a: string, b: string): number {
  const pa = sectionParts(a);
  const pb = sectionParts(b);
  if (pa && pb) {
    const n = Math.max(pa.length, pb.length);
    for (let i = 0; i < n; i++) {
      const va = pa[i] ?? -1;
      const vb = pb[i] ?? -1;
      if (va !== vb) return va - vb;
    }
    return 0;
  }
  if (pa) return -1; // con numeración primero
  if (pb) return 1;
  return a.localeCompare(b);
}

export default function ColumnPicker({
  fields,
  value,
  onChange,
  presets = [],
  showPresets = true,
  defaultOpen = false,
  sortKeys,
  groupBy,
  renderGroupHeader,
}: ColumnPickerProps) {
  const [open, setOpen] = useState(defaultOpen);
  const [query, setQuery] = useState("");
  const [activePresetIds, setActivePresetIds] = useState<string[]>([]);

  // Todas las claves válidas + mapa label
  const allKeys = useMemo(() => fields.map((f) => f.key), [fields]);
  const labelByKey = useMemo(() => {
    const m = new Map<string, string>();
    fields.forEach((f) => m.set(f.key, f.label || f.key));
    return m;
  }, [fields]);

  // Orden base de claves (sirve de input al filtrado); si no llega sortKeys, usa alfabético
  const orderedKeys = useMemo(() => {
    const base = allKeys.slice();
    return sortKeys ? sortKeys(base, presets) : base.sort((a, b) => a.localeCompare(b));
  }, [allKeys, presets, sortKeys]);

  // Calcula qué presets están "completamente activos"
  useEffect(() => {
    const valSet = new Set(value);
    const fullIncluded = presets
      .filter(p => p.keys.every(k => valSet.has(k)))
      .map(p => p.id);
    setActivePresetIds(prev => {
      const a = new Set(prev), b = new Set(fullIncluded);
      return (a.size === b.size && [...a].every(x => b.has(x))) ? prev : fullIncluded;
    });
  }, [value, presets]);

  // Helpers selección manual
  const toggleKey = (k: string) => {
    if (!allKeys.includes(k)) return;
    if (value.includes(k)) onChange(value.filter((x) => x !== k));
    else onChange([...value, k]);
  };

  const toggleAll = (keys: string[]) => {
    const allIncluded = keys.every((k) => value.includes(k));
    if (allIncluded) onChange(value.filter((k) => !keys.includes(k)));
    else onChange(uniq([...value, ...keys]));
  };

  // Filtrado por query (pre-agrupación)
  const filteredKeys = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return orderedKeys;
    return orderedKeys.filter((k) => {
      const label = labelByKey.get(k) || k;
      return k.toLowerCase().includes(q) || label.toLowerCase().includes(q);
    });
  }, [orderedKeys, labelByKey, query]);

  // === Agrupación (resiliente) + ORDEN de grupos y de items ===
  const groupNameOf = (f: FieldDef): string => {
    const by = groupBy?.(f);
    if (by) return by;
    if ((f.source || "").toLowerCase() === "qual") {
      const section = f.qual_section?.trim();
      const subsection = f.group?.trim();
      if (section && subsection) return `${section} › ${subsection}`;
      if (section) return section;
      if (subsection) return subsection;
      return "Qualification";
    }
    if (f.group) return f.group;
    if (f.source) return f.source.toString();
    if (f.key.startsWith("qual.")) return "Qualification";
    if (f.key.startsWith("sf.")) return "Salesforce";
    if (f.key.startsWith("site.")) return "Site";
    return "Other";
  };

  const groupedEntries = useMemo(() => {
    const buckets = new Map<string, string[]>();
    const byKey = new Map(fields.map(f => [f.key, f] as const));

    filteredKeys.forEach((k) => {
      const f = byKey.get(k);
      const g = f ? groupNameOf(f) : "Other";
      const arr = buckets.get(g) || [];
      arr.push(k);
      buckets.set(g, arr);
    });

    // Ordenar claves dentro del grupo por label
    for (const [g, arr] of buckets) {
      arr.sort((a, b) => {
        const la = labelByKey.get(a) || a;
        const lb = labelByKey.get(b) || b;
        return la.localeCompare(lb);
      });
      buckets.set(g, arr);
    }

    // Ordenar grupos: si empiezan por "n.m" ordenar numéricamente; resto alfabético.
    return [...buckets.entries()].sort((a, b) => compareGroupNames(a[0], b[0]));
  }, [filteredKeys, fields, labelByKey]);

  // Presets: toggle multi
  const onTogglePreset = (presetId: string) => {
    const p = presets.find((x) => x.id === presetId);
    if (!p) return;
    const isActive = activePresetIds.includes(presetId);
    if (isActive) {
      // Desactivar preset: quitamos SOLO sus keys si no están cubiertas por otros presets activos
      const otherIds = activePresetIds.filter((x) => x !== presetId);
      const otherKeys = uniq(otherIds.flatMap((id) => presets.find((pp) => pp.id === id)?.keys || []));
      const next = value.filter((k) => !(p.keys.includes(k) && !otherKeys.includes(k)));
      onChange(next);
      setActivePresetIds(otherIds);
    } else {
      // Activar preset
      const nextUnion = uniq([...value, ...p.keys]);
      const nextOrdered = orderUnionByPresets(nextUnion, presets);
      onChange(nextOrdered);
      setActivePresetIds([...activePresetIds, presetId]);
    }
  };

  // Clear presets: elimina TODAS las columnas actualmente seleccionadas que pertenezcan a CUALQUIER preset
  const clearPresets = () => {
    if (presets.length === 0) return;
    const selectedKeysThatBelongToPresets = new Set(
      presets.flatMap(p => p.keys.filter(k => value.includes(k)))
    );
    if (selectedKeysThatBelongToPresets.size === 0) return;
    const next = value.filter(k => !selectedKeysThatBelongToPresets.has(k));
    onChange(next);
    setActivePresetIds([]); // desactiva la marca de presets completos
  };

  const clearColumns = () => onChange([]);

  return (
    <div className="p-3">
      {/* Barra superior */}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="rounded-md border px-3 py-1.5 text-sm hover:bg-gray-50"
        >
          {open ? "Hide column selector" : "Show column selector"}
        </button>

        <div className="ml-auto flex items-center gap-2">
          <span className="text-sm text-gray-600">{value.length} selected</span>
          <button
            type="button"
            onClick={clearColumns}
            className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50"
            title="Clear all selected columns"
          >
            Clear columns
          </button>
        </div>
      </div>

      {!open ? null : (
        <div className="mt-3 space-y-3">
          {/* Presets */}
          {showPresets && presets.length > 0 && (
            <div>
              <div className="flex items-center justify-between mb-2">
                <span className="text-sm font-medium">Presets</span>
                <button
                  type="button"
                  onClick={clearPresets}
                  className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50"
                  title="Remove all selected fields that belong to any preset"
                >
                  Clear presets
                </button>
              </div>
              <div className="flex flex-wrap gap-2">
                {presets.map((p) => {
                  const active = activePresetIds.includes(p.id);
                  const selectedCount = p.keys.filter((k) => value.includes(k)).length;
                  const total = p.keys.length;

                  const base = "px-3 py-1.5 rounded-full border text-sm inline-flex items-center gap-2";
                  const cls = active
                    ? "bg-blue-600 border-blue-700 text-white shadow-sm"
                    : "bg-white border-gray-300 text-gray-700 hover:bg-gray-50";

                  const tooltip = p.keys.map((k) => `• ${labelByKey.get(k) ?? k}`).join("\n");

                  return (
                    <button
                      key={p.id}
                      type="button"
                      onClick={() => onTogglePreset(p.id)}
                      className={`${base} ${cls}`}
                      aria-pressed={active}
                      title={`${p.label}\n${tooltip}`}
                    >
                      <span>{p.label}</span>
                      <span
                        className={
                          "text-xs px-2 py-0.5 rounded-full border " +
                          (active ? "border-white/30 bg-white/20" : "border-gray-300 bg-gray-50 text-gray-700")
                        }
                      >
                        {selectedCount}/{total}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* Buscador y toggle por filtro */}
          <div className="flex items-center gap-2">
            <input
              className="border rounded-md px-3 py-1.5 text-sm w-72"
              placeholder="Search fields…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <button
              type="button"
              onClick={() => toggleAll(filteredKeys)}
              className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50"
              title="Toggle all filtered fields"
            >
              {filteredKeys.every((k) => value.includes(k)) ? "Deselect filtered" : "Select filtered"}
            </button>
          </div>

          {/* Lista agrupada */}
          <div className="max-h-[320px] overflow-auto rounded-md border">
            {groupedEntries.map(([groupName, keys]) => (
              <div key={groupName}>
                <div className="bg-gray-50 border-b px-3 py-2 sticky top-0 z-10">
                  {renderGroupHeader ? (
                    renderGroupHeader(groupName)
                  ) : (
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-semibold text-gray-700">{groupName}</span>
                      <button
                        type="button"
                        onClick={() => toggleAll(keys)}
                        className="text-xs rounded-md border px-2 py-0.5 hover:bg-white"
                      >
                        {keys.every((k) => value.includes(k)) ? "Deselect group" : "Select group"}
                      </button>
                    </div>
                  )}
                </div>
                <table className="min-w-full text-sm">
                  <tbody>
                    {keys.map((k) => {
                      const checked = value.includes(k);
                      return (
                        <tr key={k} className="border-t hover:bg-gray-50">
                          <td className="px-3 py-2 w-10">
                            <input
                              type="checkbox"
                              className="h-4 w-4"
                              checked={checked}
                              onChange={() => toggleKey(k)}
                            />
                          </td>
                          <td className="px-3 py-2 whitespace-nowrap">{labelByKey.get(k) ?? k}</td>
                          <td className="px-3 py-2 text-gray-500">{k}</td>
                        </tr>
                      );
                    })}
                    {keys.length === 0 && (
                      <tr>
                        <td className="px-3 py-6 text-center text-gray-500" colSpan={3}>
                          No fields in this group
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            ))}
            {groupedEntries.length === 0 && (
              <div className="px-3 py-6 text-center text-gray-500">No fields match your search</div>
            )}
          </div>

          <div className="text-xs text-gray-500">
            Tip: You can also drag & drop columns in the table header to reorder them.
          </div>
        </div>
      )}
    </div>
  );
}
