import React, { useEffect } from "react";

/**
 * Chrome compartido por los dos modales de gráfico: el contenedor con pestañas
 * del Explorer (ChartModal) y el constructor suelto del chat (BuilderModal).
 * Vive aparte para que ambos tengan EXACTAMENTE el mismo comportamiento de
 * altura acotada, scroll interno, cabecera siempre alcanzable y cierre con Escape.
 */
export default function ModalFrame({
  open, onClose, title, onChangeTitle, testId, toolbar, children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  onChangeTitle?: (v: string) => void;
  testId: string;
  /** Franja opcional bajo la cabecera (p.ej. las pestañas). No hace scroll. */
  toolbar?: React.ReactNode;
  children: React.ReactNode;
}) {
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
    // z-[11000] es el nivel que tenía el modal viejo, y no es arbitrario: por
    // debajo quedan el pill flotante de Nearby (z-[9050]), su drawer (z-[9010]) y
    // el overlay del drawer (z-[9000], con pointer-events). Con un z bajo el pill
    // se pinta sobre el modal y un click suyo lo deja inalcanzable.
    <div
      data-testid="chart-modal-overlay"
      className="fixed inset-0 z-[11000] flex items-center justify-center bg-black/30 p-4"
    >
      <div
        className="flex max-h-[90vh] w-full max-w-6xl flex-col rounded-xl bg-white shadow-xl"
        data-testid={testId}
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

        {toolbar}

        <div className="overflow-y-auto p-4">{children}</div>
      </div>
    </div>
  );
}
