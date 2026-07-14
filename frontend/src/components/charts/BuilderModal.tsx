import React from "react";
import ModalFrame from "./ModalFrame";

/**
 * Modal del constructor genérico de gráficos, para quien NO tiene filas crudas
 * del Explorer (el chat: Moby entrega un dataset ya agregado, sin `rows`). Solo
 * aporta el chrome; el constructor es `CustomView`, que se pasa como children.
 */
export default function BuilderModal({
  open, onClose, title, onChangeTitle, children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  onChangeTitle?: (v: string) => void;
  children: React.ReactNode;
}) {
  return (
    <ModalFrame
      open={open}
      onClose={onClose}
      title={title}
      onChangeTitle={onChangeTitle}
      testId="chart-builder-modal"
    >
      {children}
    </ModalFrame>
  );
}
