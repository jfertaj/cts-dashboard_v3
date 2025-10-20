// src/pages/ChatView.tsx
import React, { useEffect, useMemo, useRef, useState } from "react";
import { askAI, ChatResponse, getFieldsIndex } from "../lib/ai";
import ChartModal from "../components/ChartModal";
import Moby from "../assets/Moby.png";
import AIResultTable from "../components/AIResultTable";
import { findMatchingAliases, AliasEntry } from "../alias/qualificationAliasPack";

type Msg = { role: "user" | "assistant"; content: React.ReactNode };

export default function ChatView() {
  // ——— Estilos para HTML del asistente (mejora listas y lectura) ———
  const ASSISTANT_HTML_CLS = [
    "text-sm text-gray-800 leading-6 max-w-[68ch]",
    "prose prose-sm prose-blue",
    "prose-headings:mt-3 prose-p:my-2",
    "prose-ul:my-2 prose-ol:my-2",
    "prose-ul:ml-5 prose-ol:ml-5",
    "prose-ul:list-disc prose-ol:list-decimal",
    "prose-li:my-0.5",
    "prose-ul:space-y-1 prose-ol:space-y-1",
    "prose-li:marker:text-gray-500",
  ].join(" ");

  // --- persistence keys ---
  const MSGS_KEY = "moby_chat_messages_v1";
  const INPUT_KEY = "moby_chat_input_v1";
  const TABLE_KEY = "moby_last_table_v1";

  const [messages, setMessages] = useState<Msg[]>([
    {
      role: "assistant",
      content: (
        <>
          Hi, I’m Moby{" "}
          <img
            src={Moby}
            alt="Moby the cat"
            width={22}
            style={{ verticalAlign: "middle", display: "inline-block" }}
          />{" "}
          What would you like to explore or visualize?
        </>
      ),
    },
  ]);

  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Chart state (reuse your ChartModal)
  type ChartType = "bar" | "line";
  const [chartOpen, setChartOpen] = useState(false);
  const [chartType, setChartType] = useState<ChartType>("bar");
  const [chartTitle, setChartTitle] = useState<string>("Explorer Chart");
  const [chartXKey, setChartXKey] = useState<string>("sf.Account.Name");
  const [chartYKeys, setChartYKeys] = useState<string[]>([]);
  const [chartData, setChartData] = useState<Array<Record<string, any>>>([]);
  const labelByKey = useMemo(() => new Map<string, string>(), []);

  // ---- Explorer field catalog (para saber qué columnas reconoce) ----
  const [explorerFields, setExplorerFields] = useState<
    { key: string; type?: string; label?: string }[]
  >([]);
  const explorerKeySet = useMemo(
    () => new Set(explorerFields.map((f) => f.key)),
    [explorerFields]
  );

  useEffect(() => {
    (async () => {
      try {
        const res = await fetch("/api/explorer/fields");
        const data = await res.json();
        setExplorerFields(data.fields || []);
      } catch {}
    })();
  }, []);

  // --- auto-scroll ---
  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages, busy]);

  // --- restore from sessionStorage on mount (only text messages to avoid serializing React nodes) ---
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(MSGS_KEY);
      if (raw) {
        const saved: Array<{ role: "user" | "assistant"; content: string | null }> =
          JSON.parse(raw);
        const rebuilt: Msg[] = saved
          .filter((m) => typeof m.content === "string" && m.content !== null)
          .map((m) => ({ role: m.role, content: m.content as string }));
        if (rebuilt.length) setMessages(rebuilt);
      }
      const inputSaved = sessionStorage.getItem(INPUT_KEY);
      if (inputSaved) setInput(inputSaved);
    } catch {}
  }, []);

  // --- restore last table if present (con botones) ---
  const restoredTableRef = useRef(false);
  useEffect(() => {
    if (restoredTableRef.current) return;
    if (explorerFields.length === 0) return; // esperamos a tener catálogo para mostrar botones correctos
    try {
      const raw = sessionStorage.getItem(TABLE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed.columns && parsed.rows) {
          setMessages((m) => [
            ...m,
            {
              role: "assistant",
              content: (
                <ActionableTable
                  columns={parsed.columns}
                  rows={parsed.rows}
                  explorerKeySet={explorerKeySet}
                />
              ),
            },
          ]);
          restoredTableRef.current = true;
        }
      }
    } catch {}
  }, [explorerFields, explorerKeySet]);

  // --- persist on change ---
  useEffect(() => {
    try {
      const serializable = messages.map((m) => ({
        role: m.role,
        content: typeof m.content === "string" ? (m.content as string) : null,
      }));
      sessionStorage.setItem(MSGS_KEY, JSON.stringify(serializable));
    } catch {}
  }, [messages]);

  useEffect(() => {
    try {
      sessionStorage.setItem(INPUT_KEY, input);
    } catch {}
  }, [input]);

  // --------- Componente interno: tabla con botones para Explorer ----------
  function ActionableTable({
    columns,
    rows,
    explorerKeySet,
  }: {
    columns: string[] | Array<{ key: string; label?: string }>;
    rows: Array<Record<string, any>>;
    explorerKeySet: Set<string>;
  }) {
    // normaliza columns a array de keys
    const colKeys: string[] = Array.isArray(columns)
      ? typeof columns[0] === "string"
        ? (columns as string[])
        : (columns as Array<{ key: string }>).map((c) => c.key)
      : [];

    const knownCols = colKeys.filter((c) => explorerKeySet.has(c));
    const hasKnownCols = knownCols.length > 0;

    // buscamos account ids en diferentes formatos
    const accIds: string[] = (rows || [])
      .map(
        (r) =>
          r["sf.Account.Id"] ||
          r["sf.AccountId"] ||
          r["Account.Id"] ||
          r["account_id"] ||
          (r?.data?.["sf.Account.Id"] || r?.data?.["sf.AccountId"])
      )
      .filter(Boolean)
      .map(String);

    const handleHighlight = () => {
      if (!accIds.length) return;
      window.dispatchEvent(
        new CustomEvent("cts:chat:explorer:highlight", {
          detail: { account_ids: accIds },
        })
      );
    };

    const handleOpenFiltered = () => {
      if (!accIds.length) return;
      const detail = {
        filters: {
          logic: "AND",
          rules: [{ field: "sf.Account.Id", operator: "in", value: accIds }],
        },
      };
      try {
        sessionStorage.setItem(
          "cts:explorer:pending:set",
          JSON.stringify(detail)
        );
      } catch {}
      window.dispatchEvent(
        new CustomEvent("cts:chat:explorer:set", { detail })
      );
      setTimeout(() => {
        if (!document.body.getAttribute("data-explorer-listening")) {
          window.location.href = "/explorer";
        }
      }, 50);
    };

    const handleAddColumns = () => {
      if (!hasKnownCols) return;
      window.dispatchEvent(
        new CustomEvent("cts:chat:explorer:columns:add", {
          detail: { columns: knownCols },
        })
      );
    };

    return (
      <div className="space-y-2">
        <AIResultTable columns={columns as any} rows={rows} />
        {(accIds.length > 0 || hasKnownCols) && (
          <div className="flex flex-wrap gap-2 pt-1">
            {accIds.length > 0 && (
              <>
                <button
                  className="rounded-md border px-2.5 py-1.5 text-xs hover:bg-gray-50"
                  onClick={handleHighlight}
                  title="Resaltar estos centros en el mapa/tabla del Explorer"
                >
                  🔦 Highlight en Explorer
                </button>
                <button
                  className="rounded-md border px-2.5 py-1.5 text-xs hover:bg-gray-50"
                  onClick={handleOpenFiltered}
                  title="Abrir Explorer filtrado a estos centros"
                >
                  🎯 Abrir en Explorer (filtrar)
                </button>
              </>
            )}
            {hasKnownCols && (
              <button
                className="rounded-md border px-2.5 py-1.5 text-xs hover:bg-gray-50"
                onClick={handleAddColumns}
                title="Añadir estas columnas a la tabla del Explorer"
              >
                ➕ Añadir columnas al Explorer
              </button>
            )}
          </div>
        )}
      </div>
    );
  }

  // --------------------- ARTIFACTS HANDLER ---------------------
  const handleArtifacts = (resp: ChatResponse) => {
    // Helpers
    const asNumber = (v: any) => {
      if (typeof v === "number") return v;
      if (v === null || v === undefined) return NaN;
      const n = parseFloat(String(v).replace(/,/g, ""));
      return Number.isFinite(n) ? n : NaN;
    };
    const coerceNumeric = (
      rows: Array<Record<string, any>>,
      yKeys: string[]
    ) => {
      if (!Array.isArray(rows) || !rows.length || !yKeys?.length) return rows || [];
      const Y = new Set(yKeys);
      return rows.map((r) => {
        const out: Record<string, any> = { ...r };
        for (const k of Y) {
          const n = asNumber(out[k]);
          out[k] = Number.isFinite(n) ? n : 0;
        }
        return out;
      });
    };
    const autoYKeys = (rows: Array<Record<string, any>>, xKey: string) => {
      if (!Array.isArray(rows) || !rows.length) return [] as string[];
      const keys = Object.keys(rows[0] || {}).filter((k) => k !== xKey);
      const numeric = keys.filter((k) => Number.isFinite(asNumber(rows[0][k])));
      const preferred = [
        "sf.C_Number_of_T1D_Patients_currently_U_18__c",
        "sf.C_Number_of_T1D_Patients_currently_O_18__c",
        "t1d_u18",
        "t1d_o18",
        "Stage1",
        "Stage2",
      ].filter((k) => numeric.includes(k));
      const fallback = numeric.filter((k) => !preferred.includes(k));
      return preferred.concat(fallback).slice(0, 2);
    };

    // 1) Texto
    const answerText =
      (resp as any)?.answer_md ?? (resp as any)?.answer ?? (resp as any)?.text ?? null;
    if (typeof answerText === "string" && answerText.trim()) {
      setMessages((m) => [...m, { role: "assistant", content: answerText.trim() }]);
    }

    // 2) Tabla
    if ((resp as any)?.table?.columns && (resp as any)?.table?.rows) {
      const { columns, rows } = (resp as any).table;
      try {
        const accountIds = (rows || [])
          .map(
            (r: any) =>
              r["sf.Account.Id"] ||
              r["sf.AccountId"] ||
              r?.data?.["sf.Account.Id"] ||
              r?.data?.["sf.AccountId"] ||
              r?.account_id
          )
          .filter(Boolean);
        sessionStorage.setItem(
          TABLE_KEY,
          JSON.stringify({ columns, rows, account_ids: accountIds })
        );
      } catch {}
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content: (
            <ActionableTable
              columns={columns}
              rows={rows}
              explorerKeySet={explorerKeySet}
            />
          ),
        },
      ]);
    }

    // 3) Visualizaciones
    if (resp?.visualization?.data && resp.visualization?.xKey) {
      const v = resp.visualization as any;
      const type: ChartType = v.type === "line" ? "line" : "bar";
      const xKey: string = v.xKey;
      let yKeys: string[] = Array.isArray(v.yKeys) ? v.yKeys : [];
      let data: Array<Record<string, any>> = Array.isArray(v.data) ? v.data : [];
      if (!yKeys.length) yKeys = autoYKeys(data, xKey);
      data = coerceNumeric(data, yKeys);
      setChartType(type);
      setChartXKey(xKey);
      setChartYKeys(yKeys);
      setChartData(data);
      setChartTitle(v.meta?.title || "Explorer Chart");
      setChartOpen(true);
      return;
    }

    // 4) Retrocompat
    const arts = (resp as any)?.artifacts;
    if (Array.isArray(arts)) {
      for (const a of arts) {
        if (a?.type === "chart") {
          const v = a.data || a.options || {};
          const type: ChartType =
            a.chart_kind === "line" || v.type === "line" ? "line" : "bar";
          if (v?.xKey && v?.data) {
            let yKeys: string[] = Array.isArray(v.yKeys) ? v.yKeys : [];
            let data: Array<Record<string, any>> = Array.isArray(v.data)
              ? v.data
              : [];
            if (!yKeys.length) yKeys = autoYKeys(data, v.xKey);
            data = coerceNumeric(data, yKeys);
            setChartType(type);
            setChartXKey(v.xKey);
            setChartYKeys(yKeys);
            setChartData(data);
            setChartTitle(v.meta?.title || "Explorer Chart");
            setChartOpen(true);
          }
        }
        if (a?.type === "explorer_action" && a?.action === "set_filters") {
          window.dispatchEvent(
            new CustomEvent("cts:chat:explorer:set", { detail: a.payload })
          );
        }
      }
    }
  };

  // --------------------- SEND ---------------------
  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    setMessages((m) => [...m, { role: "user", content: text }]);
    setInput("");
    setBusy(true);
    try {
      // FOLLOW-UP RESOLVER (antes de ir al LLM)
      const handled = await tryFollowUp(text);
      if (handled) {
        setBusy(false);
        return;
      }
      // LLM
      const resp: ChatResponse = await askAI(text);
      handleArtifacts(resp);
    } catch (e: any) {
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content: `⚠️ Error contacting Moby: ${e?.message || "Unknown error"}`,
        },
      ]);
    } finally {
      setBusy(false);
    }
  };

  // ---------- Follow-up resolver ----------
  function isFollowUpText(q: string): boolean {
    const s = q.toLowerCase();
    return (
      /(de esos|de esas|de los anteriores|de las anteriores|de los 5|de los cinco|de los resultados|de arriba|sobre esos|sobre las anteriores)/i.test(
        s
      ) ||
      /(of those|from those|among those|of the previous|in the previous|those ones|the above|from the list)/i.test(
        s
      ) ||
      /(de ceux|de celles|parmi ceux|des précédents|parmi les précédents|dans les précédents|ceux-ci|celles-ci|ci-dessus)/i.test(
        s
      ) ||
      /(di quelli|di quelle|tra quelli|tra le precedenti|dei precedenti|nelle precedenti|sopra citati)/i.test(
        s
      ) ||
      /(von diesen|aus diesen|unter diesen|von den vorherigen|unter den vorherigen|oben genannten|oben erwähnten)/i.test(
        s
      ) ||
      /(van die|van deze|uit die|uit deze|onder die|onder deze|van de vorige|uit de vorige|bovenstaande|hierboven|de voorgaande|de eerdere)/i.test(
        s
      )
    );
  }

  // ---------- Helpers de intención ----------
  const DIST_RE =
    /(?:within|≤|<=|radius|radio|distancia|a)\s*(?:de\s*)?(\d{2,4})\s*km/i;
  function extractDistance(q: string): number | null {
    const m = DIST_RE.exec(q);
    return m ? Number(m[1]) : null;
  }

  // Sinónimos rápidos (aún usados por el fuzzy fallback)
  const KEYWORD_ALIASES: Array<{ rx: RegExp; prefer: string[] }> = [
    {
      rx: /\b(farmacia|pharmacy|dispensary)\b.*\b(on[\s-]?site|interna|propia)?/i,
      prefer: [
        "qual.onsite_pharmacy",
        "qual.pharmacy_available",
        "qual.comments_pharmacy",
      ],
    },
    {
      rx: /\b(pernocta|overnight\s*stay|ingreso\s*nocturno)\b/i,
      prefer: [
        "qual.overnight_stay",
        "qual.beds_available",
        "qual.comments_facilities",
      ],
    },
    { rx: /\b(hla\s*typing)\b/i, prefer: ["qual.C_Is_HLA_typing_performed__c"] },
  ];

  // Descubre si la frase parece “consulta textual” (no se usa directamente pero lo dejamos por si lo extiendes)
  function looksTextQuery(q: string): boolean {
    const s = q.toLowerCase();
    const hasNumber = /\d/.test(s);
    const hasComparator =
      /(>=|≤|<=|>|<|=|at least|como mínimo|más de|menos de|greater than|less than)/i.test(
        s
      );
    return !hasNumber && !hasComparator;
  }

  async function findFieldKeysByFuzzy(
    query: string,
    max: number = 6
  ): Promise<string[]> {
    const needle = query.toLowerCase();
    const fields = await getFieldsIndex();
    // 0) preferencia por algunos alias simples
    for (const a of KEYWORD_ALIASES) {
      if (a.rx.test(query)) return a.prefer;
    }
    const scored: Array<{ key: string; score: number }> = [];
    for (const f of fields) {
      const hay = `${f.label ?? ""} ${f.key}`.toLowerCase();
      const tokens = needle.split(/[^a-z0-9_]+/i).filter(Boolean);
      const ok = tokens.every((t) => hay.includes(t));
      if (ok) scored.push({ key: f.key, score: hay.indexOf(tokens[0]) });
    }
    // prioriza qual.* y extra.*
    scored.sort((a, b) => {
      const pa = a.key.startsWith("qual.")
        ? 0
        : a.key.startsWith("extra.")
        ? 1
        : 2;
      const pb = b.key.startsWith("qual.")
        ? 0
        : b.key.startsWith("extra.")
        ? 1
        : 2;
      return pa - pb || a.score - b.score;
    });
    return scored.slice(0, max).map((x) => x.key);
  }

  type Rule = { field: string; operator: string; value?: any };

  // Aplica reglas “contains / = / >= …” en el cliente (por si el backend no soporta todas)
  function applyRulesClient(rows: any[], rules: Rule[]): any[] {
    const cmp = (a: any, op: string, b: any) => {
      if (op === "contains")
        return String(a ?? "")
          .toLowerCase()
          .includes(String(b ?? "").toLowerCase());
      if (op === "=") return String(a) === String(b);
      const na = Number(a),
        nb = Number(b);
      if (!Number.isFinite(na) || !Number.isFinite(nb)) return false;
      if (op === ">=") return na >= nb;
      if (op === ">") return na > nb;
      if (op === "<=") return na <= nb;
      if (op === "<") return na < nb;
      return false;
    };
    return rows.filter((r) => rules.every((rule) => cmp(r[rule.field], rule.operator, rule.value)));
  }

  // ---- helper: comparadores desde texto
  function parseComparatorFromText(
    raw: string
  ): { op: string; value: number } | null {
    const s = raw
      .toLowerCase()
      .replace(/\bcomo mínimo\b|al menos/gi, ">=")
      .replace(/\bmás de\b/gi, ">")
      .replace(/\bmenos de\b/gi, "<")
      .replace(/≥/g, ">=")
      .replace(/≤/g, "<=");
    const m = /(>=|<=|>|<|=)?\s*([\d.,]+)/.exec(s);
    if (!m) return null;
    const op = (m[1] || ">=").trim();
    const value = parseFloat(m[2].replace(",", ""));
    return Number.isFinite(value) ? { op, value } : null;
  }

  async function tryFollowUp(q: string): Promise<boolean> {
    try {
      // 1) ¿Es follow-up (o tiene radio km)?
      const distanceRe =
        /(?:within|radius|radio|distancia\s*(?:de)?|a)\s*(?:de\s*)?(\d{2,4})\s*km/i;
      const isFU = isFollowUpText(q) || distanceRe.test(q);
      if (!isFU) return false;

      // 2) Última tabla → account_ids
      const saved = sessionStorage.getItem(TABLE_KEY);
      if (!saved) return false;
      const parsed = JSON.parse(saved);
      const account_ids: string[] = (parsed?.account_ids || [])
        .map(String)
        .filter(Boolean);
      if (!account_ids.length) return false;

      // 3) Radio
      const m = /(\d{2,4})\s*km/i.exec(q);
      const maxKm = m ? Number(m[1]) : 120;
      const baseAccount = String(account_ids[0]);

      // 4) Construcción **genérica** de filtros
      const rules: any[] = [];

      // 4.a) Comparadores numéricos (fallback por fuzzy)
      {
        const cmp = q
          .toLowerCase()
          .replace("como mínimo", ">=")
          .replace("al menos", ">=")
          .replace("más de", ">")
          .replace("menos de", "<");
        const numRe =
          /(>=|<=|>|<|=)?\s*([\d.,]+)\s*(patients?|pacientes|stage1|stage2|u18|o18)?/i;
        const numMatch = cmp.match(numRe);
        if (numMatch) {
          const op = (numMatch[1] || ">=").trim();
          const val = parseFloat(numMatch[2].replace(",", ""));
          const fields = await findFieldKeysByFuzzy(q);
          for (const f of fields)
            rules.push({ field: f, operator: op, value: val });
        }
      }

      // 4.b) Reglas desde el alias pack (qual y/o SF)
      {
        const matches: AliasEntry[] = findMatchingAliases(q);

        for (const a of matches) {
          // BOOLEAN
          if (a.type === "boolean") {
            const neg = /\b(no|not|sin|without|doesn['’]?t|don['’]?t|ninguna?)\b/i.test(
              q
            );
            for (const key of a.prefer) {
              rules.push({ field: key, operator: "=", value: !neg });
            }
            continue;
          }

          // NUMBER
          if (a.type === "number") {
            const parsedCmp = parseComparatorFromText(q);
            if (parsedCmp) {
              for (const key of a.prefer) {
                rules.push({
                  field: key,
                  operator: parsedCmp.op,
                  value: parsedCmp.value,
                });
              }
            }
            continue;
          }

          // TEXT
          if (a.type === "text") {
            const termMatch = q.match(
              /(?:que\s+ponga|that\s+says|contenga|contains)\s+["“”']?([^"“”']+)["“”']?/i
            );
            const term = (termMatch?.[1] || q).trim();
            for (const key of a.prefer) {
              rules.push({ field: key, operator: "ilike", value: `%${term}%` });
            }
          }
        }
      }

      // 4.c) Texto libre en comentarios (fallback)
      if (/comentari|comment/i.test(q)) {
        const comments = (await getFieldsIndex())
          .filter(
            (f) => /comment/i.test(f.key) || /comment/i.test(f.label || "")
          )
          .slice(0, 5)
          .map((f) => f.key);
        const term = q.replace(/.*comentari[oa]s?:?/i, "").trim();
        for (const k of comments)
          rules.push({ field: k, operator: "ilike", value: `%${term}%` });
      }

      // 4.d) Normaliza operadores si tu backend lo necesita (opcional)
      for (const r of rules) {
        if (r.operator === "contains") {
          r.operator = "ilike";
          r.value = `%${r.value}%`;
        }
      }

      const cleanRules = rules.filter(Boolean);
      const filters = cleanRules.length
        ? { logic: "AND", rules: cleanRules }
        : { logic: "AND", rules: [] };

      // 5) Columnas: añade SF + qual detectadas
      const columns = Array.from(
        new Set([
          "sf.Account.Id",
          "sf.Account.Name",
          "sf.Account.ShippingCountry",
          "sf.Account.ShippingCity",
          ...(cleanRules.map((r: any) => r.field).filter(Boolean) as string[]),
        ])
      );

      // 6) Llamada
      const res = await fetch("/api/explorer/search/within-drive-km", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_account_id: baseAccount,
          max_km: maxKm,
          filters,
          columns,
        }),
      });
      const data = await res.json();
      const rows = data?.rows ?? [];

      const columnsOut = rows.length
        ? Array.from(
            new Set(
              rows.flatMap((r: any) => Object.keys(r))
            )
          ).map((k) => ({ key: k, label: k }))
        : [{ key: "account_id", label: "Account Id" }];

      sessionStorage.setItem(
        TABLE_KEY,
        JSON.stringify({ columns: columnsOut, rows, account_ids })
      );

      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content: rows.length
            ? `Encontré ${rows.length} centro(s) dentro de ≤ ${maxKm} km.`
            : "No se han encontrado centros con esos criterios.",
        },
        {
          role: "assistant",
          content: (
            <ActionableTable
              columns={columnsOut as any}
              rows={rows}
              explorerKeySet={explorerKeySet}
            />
          ),
        },
      ]);
      return true;
    } catch {
      return false;
    }
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  const clearConversation = () => {
    setMessages([
      {
        role: "assistant",
        content: (
          <>
            Hi, I’m Moby{" "}
            <img
              src={Moby}
              alt="Moby the cat"
              width={22}
              style={{ verticalAlign: "middle", display: "inline-block" }}
            />{" "}
            What would you like to explore or visualize?
          </>
        ),
      },
    ]);
    try {
      sessionStorage.removeItem(MSGS_KEY);
      sessionStorage.removeItem(INPUT_KEY);
      // sessionStorage.removeItem(TABLE_KEY); // opcional
    } catch {}
  };

  // ---- Render helper: formatea strings con saltos de línea y ancho legible ----
  const renderContent = (node: React.ReactNode) => {
    if (typeof node === "string") {
      const looksHtml = /<\/?[a-z][\s\S]*>/i.test(node);
      if (looksHtml) {
        return (
          <div
            className={ASSISTANT_HTML_CLS}
            dangerouslySetInnerHTML={{ __html: node }}
          />
        );
      }
      return (
        <div className="text-sm text-gray-800 whitespace-pre-wrap leading-6 max-w-[68ch]">
          {node}
        </div>
      );
    }
    return node;
  };

  return (
    <div className="w-full max-w-[96rem] mx-auto px-6 py-6">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-gray-900 flex items-center gap-2">
            Moby (Chat)
            <img
              src={Moby}
              alt="Moby the cat"
              width={24}
              height={24}
              className="inline-block align-middle"
            />
          </h1>
          <p className="text-sm text-gray-600">
            Type your question in natural language. Moby can query data, adjust
            Explorer filters, and open charts.
          </p>
        </div>
        <button
          className="rounded-md border px-3 py-1.5 text-sm hover:bg-gray-50"
          onClick={clearConversation}
          disabled={busy}
          title="Clear conversation"
        >
          Clear conversation
        </button>
      </div>

      <div className="grid grid-rows-[1fr_auto] h-[72vh] rounded-xl border bg-white shadow-sm overflow-hidden">
        {/* Messages */}
        <div ref={scrollRef} className="overflow-auto p-4 space-y-3">
          {messages.map((m, i) => (
            <div
              key={i}
              className={`max-w-[70ch] ${m.role === "user" ? "ml-auto text-right" : ""}`}
            >
              <div
                className={`inline-block px-3 py-2 rounded-lg text-sm ${
                  m.role === "user"
                    ? "bg-blue-600 text-white"
                    : "bg-gray-100 text-gray-800"
                }`}
                style={{ wordBreak: "break-word" }}
              >
                {renderContent(m.content)}
              </div>
            </div>
          ))}
          {busy && <div className="text-sm text-gray-500">Moby is thinking…</div>}
        </div>

        {/* Input */}
        <div className="border-t bg-gray-50 p-3">
          <div className="flex items-center gap-2">
            <input
              className="flex-1 rounded-lg border px-3 py-2 text-sm focus:ring-2 focus:ring-[#0072CE] bg-white"
              placeholder="Ask Moby…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              disabled={busy}
              aria-label="Ask Moby"
            />
            <button
              className="rounded-lg bg-[#0072CE] text-white px-4 py-2 text-sm font-medium hover:opacity-90 disabled:opacity-60"
              onClick={send}
              disabled={busy || input.trim().length === 0}
            >
              Send
            </button>
          </div>
        </div>
      </div>

      {/* Chart modal */}
      <ChartModal
        open={chartOpen}
        onClose={() => setChartOpen(false)}
        title={chartTitle}
        data={chartData}
        xKey={chartXKey}
        yKeys={chartYKeys}
        type={chartType}
        xCandidates={[]}
        yCandidates={[]}
        labelByKey={labelByKey}
        onChangeTitle={setChartTitle}
        onChangeType={(t) => setChartType(t as ChartType)}
        onChangeXKey={() => {}}
        onToggleYKey={() => {}}
      />
    </div>
  );
}