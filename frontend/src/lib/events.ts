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

// ---- Auth session change (global 401 / login state) ----
// Single source of truth for the "app-auth" event name + detail shape so the
// dispatcher (lib/api.ts on 401) and listeners (useAuth, Header) cannot drift.
export type AuthChangeDetail = { ok: boolean };
const APP_AUTH_EVENT = "app-auth";

export function broadcastAuthChange(ok: boolean) {
  window.dispatchEvent(new CustomEvent<AuthChangeDetail>(APP_AUTH_EVENT, { detail: { ok } }));
}

export function listenAuthChange(cb: (ok: boolean) => void) {
  const onEvt = (e: Event) => {
    const detail = (e as CustomEvent<AuthChangeDetail>).detail;
    if (typeof detail?.ok === "boolean") cb(detail.ok);
  };
  window.addEventListener(APP_AUTH_EVENT, onEvt as EventListener);
  return () => window.removeEventListener(APP_AUTH_EVENT, onEvt as EventListener);
}