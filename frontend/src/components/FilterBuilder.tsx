// src/components/FilterBuilder.tsx
import React from "react";
import {
  filterGroupToFlat,
  parseExprToFilterGroup,
  validateExpr,
  defaultExpr,
  addRuleToExpr,
  removeRuleFromExpr,
  type FlatRule,
} from "../lib/filterLogic";
import type { FilterGroup, Rule } from "../lib/api";

export type FieldDef = {
  key: string;
  label: string;
  type?: string;
  source?: "sf" | "site" | "qual" | string;
  group?: string;
  qual_section?: string;
};

// Re-export for convenience
export type { FilterGroup, Rule };

type Props = {
  fields: FieldDef[];
  value: FilterGroup;
  onChange: (next: FilterGroup) => void;
};

// ---------------- kind/ops helpers ----------------

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

function sectionParts(name: string): number[] | null {
  const m = name.trim().match(/^(\d+(?:\.\d+)*)(?:\s|$)/);
  if (!m) return null;
  return m[1].split(".").map((x) => parseInt(x, 10));
}

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

const SF_GROUP_RANK: Record<string, number> = {
  "Site": 0,
  "Account Basics": 1,
  "OnBoarding": 2,
  "Profiling › PI and Sub-I Experience": 10,
  "Profiling › Lead Study Coordinator": 11,
  "Profiling › Lead Study Nurse": 12,
  "Profiling › Clinical Site Set Up": 13,
  "Profiling › Patient Population": 14,
  "Profiling › Screening Initiatives": 15,
  "Profiling › Process & Admin": 16,
  "Salesforce (other)": 20,
  "Salesforce": 21,
  "Other": 22,
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

// ---------------- Filter Logic Modal ----------------

function FilterLogicModal({
  open,
  expr,
  numRules,
  ruleLabels,
  onClose,
  onApply,
}: {
  open: boolean;
  expr: string;
  numRules: number;
  ruleLabels: string[];
  onClose: () => void;
  onApply: (newExpr: string) => void;
}) {
  const [draft, setDraft] = React.useState(expr);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (open) { setDraft(expr); setError(null); }
  }, [open, expr]);

  const handleChange = (v: string) => {
    setDraft(v);
    setError(validateExpr(v, numRules));
  };

  const handleApply = () => {
    const err = validateExpr(draft, numRules);
    if (err) { setError(err); return; }
    onApply(draft.trim());
    onClose();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleApply(); }
    if (e.key === "Escape") onClose();
  };

  if (!open) return null;

  return (
    <div data-testid="filter-logic-modal" className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-md p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-base font-semibold text-gray-900">Edit Filter Logic</h3>
          <button
            onClick={onClose}
            className="rounded p-1 hover:bg-gray-100 text-gray-500"
            aria-label="Close"
          >
            <svg className="h-4 w-4" viewBox="0 0 16 16" fill="none" aria-hidden>
              <path d="M2 2l12 12M14 2L2 14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
            </svg>
          </button>
        </div>

        <textarea
          data-testid="filter-logic-textarea"
          className={`w-full border rounded-lg px-3 py-2 text-sm font-mono resize-none focus:outline-none focus:ring-2 ${
            error ? "border-red-400 focus:ring-red-200" : "border-gray-300 focus:ring-blue-200"
          }`}
          rows={3}
          value={draft}
          onChange={(e) => handleChange(e.target.value)}
          onKeyDown={handleKeyDown}
          autoFocus
          spellCheck={false}
        />

        {error && (
          <p data-testid="filter-logic-error" className="mt-1 text-xs text-red-600">{error}</p>
        )}
        {!error && draft.trim() && (
          <p data-testid="filter-logic-valid" className="mt-1 text-xs text-green-600">Valid expression</p>
        )}

        {ruleLabels.length > 0 && (
          <div className="mt-3 space-y-0.5 max-h-36 overflow-auto">
            {ruleLabels.map((label, i) => (
              <div key={i} className="text-xs text-gray-500">
                <span className="font-semibold text-gray-700">{i + 1}</span> = {label}
              </div>
            ))}
          </div>
        )}

        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-md border px-4 py-1.5 text-sm hover:bg-gray-50"
          >
            Cancel
          </button>
          <button
            data-testid="filter-logic-apply-btn"
            onClick={handleApply}
            disabled={!!error}
            className="rounded-md bg-blue-600 text-white px-4 py-1.5 text-sm hover:bg-blue-700 disabled:opacity-50"
          >
            Apply
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------- Main FilterBuilder ----------------

export default function FilterBuilder({ fields, value, onChange }: Props) {
  // Convert incoming FilterGroup prop → flat rules + expr for display
  const [rules, setRules] = React.useState<FlatRule[]>([]);
  const [logicExpr, setLogicExpr] = React.useState<string>("");
  const [showModal, setShowModal] = React.useState(false);
  const [pickerOpenFor, setPickerOpenFor] = React.useState<number | null>(null);
  const [pickerQuery, setPickerQuery] = React.useState("");

  // Track last emitted JSON to avoid infinite sync loops
  const lastEmittedRef = React.useRef<string>("");

  // Sync from parent prop when it changes externally (e.g., Moby sets new filters)
  React.useEffect(() => {
    const json = JSON.stringify(value);
    if (json === lastEmittedRef.current) return;  // our own emit — skip
    try {
      const flat = filterGroupToFlat(value);
      setRules(flat.rules);
      setLogicExpr(flat.logicExpr);
    } catch {
      setRules([]);
      setLogicExpr("");
    }
  }, [value]);

  // Emit a new FilterGroup to the parent when flat state changes
  const emit = React.useCallback((newRules: FlatRule[], newExpr: string) => {
    try {
      // Build a flat FilterGroup: logic=expr, rules=flat leaf rules
      const flatFg: FilterGroup = {
        logic: newExpr || defaultExpr(newRules.length),
        rules: newRules.map((r) => ({ field: r.field, operator: r.operator, value: r.value })),
      };
      const json = JSON.stringify(flatFg);
      lastEmittedRef.current = json;
      onChange(flatFg);
    } catch {
      // If parseExpr fails (shouldn't happen here), emit simple AND
      const fallback: FilterGroup = {
        logic: "AND",
        rules: newRules.map((r) => ({ field: r.field, operator: r.operator, value: r.value })),
      };
      lastEmittedRef.current = JSON.stringify(fallback);
      onChange(fallback);
    }
  }, [onChange]);

  // Field groups for the picker
  const groups = React.useMemo(() => {
    const map = new Map<string, FieldDef[]>();
    for (const f of fields) {
      const g = groupName(f);
      if (!map.has(g)) map.set(g, []);
      map.get(g)!.push(f);
    }
    const rankOf = (g: string) => SF_GROUP_RANK[g] ?? 50;
    return [...map.entries()]
      .sort((a, b) => {
        const ra = rankOf(a[0]); const rb = rankOf(b[0]);
        if (ra !== rb) return ra - rb;
        return compareGroupNames(a[0], b[0]);
      })
      .map(([name, list]) => [name, list.sort((x, y) => (x.label || x.key).localeCompare(y.label || y.key))] as const);
  }, [fields]);

  // Rule labels for the logic modal reference list
  const ruleLabels = React.useMemo(() =>
    rules.map((r) => {
      const meta = fields.find((f) => f.key === r.field);
      const label = meta?.label || r.field.split(".").pop() || r.field;
      const opLabel: Record<string, string> = {
        equals: "=", not_equals: "≠", ">": ">", ">=": "≥", "<": "<", "<=": "≤",
        between: "between", in: "in", not_in: "not in",
        is_empty: "is empty", is_not_empty: "is not empty",
        contains: "contains", not_contains: "not contains",
        starts_with: "starts with", ends_with: "ends with",
      };
      const op = opLabel[r.operator] ?? r.operator;
      const noValue = r.operator === "is_empty" || r.operator === "is_not_empty";
      const valStr = noValue ? "" : Array.isArray(r.value) ? r.value.filter(Boolean).join(" – ") : String(r.value ?? "");
      return noValue ? `${label} ${op}` : `${label} ${op} ${valStr}`.trim();
    }),
    [rules, fields]
  );

  // ---- Rule mutations ----

  const addRule = () => {
    const firstField = fields[0]?.key || "";
    const newId = rules.length + 1;
    const newRule: FlatRule = { id: newId, field: firstField, operator: "equals", value: "" };
    const newRules = [...rules, newRule];
    const newExpr = addRuleToExpr(logicExpr, newId);
    setRules(newRules);
    setLogicExpr(newExpr);
    emit(newRules, newExpr);
  };

  const removeRule = (id: number) => {
    const newRules = rules
      .filter((r) => r.id !== id)
      .map((r, i) => ({ ...r, id: i + 1 }));
    const newExpr = removeRuleFromExpr(logicExpr, id) || defaultExpr(newRules.length);
    setRules(newRules);
    setLogicExpr(newExpr);
    emit(newRules, newExpr);
  };

  const updateRule = (id: number, patch: Partial<FlatRule>) => {
    const newRules = rules.map((r) => r.id === id ? { ...r, ...patch } : r);
    setRules(newRules);
    emit(newRules, logicExpr);
  };

  const applyLogicExpr = (newExpr: string) => {
    setLogicExpr(newExpr);
    emit(rules, newExpr);
  };

  // ---- Field picker ----

  function FieldPicker({ ruleId, currentKey }: { ruleId: number; currentKey: string }) {
    const q = pickerQuery.trim().toLowerCase();
    const match = (f: FieldDef) =>
      !q ||
      (f.label || "").toLowerCase().includes(q) ||
      f.key.toLowerCase().includes(q) ||
      (f.group || "").toLowerCase().includes(q);

    return (
      <div className="absolute z-30 mt-1 w-[28rem] rounded-lg border bg-white shadow-xl p-2">
        <div className="p-2 pb-1">
          <input
            autoFocus
            className="w-full border rounded-md px-2 py-1 text-sm"
            placeholder="Search fields…"
            value={pickerQuery}
            onChange={(e) => setPickerQuery(e.target.value)}
          />
        </div>
        <div className="max-h-72 overflow-auto px-2 pb-2">
          {groups.map(([gname, flist]) => {
            const vis = flist.filter(match);
            if (!vis.length) return null;
            return (
              <div key={gname} className="mb-2">
                <div className="text-xs font-semibold text-gray-500 px-1 py-1">{gname}</div>
                <ul className="space-y-0.5">
                  {vis.map((f) => (
                    <li key={f.key}>
                      <button
                        className={`w-full text-left text-sm px-2 py-1 rounded hover:bg-gray-50 ${currentKey === f.key ? "bg-gray-100" : ""}`}
                        onClick={() => {
                          updateRule(ruleId, { field: f.key, operator: "equals", value: "" });
                          setPickerOpenFor(null);
                          setPickerQuery("");
                        }}
                      >
                        <span className="mr-2 text-gray-500 text-xs">{getFieldBadge(f)}</span>
                        <span className="font-medium">{f.label}</span>
                        <span className="ml-2 text-[11px] text-gray-400">{f.key}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
          {!groups.some(([, fl]) => fl.some(match)) && (
            <div className="text-sm text-gray-400 px-2 py-3">No matching fields</div>
          )}
        </div>
      </div>
    );
  }

  // ---- Value input for a rule ----
  // Text/number inputs use local state and only commit on blur or Enter,
  // so typing doesn't trigger emit (and thus a search) on every keystroke.

  function ValueInput({ rule }: { rule: FlatRule }) {
    const meta = fields.find((f) => f.key === rule.field);
    const kind = getKind(meta);

    if (kind === "boolean") {
      return (
        <select
          className="border rounded-md px-2 py-1.5 text-sm"
          value={String(rule.value ?? "true")}
          onChange={(e) => updateRule(rule.id, { value: e.target.value === "true" })}
        >
          <option value="true">True</option>
          <option value="false">False</option>
        </select>
      );
    }

    if (rule.operator === "is_empty" || rule.operator === "is_not_empty") return null;

    if (rule.operator === "between") {
      const [a, b] = Array.isArray(rule.value) ? rule.value : ["", ""];
      if (kind === "date") {
        return (
          <div className="flex items-center gap-1">
            <input type="date" className="border rounded-md px-2 py-1.5 text-sm"
              value={a || ""} onChange={(e) => updateRule(rule.id, { value: [e.target.value, b || ""] })} />
            <span className="text-gray-400 text-xs">–</span>
            <input type="date" className="border rounded-md px-2 py-1.5 text-sm"
              value={b || ""} onChange={(e) => updateRule(rule.id, { value: [a || "", e.target.value] })} />
          </div>
        );
      }
      // Between text/number: commit on blur or Enter
      return <BetweenInput rule={rule} kind={kind} a={a} b={b} />;
    }

    if (kind === "date") {
      return (
        <input type="date" className="border rounded-md px-2 py-1.5 text-sm"
          value={rule.value ?? ""}
          onChange={(e) => updateRule(rule.id, { value: e.target.value })} />
      );
    }

    // Text and number: commit on blur or Enter
    return <TextInput rule={rule} kind={kind} />;
  }

  function TextInput({ rule, kind }: { rule: FlatRule; kind: string }) {
    const [local, setLocal] = React.useState<string>(String(rule.value ?? ""));
    // Sync if rule.value changes externally (e.g. field picker reset)
    React.useEffect(() => { setLocal(String(rule.value ?? "")); }, [rule.value, rule.field, rule.operator]);

    const commit = () => updateRule(rule.id, { value: local });

    return (
      <input
        type={kind === "number" ? "number" : "text"}
        className={`border rounded-md px-2 py-1.5 text-sm ${kind === "number" ? "w-32" : "w-52"}`}
        placeholder={rule.operator === "in" || rule.operator === "not_in" ? "val1, val2, …" : undefined}
        value={local}
        onChange={(e) => setLocal(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } }}
      />
    );
  }

  function BetweenInput({ rule, kind, a, b }: { rule: FlatRule; kind: string; a: string; b: string }) {
    const [localA, setLocalA] = React.useState<string>(a ?? "");
    const [localB, setLocalB] = React.useState<string>(b ?? "");
    React.useEffect(() => { setLocalA(a ?? ""); setLocalB(b ?? ""); }, [a, b, rule.field, rule.operator]);

    const commitA = () => updateRule(rule.id, { value: [localA, localB] });
    const commitB = () => updateRule(rule.id, { value: [localA, localB] });

    return (
      <div className="flex items-center gap-1">
        <input type={kind === "number" ? "number" : "text"} className="border rounded-md px-2 py-1.5 text-sm w-24"
          value={localA}
          onChange={(e) => setLocalA(e.target.value)}
          onBlur={commitA}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commitA(); } }} />
        <span className="text-gray-400 text-xs">–</span>
        <input type={kind === "number" ? "number" : "text"} className="border rounded-md px-2 py-1.5 text-sm w-24"
          value={localB}
          onChange={(e) => setLocalB(e.target.value)}
          onBlur={commitB}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commitB(); } }} />
      </div>
    );
  }

  // ---- Operator selector for a rule ----

  function OpsSelect({ rule }: { rule: FlatRule }) {
    const meta = fields.find((f) => f.key === rule.field);
    const kind = getKind(meta);
    const ops = kind === "number" ? OPS_NUMBER : kind === "date" ? OPS_DATE : kind === "boolean" ? OPS_BOOL : OPS_STRING;
    return (
      <select
        className="border rounded-md px-2 py-1.5 text-sm"
        value={rule.operator}
        onChange={(e) => {
          const op = e.target.value;
          updateRule(rule.id, {
            operator: op,
            value: op === "between" ? ["", ""]
              : (op === "is_empty" || op === "is_not_empty") ? null
              : "",
          });
        }}
      >
        {ops.map((o) => <option key={o.v} value={o.v}>{o.label}</option>)}
      </select>
    );
  }

  // ---- Close picker on outside click ----

  React.useEffect(() => {
    const handler = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      if (t.closest(".filter-field-picker-anchor") || t.closest(".filter-field-picker-dropdown")) return;
      if (pickerOpenFor !== null) setPickerOpenFor(null);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [pickerOpenFor]);

  // ---- Render ----

  return (
    <div data-testid="filter-builder">
      {/* Rule list */}
      <div className="space-y-2">
        {rules.map((rule) => {
          const meta = fields.find((f) => f.key === rule.field);
          const needsValue = !["is_empty", "is_not_empty"].includes(rule.operator);
          const isEmpty = needsValue && (
            rule.value === null || rule.value === undefined || rule.value === "" ||
            (Array.isArray(rule.value) && rule.value.every((v: any) => v === "" || v == null))
          );
          return (
            <div key={rule.id} data-testid="filter-rule" className="flex flex-wrap items-center gap-2">
              {/* Number badge */}
              <span className="inline-flex items-center justify-center w-6 h-6 rounded-full bg-blue-100 text-blue-700 text-xs font-bold flex-shrink-0">
                {rule.id}
              </span>

              {/* Field picker */}
              <div className="relative filter-field-picker-anchor">
                <button
                  className="border rounded-md px-3 py-1.5 text-sm min-w-[16rem] text-left hover:bg-gray-50"
                  onClick={() => {
                    setPickerOpenFor(pickerOpenFor === rule.id ? null : rule.id);
                    setPickerQuery("");
                  }}
                >
                  <span className="text-gray-400 mr-1.5 text-xs">{getFieldBadge(meta || { key: rule.field })}</span>
                  <span className="font-medium">{meta?.label || rule.field || "Select field"}</span>
                </button>
                {pickerOpenFor === rule.id && (
                  <div className="filter-field-picker-dropdown">
                    <FieldPicker ruleId={rule.id} currentKey={rule.field} />
                  </div>
                )}
              </div>

              {/* Operator */}
              <OpsSelect rule={rule} />

              {/* Value */}
              <ValueInput rule={rule} />

              {/* Empty-value warning */}
              {isEmpty && (
                <span className="text-xs text-amber-600 bg-amber-50 border border-amber-200 rounded px-2 py-0.5" title="No value set — this filter will be skipped">
                  ⚠ will be skipped
                </span>
              )}

              {/* Remove */}
              <button
                data-testid="filter-rule-remove"
                className="ml-1 flex-shrink-0 text-gray-400 hover:text-red-500 rounded p-0.5 hover:bg-red-50 transition-colors"
                onClick={() => removeRule(rule.id)}
                title="Remove filter"
                aria-label="Remove filter"
              >
                <svg className="h-4 w-4" viewBox="0 0 16 16" fill="none" aria-hidden>
                  <path d="M2 2l12 12M14 2L2 14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
                </svg>
              </button>
            </div>
          );
        })}
      </div>

      {/* Logic expression bar (only when 2+ rules) */}
      {rules.length >= 2 && (
        <div data-testid="filter-logic-bar" className="mt-3 flex items-center gap-2 px-1">
          <span className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Include rows matching</span>
          <code data-testid="filter-logic-expr" className="text-sm font-mono text-blue-700 bg-blue-50 rounded px-2 py-0.5 border border-blue-100">
            {logicExpr || defaultExpr(rules.length)}
          </code>
          <button
            data-testid="filter-logic-edit-btn"
            className="inline-flex items-center gap-1 text-xs text-gray-500 hover:text-gray-800 rounded px-2 py-1 hover:bg-gray-100 border border-transparent hover:border-gray-200"
            onClick={() => setShowModal(true)}
            title="Edit filter logic"
          >
            <svg className="h-3.5 w-3.5" viewBox="0 0 16 16" fill="none" aria-hidden>
              <path d="M11.5 2.5l2 2-9 9H2.5v-2l9-9z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/>
            </svg>
            Edit logic
          </button>
        </div>
      )}

      {/* Add filter button */}
      <div className="mt-3">
        <button
          data-testid="filter-add-btn"
          className="inline-flex items-center gap-1.5 text-sm text-blue-600 hover:text-blue-800 rounded-md border border-blue-200 bg-blue-50 px-3 py-1.5 hover:bg-blue-100"
          onClick={addRule}
        >
          <svg className="h-4 w-4" viewBox="0 0 16 16" fill="none" aria-hidden>
            <path d="M8 2v12M2 8h12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
          </svg>
          Add filter
        </button>
      </div>

      {/* Logic expression modal */}
      <FilterLogicModal
        open={showModal}
        expr={logicExpr || defaultExpr(rules.length)}
        numRules={rules.length}
        ruleLabels={ruleLabels}
        onClose={() => setShowModal(false)}
        onApply={applyLogicExpr}
      />
    </div>
  );
}
