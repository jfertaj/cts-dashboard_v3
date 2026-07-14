import React, { useEffect, useState } from "react";
import type { DataRow } from "../../lib/rowAccess";
import CountriesView from "./CountriesView";
import RankingView from "./RankingView";
import DistributionView from "./DistributionView";
import FunnelView from "./FunnelView";
import ModalFrame from "./ModalFrame";

export type ChartTab = "countries" | "ranking" | "distribution" | "funnel" | "custom";

const TABS: Array<{ key: ChartTab; label: string }> = [
  { key: "countries", label: "Countries" },
  { key: "ranking", label: "Ranking" },
  { key: "distribution", label: "Distribution" },
  { key: "funnel", label: "Funnel" },
  { key: "custom", label: "Custom" },
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

  const tabs = (
    <div role="tablist" className="flex shrink-0 gap-1 border-b px-4 pt-2">
      {TABS.map((t) => (
        <button
          key={t.key}
          role="tab"
          // Las cinco pestañas están siempre visibles: `aria-selected` es lo único
          // que distingue cuál está activa (y lo único que puede aseverar el E2E).
          aria-selected={tab === t.key}
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
  );

  return (
    <ModalFrame
      open={open}
      onClose={onClose}
      title={title}
      onChangeTitle={onChangeTitle}
      testId="chart-modal"
      toolbar={tabs}
    >
      {/* Guard de 0 filas: las vistas dicen "ningún centro reporta esta métrica",
          consejo que sería FALSO cuando el problema real es que la búsqueda no
          devolvió nada. Con rows vacío NO se renderiza ninguna vista. */}
      {rows.length === 0 ? (
        <p data-testid="chart-no-rows" className="py-16 text-center text-gray-500">
          No rows in the current result. Adjust the Explorer filters.
        </p>
      ) : (
        <>
          {tab === "countries" && <CountriesView rows={rows} />}
          {tab === "ranking" && <RankingView rows={rows} />}
          {tab === "distribution" && <DistributionView rows={rows} />}
          {tab === "funnel" && <FunnelView rows={rows} />}
          {tab === "custom" && (
            <div className="space-y-3">
              {/* El constructor ya NO rellena con ceros (buildChartDataset devuelve
                  null para el que no reporta), pero tampoco declara su cobertura
                  como las otras cuatro vistas: el hueco hay que nombrarlo, o el
                  usuario lo lee como "no lo han pintado" en vez de "no hay dato". */}
              <p data-testid="chart-custom-note" className="text-sm text-gray-600">
                A site that does not report a metric is left as a gap, not as a zero: no bar
                is drawn for it. Unlike Countries, Ranking, Distribution and Funnel, this tab
                does not state how many sites are left out.
              </p>
              {custom}
            </div>
          )}
        </>
      )}
    </ModalFrame>
  );
}
