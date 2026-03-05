// src/components/FilterBuilder.tsx
import React from "react";

export type FieldDef = {
  key: string;
  label: string;
  type?: string;
  source?: "sf" | "site" | "qual" | string;
  group?: string;
  qual_section?: string;
};

export type Rule = {
  field: string;
  operator: string;
  value: any;
};

export type FilterGroup = {
  logic: "AND" | "OR";
  rules: Array<Rule | FilterGroup>;
};

type Props = {
  fields: FieldDef[];
  value: FilterGroup;
  onChange: (next: FilterGroup) => void;
};

// ---------------- helpers ----------------
function getKind(f?: FieldDef) {
  const t = (f?.type || "string").toLowerCase();
  if (["number", "double", "currency", "int", "integer", "long"].includes(t)) return "number";
  if (["date", "datetime", "datetime2", "timestamp", "datetime-local"].includes(t)) return "date";
  if (["bool", "boolean"].includes(t)) return "boolean";
  return "string";
}

const OPS_STRING = [
  { v: "equals", label: "=" },
  { v: "not_equals", label: "≠" },
  { v: "is_empty", label: "is empty" },
  { v: "is_not_empty", label: "is not empty" },
  { v: "in", label: "is one of" },
  { v: "not_in", label: "is NOT one of" },
  { v: "contains", label: "contains" },
  { v: "not_contains", label: "not contains" },
  { v: "starts_with", label: "starts with" },
  { v: "ends_with", label: "ends with" },
];

const OPS_NUMBER = [
  { v: "equals", label: "=" },
  { v: "not_equals", label: "≠" },
  { v: "is_empty", label: "is empty" },
  { v: "is_not_empty", label: "is not empty" },
  { v: "in", label: "is one of" },
  { v: "not_in", label: "is NOT one of" },
  { v: ">", label: ">" },
  { v: ">=", label: "≥" },
  { v: "<", label: "<" },
  { v: "<=", label: "≤" },
  { v: "between", label: "between" },
];

const OPS_DATE = [
  { v: "equals", label: "=" },
  { v: "not_equals", label: "≠" },
  { v: ">", label: "after" },
  { v: ">=", label: "on or after" },
  { v: "<", label: "before" },
  { v: "<=", label: "on or before" },
  { v: "between", label: "between" },
];

const OPS_BOOL = [
  { v: "equals", label: "=" },
  { v: "not_equals", label: "≠" },
];

/** Extracts numeric section prefix e.g. "3.5.1" → [3,5,1] for sorting */
function sectionParts(name: string): number[] | null {
  const m = name.trim().match(/^(\d+(?:\.\d+)*)(?:\s|$)/);
  if (!m) return null;
  return m[1].split(".").map((x) => parseInt(x, 10));
}

/** Same comparator used in ColumnPicker — numeric section names first, then alphabetical */
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
  if (pa) return -1;
  if (pb) return 1;
  return a.localeCompare(b);
}

/** Explicit rank for known SF groups — mirrors the order in ExplorerView's SF_GROUPS */
const SF_GROUP_RANK: Record<string, number> = {
  "Site":               0,
  "Account Basics":     1,
  "OnBoarding":         2,
  "Profiling Basics":   3,
  "Screening":          4,
  "Salesforce (other)": 5,
  "Salesforce":         6,
  "Other":              7,
};

function getFieldBadge(f: { key: string; source?: string }) {
  const src = (f.source || "").toLowerCase();
  if (src === "qual" || f.key.startsWith("qual.")) return "[qual]";
  if (src === "site" || f.key.startsWith("site.")) return "[site]";
  return "[sf]";
}

function groupName(f: FieldDef): string {
  if (f.group && f.group.trim()) return f.group.trim();
  const s = (f.source || "").toLowerCase();
  if (s === "site") return "Site";
  if (s === "sf") return "Salesforce";
  if (s === "qual") {
    const section = f.qual_section?.trim();
    const group = f.group?.trim();
    if (section && group) return `${section} › ${group}`;
    if (section) return section;
    return group || "Qualification forms";
  }
  return "Other";
}

// ---------- UI ----------
export default function FilterBuilder({ fields, value, onChange }: Props) {
  // estado para el “picker buscable”
  const [pickerOpenFor, setPickerOpenFor] = React.useState<string | null>(null); // path string "0.1.2"
  const [pickerQuery, setPickerQuery] = React.useState("");

  // utils de edición inmutable
  const updateAt = (path: number[], updater: (node: FilterGroup | Rule) => FilterGroup | Rule) => {
    const deepClone = (n: FilterGroup | Rule): FilterGroup | Rule =>
      "rules" in n
        ? { logic: n.logic, rules: n.rules.map(deepClone as any) }
        : { field: (n as Rule).field, operator: (n as Rule).operator, value: (n as Rule).value };

    const root = deepClone(value) as FilterGroup;

    const getNode = (p: number[]): { parent: FilterGroup; index: number } => {
      let parent: FilterGroup = root;
      for (let i = 0; i < p.length - 1; i++) {
        parent = parent.rules[p[i]] as FilterGroup;
      }
      return { parent, index: p[p.length - 1] };
    };

    const { parent, index } = getNode(path);
    parent.rules[index] = updater(parent.rules[index]);
    onChange(root);
  };

  const addRuleTo = (groupPath: number[] = []) => {
    const root = structuredClone(value) as FilterGroup;
    let g: FilterGroup = root;
    for (let i = 0; i < groupPath.length; i++) g = g.rules[groupPath[i]] as FilterGroup;
    const firstField = fields[0]?.key || "";
    g.rules.push({ field: firstField, operator: "equals", value: "" });
    onChange(root);
  };

  const addGroupTo = (groupPath: number[] = []) => {
    const root = structuredClone(value) as FilterGroup;
    let g: FilterGroup = root;
    for (let i = 0; i < groupPath.length; i++) g = g.rules[groupPath[i]] as FilterGroup;
    g.rules.push({ logic: "AND", rules: [] });
    onChange(root);
  };

  const removeAt = (path: number[]) => {
    const root = structuredClone(value) as FilterGroup;
    let g: FilterGroup = root;
    for (let i = 0; i < path.length - 1; i++) g = g.rules[path[i]] as FilterGroup;
    g.rules.splice(path[path.length - 1], 1);
    onChange(root);
  };

  const changeGroupLogic = (path: number[] = [], logic: "AND" | "OR") => {
    const root = structuredClone(value) as FilterGroup;
    let g: FilterGroup = root;
    for (let i = 0; i < path.length; i++) g = g.rules[path[i]] as FilterGroup;
    g.logic = logic;
    onChange(root);
  };

  // grupos para el picker
  const groups = React.useMemo(() => {
    const map = new Map<string, FieldDef[]>();
    for (const f of fields) {
      const g = groupName(f);
      if (!map.has(g)) map.set(g, []);
      map.get(g)!.push(f);
    }
    const rankOf = (g: string) => SF_GROUP_RANK[g] ?? 50; // qual sections (numeric) get 50, sorted by compareGroupNames
    const sorted = [...map.entries()]
      .sort((a, b) => {
        const ra = rankOf(a[0]);
        const rb = rankOf(b[0]);
        if (ra !== rb) return ra - rb;
        // Both unknown (qual sections with numeric prefix) → sort numerically
        return compareGroupNames(a[0], b[0]);
      })
      .map(([name, list]) => [name, list.sort((x, y) => (x.label || x.key).localeCompare(y.label || y.key))] as const);
    return sorted;
  }, [fields]);

  // ---------------- searchable picker ----------------
  function FieldPicker({
    currentKey,
    onChoose,
  }: {
    currentKey?: string;
    onChoose: (k: string) => void;
  }) {
    const q = pickerQuery.trim().toLowerCase();
    const match = (f: FieldDef) =>
      !q ||
      (f.label || "").toLowerCase().includes(q) ||
      f.key.toLowerCase().includes(q) ||
      (f.group || "").toLowerCase().includes(q);

    return (
      <div className="absolute z-20 mt-1 w-[28rem] rounded-lg border bg-white shadow-lg p-2">
        <div className="p-2 pb-1">
          <input
            autoFocus
            className="w-full border rounded-md px-2 py-1 text-sm"
            placeholder="Search fields… (name, key or group)"
            value={pickerQuery}
            onChange={(e) => setPickerQuery(e.target.value)}
          />
        </div>
        <div className="max-h-72 overflow-auto px-2 pb-2">
          {groups.map(([gname, flist]) => {
            const visibles = flist.filter(match);
            if (!visibles.length) return null;
            return (
              <div key={gname} className="mb-2">
                <div className="text-xs font-semibold text-gray-500 px-1 py-1">{gname}</div>
                <ul className="space-y-1">
                  {visibles.map((f) => (
                    <li key={f.key}>
                      <button
                        className={`w-full text-left text-sm px-2 py-1 rounded hover:bg-gray-50 ${
                          currentKey === f.key ? "bg-gray-100" : ""
                        }`}
                        onClick={() => {
                          onChoose(f.key);
                          setPickerOpenFor(null);
                          setPickerQuery("");
                        }}
                      >
                        <span className="mr-2 text-gray-500">{getFieldBadge(f)}</span>
                        <span className="font-medium">{f.label}</span>
                        <span className="ml-2 text-[11px] text-gray-500">{f.key}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
          {!groups.some(([_, fl]) => fl.some((f) => match(f))) ? (
            <div className="text-sm text-gray-500 px-2 py-3">No matching fields</div>
          ) : null}
        </div>
      </div>
    );
  }

  // -------------- renderers --------------
  const renderRule = (rule: Rule, path: number[]) => {
    const meta = fields.find((f) => f.key === rule.field);
    const kind = getKind(meta);

    const ops =
      kind === "number" ? OPS_NUMBER : kind === "date" ? OPS_DATE : kind === "boolean" ? OPS_BOOL : OPS_STRING;

    // inputs de valor
    const valueInput = () => {
      if (kind === "boolean") {
        const val = String(rule.value ?? "true");
        return (
          <select
            className="border rounded-md px-2 py-1 text-sm"
            value={val}
            onChange={(e) => updateAt(path, (r) => ({ ...(r as Rule), value: e.target.value === "true" }))}
          >
            <option value="true">True</option>
            <option value="false">False</option>
          </select>
        );
      }

      if (rule.operator === "is_empty" || rule.operator === "is_not_empty") {
        return null; // no value needed for these operators
      }

      if (rule.operator === "between") {
        const [a, b] = Array.isArray(rule.value) ? rule.value : ["", ""];
        if (kind === "date") {
          return (
            <div className="flex items-center gap-2">
              <input
                type="date"
                className="border rounded-md px-2 py-1 text-sm"
                value={a || ""}
                onChange={(e) => updateAt(path, (r) => ({ ...(r as Rule), value: [e.target.value, b || ""] }))}
              />
              <span className="text-gray-500 text-sm">and</span>
              <input
                type="date"
                className="border rounded-md px-2 py-1 text-sm"
                value={b || ""}
                onChange={(e) => updateAt(path, (r) => ({ ...(r as Rule), value: [a || "", e.target.value] }))}
              />
            </div>
          );
        }
        return (
          <div className="flex items-center gap-2">
            <input
              type={kind === "number" ? "number" : "text"}
              className="border rounded-md px-2 py-1 text-sm w-32"
              value={a ?? ""}
              onChange={(e) => updateAt(path, (r) => ({ ...(r as Rule), value: [e.target.value, b ?? ""] }))}
            />
            <span className="text-gray-500 text-sm">and</span>
            <input
              type={kind === "number" ? "number" : "text"}
              className="border rounded-md px-2 py-1 text-sm w-32"
              value={b ?? ""}
              onChange={(e) => updateAt(path, (r) => ({ ...(r as Rule), value: [a ?? "", e.target.value] }))}
            />
          </div>
        );
      }

      if (kind === "date") {
        return (
          <input
            type="date"
            className="border rounded-md px-2 py-1 text-sm"
            value={rule.value ?? ""}
            onChange={(e) => updateAt(path, (r) => ({ ...(r as Rule), value: e.target.value }))}
          />
        );
      }

      if (kind === "number") {
        return (
          <input
            type="number"
            className="border rounded-md px-2 py-1 text-sm w-40"
            value={rule.value ?? ""}
            onChange={(e) => updateAt(path, (r) => ({ ...(r as Rule), value: e.target.value }))}
          />
        );
      }

      return (
        <input
          type="text"
          className="border rounded-md px-2 py-1 text-sm w-64"
          placeholder={rule.operator === "in" || rule.operator === "not_in" || /\.Id$|Id$/i.test(rule.field) ? "value1, value2, …" : undefined}
          value={rule.value ?? ""}
          onChange={(e) => updateAt(path, (r) => ({ ...(r as Rule), value: e.target.value }))}
        />
      );
    };

    const pathKey = path.join(".");
    const firstFieldKey = fields[0]?.key || "";

    return (
      <div key={pathKey} className="flex flex-wrap items-center gap-2">
        {/* botón que abre el picker */}
        <div className="relative">
          <button
            className="border rounded-md px-3 py-1.5 text-sm min-w-[18rem] text-left hover:bg-gray-50"
            onClick={() => setPickerOpenFor(pickerOpenFor === pathKey ? null : pathKey)}
            title="Choose field"
          >
            <span className="text-gray-500 mr-2">{getFieldBadge(meta || { key: rule.field })}</span>
            <span className="font-medium">
              {meta?.label || rule.field || "Select a field"}
            </span>
            <span className="ml-2 text-[11px] text-gray-500">{meta?.key || rule.field}</span>
          </button>

          {pickerOpenFor === pathKey ? (
            <FieldPicker
              currentKey={rule.field}
              onChoose={(k) =>
                updateAt(path, (r) => ({
                  ...(r as Rule),
                  field: k || firstFieldKey,
                  operator: "equals",
                  value: "",
                }))
              }
            />
          ) : null}
        </div>

        {/* operador */}
        <select
          className="border rounded-md px-2 py-1 text-sm"
          value={rule.operator}
          onChange={(e) =>
            updateAt(path, (r) => ({
              ...(r as Rule),
              operator: e.target.value,
              value: e.target.value === "between" ? ["", ""]
                   : (e.target.value === "is_empty" || e.target.value === "is_not_empty") ? null
                   : "",
            }))
          }
          title={rule.operator === "in" || rule.operator === "not_in" ? "Use comma-separated values" : undefined}
        >
          {ops.map((o) => (
            <option key={o.v} value={o.v}>
              {o.label}
            </option>
          ))}
        </select>

        {/* valor */}
        {valueInput()}

        <button
          className="ml-2 text-sm rounded-md border px-3 py-1 hover:bg-gray-50 text-red-600 border-red-200"
          onClick={() => removeAt(path)}
        >
          Remove
        </button>
      </div>
    );
  };

  const renderGroup = (group: FilterGroup, path: number[] = []) => {
    const isRoot = path.length === 0;

    const scaffoldTwoGroups = (logic: "AND" | "OR") => {
      // Replace current group content with two empty subgroups under chosen logic
      const root = structuredClone(value) as FilterGroup;
      // Navigate to target group
      const getNode = (p: number[]): FilterGroup => {
        let g: FilterGroup = root;
        for (let i = 0; i < p.length; i++) g = g.rules[p[i]] as FilterGroup;
        return g;
      };
      const target = getNode(path);
      target.logic = logic;
      target.rules = [ { logic: "AND", rules: [] }, { logic: "AND", rules: [] } ] as any;
      onChange(root);
    };

    return (
      <div className="space-y-3 border rounded-lg p-3">
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-sm text-gray-700">Group</label>
          <select
            className="border rounded-md px-2 py-1 text-sm"
            value={group.logic}
            onChange={(e) => changeGroupLogic(path, e.target.value as "AND" | "OR")}
          >
            <option value="AND">AND</option>
            <option value="OR">OR</option>
          </select>

          <button className="text-sm rounded-md border px-3 py-1.5 hover:bg-gray-50" onClick={() => addRuleTo(path)}>
            + Rule
          </button>
          <button className="text-sm rounded-md border px-3 py-1.5 hover:bg-gray-50" onClick={() => addGroupTo(path)}>
            + Group
          </button>

          {isRoot && (
            <>
              <button
                className="text-sm rounded-md border px-3 py-1.5 hover:bg-gray-50"
                title="Scaffold ( … ) AND ( … )"
                onClick={() => scaffoldTwoGroups("AND")}
              >
                Scaffold: ( … ) AND ( … )
              </button>
              <button
                className="text-sm rounded-md border px-3 py-1.5 hover:bg-gray-50"
                title="Scaffold ( … ) OR ( … )"
                onClick={() => scaffoldTwoGroups("OR")}
              >
                Scaffold: ( … ) OR ( … )
              </button>
            </>
          )}

          {!isRoot && (
            <button
              className="ml-auto text-sm rounded-md border px-3 py-1.5 hover:bg-red-50 text-red-600 border-red-200"
              onClick={() => removeAt(path)}
              title="Remove this group"
            >
              Remove group
            </button>
          )}
        </div>

        <div className="space-y-2">
          {group.rules.length === 0 ? (
            <div className="text-sm text-gray-500">Add rules or subgroups</div>
          ) : null}
          {group.rules.map((child, idx) =>
            "rules" in child ? (
              <div key={[...path, idx].join(".")} className="pl-3 border-l-2 border-gray-200">
                {renderGroup(child as FilterGroup, [...path, idx])}
              </div>
            ) : (
              renderRule(child as Rule, [...path, idx])
            )
          )}
        </div>
      </div>
    );
  };

  // cerrar el picker al hacer click fuera
  React.useEffect(() => {
    const onDocClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      // si pulsamos en un botón del picker no cerramos
      if (target.closest(".absolute.z-20")) return;
      if (target.closest("button[title='Choose field']")) return;
      if (pickerOpenFor) setPickerOpenFor(null);
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [pickerOpenFor]);

  return <div data-testid="filter-builder">{renderGroup(value)}</div>;
}
