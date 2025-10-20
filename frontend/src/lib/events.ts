// Dispara un CustomEvent y además usa localStorage para “despertar” otras pestañas
const LS_KEY = "__cts:explorer:changed";

export function broadcastExplorerChange() {
  // 1) Event para la misma pestaña / SPA
  window.dispatchEvent(new CustomEvent("cts:explorer:changed"));

  // 2) Storage ping para otras pestañas
  try {
    localStorage.setItem(LS_KEY, String(Date.now()));
    // limpiar para no ensuciar
    setTimeout(() => localStorage.removeItem(LS_KEY), 0);
  } catch {}
}

export function listenExplorerChange(cb: () => void) {
  const onEvt = () => cb();
  const onStorage = (e: StorageEvent) => {
    if (e.key === LS_KEY) cb();
  };
  window.addEventListener("cts:explorer:changed", onEvt);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener("cts:explorer:changed", onEvt);
    window.removeEventListener("storage", onStorage);
  };
}