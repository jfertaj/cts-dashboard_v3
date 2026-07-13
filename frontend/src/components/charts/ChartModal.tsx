import React, { useEffect, useState } from "react";
import type { DataRow } from "../../lib/rowAccess";
import CountriesView from "./CountriesView";
import RankingView from "./RankingView";
import DistributionView from "./DistributionView";
import FunnelView from "./FunnelView";

export type ChartTab = "countries" | "ranking" | "distribution" | "funnel" | "custom";

const TABS: Array<{ key: ChartTab; label: string }> = [
  { key: "countries", label: "Países" },
  { key: "ranking", label: "Ranking" },
  { key: "distribution", label: "Distribución" },
  { key: "funnel", label: "Embudo" },
  { key: "custom", label: "Personalizado" },
];

export default function ChartModal({
  open, onClose, title, onChangeTitle, rows, custom,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  onChangeTitle?: (v: string) => void;
  rows: DataRow[];
  custom: React.ReactNode;
}) {
  const [tab, setTab] = useState<ChartTab>("countries");
  useEffect(() => { if (open) setTab("countries"); }, [open]);

  // Segunda vía de cierre: si el panel crece más que el viewport (Ranking con
  // N alto, p.ej.) el usuario debe poder salir sin depender de que el botón
  // Close siga visible. Solo escucha mientras el modal está abierto.
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div
        className="flex max-h-[90vh] w-full max-w-6xl flex-col rounded-xl bg-white shadow-xl"
        data-testid="chart-modal"
      >
        <div className="flex shrink-0 items-center justify-between border-b px-4 py-3">
          {onChangeTitle ? (
            <input
              className="min-w-0 flex-1 rounded-md border px-2 py-1 font-semibold"
              value={title}
              onChange={(e) => onChangeTitle(e.target.value)}
            />
          ) : (
            <h2 className="font-semibold">{title}</h2>
          )}
          <button className="ml-3 rounded-md border px-3 py-1 text-sm hover:bg-gray-50" onClick={onClose}>
            Close
          </button>
        </div>

        <div className="flex shrink-0 gap-1 border-b px-4 pt-2">
          {TABS.map((t) => (
            <button
              key={t.key}
              data-testid={`chart-tab-${t.key}`}
              onClick={() => setTab(t.key)}
              className={`rounded-t-md px-3 py-1.5 text-sm ${
                tab === t.key
                  ? "border border-b-white bg-white font-medium text-violet-700"
                  : "text-gray-600 hover:bg-gray-50"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="overflow-y-auto p-4">
          {/* Guard de 0 filas: las vistas dicen "ningún centro reporta esta métrica",
              consejo que sería FALSO cuando el problema real es que la búsqueda no
              devolvió nada. Con rows vacío NO se renderiza ninguna vista. */}
          {rows.length === 0 ? (
            <p className="py-16 text-center text-gray-500">
              No hay filas en el resultado actual. Ajusta los filtros del Explorer.
            </p>
          ) : (
            <>
              {tab === "countries" && <CountriesView rows={rows} />}
              {tab === "ranking" && <RankingView rows={rows} />}
              {tab === "distribution" && <DistributionView rows={rows} />}
              {tab === "funnel" && <FunnelView rows={rows} />}
              {tab === "custom" && custom}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
