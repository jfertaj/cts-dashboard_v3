import React, { useEffect, useState } from "react";
import Header from "./components/Header";
import UploadLinkView from "./pages/UploadLinkView";
import ChatView from "./pages/ChatView";
import ExplorerView from "./pages/ExplorerView";
import { sfMe, sfLogout } from "./lib/salesforce";

type Tab = "upload" | "explorer" | "chat";

function getTabFromURL(): Tab {
  // 1) Permite /explorer y /chat directamente en la URL
  const path = (window.location.pathname || "").toLowerCase();
  if (path === "/explorer") return "explorer";
  if (path === "/chat") return "chat";
  // 2) Fallback al query param ?tab=...
  const p = new URLSearchParams(window.location.search);
  const t = (p.get("tab") || "").toLowerCase();
  return t === "explorer" ? "explorer" : t === "chat" ? "chat" : "upload";
}

export default function App() {
  const [tab, setTab] = useState<Tab>(getTabFromURL());
  const [authed, setAuthed] = useState<boolean | null>(null); // null -> unknown
  const [expired, setExpired] = useState(false);

  // Lee auth al cargar
  useEffect(() => {
    (async () => {
      const me = await sfMe();
      setAuthed(!!me.authenticated);
      if (!me.authenticated) setExpired(true);
    })();
  }, []);

  // Auto-logout por inactividad de pestaña
  useEffect(() => {
    const IDLE_LIMIT_MS = 60 * 60 * 1000; // 1h
    let last = Date.now();
    const mark = () => { last = Date.now(); };
    const events = ["mousemove","mousedown","keydown","scroll","touchstart","visibilitychange","focus"]; 
    events.forEach(ev => window.addEventListener(ev, mark, { passive: true }));

    const t = window.setInterval(async () => {
      if (expired) return; // ya expirado
      const diff = Date.now() - last;
      if (diff >= IDLE_LIMIT_MS) {
        try { await sfLogout(); } catch {}
        setExpired(true);
      }
    }, 60 * 1000); // comprueba cada minuto

    return () => {
      window.clearInterval(t);
      events.forEach(ev => window.removeEventListener(ev, mark));
    };
  }, [expired]);

  // Sondeo periódico de sesión SF: si cae, muestra overlay
  useEffect(() => {
    const poll = window.setInterval(async () => {
      try {
        const me = await sfMe();
        setAuthed(!!me.authenticated);
        if (!me.authenticated) setExpired(true);
      } catch {
        // si falla el fetch, no cambiamos estado
      }
    }, 5 * 60 * 1000); // cada 5 minutos

    const onSfAuth = (e: Event) => {
      const ok = (e as CustomEvent<{ ok: boolean }>).detail?.ok;
      if (ok === false) setExpired(true);
    };
    window.addEventListener("sf-auth", onSfAuth as EventListener);

    return () => {
      window.clearInterval(poll);
      window.removeEventListener("sf-auth", onSfAuth as EventListener);
    };
  }, []);

  // Escucha cambios del historial (back/forward)
  useEffect(() => {
    const onPop = () => setTab(getTabFromURL());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // Cambia tab y sincroniza la URL (?tab=…)
  const goTab = (next: Tab) => {
    // Soporta rutas limpias /explorer y /chat para deep-linking
    const base = `${window.location.origin}`;
    const nextPath = next === "explorer" ? "/explorer" : next === "chat" ? "/chat" : "/";
    window.history.pushState({}, "", base + nextPath);
    setTab(next);
  };

  // Marca el body cuando el Explorer está escuchando eventos
  useEffect(() => {
    const b = document.body;
    if (!b) return;
    if (tab === "explorer") {
      b.setAttribute("data-explorer-listening", "1");
    } else {
      b.removeAttribute("data-explorer-listening");
    }
  }, [tab]);


  return (
    <div className="min-h-screen relative bg-[#f6f9fb] text-[#0f172a]">
      {/* Blur overlay when expired */}
      {expired && (
        <div className="absolute inset-0 z-[2000]">
          <div className="absolute inset-0 backdrop-blur-sm bg-black/20" />
          <div className="absolute inset-0 flex items-center justify-center p-4">
            <div className="max-w-md w-full rounded-xl bg-white shadow-2xl border p-5 text-center">
              <h2 className="text-lg font-semibold text-gray-900">Sesión expirada</h2>
              <p className="mt-2 text-sm text-gray-700">
                Tu sesión de Salesforce ha caducado por inactividad o desconexión. Refresca la página para volver a iniciar sesión.
              </p>
              <div className="mt-4 flex items-center justify-center gap-2">
                <button
                  className="rounded-md bg-[#0072CE] text-white px-4 py-2 text-sm font-medium hover:opacity-90"
                  onClick={() => window.location.reload()}
                >
                  Refrescar página
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Main content (dimmed visually by overlay above) */}
      <Header active={tab} onTab={goTab} />
      <main className="w-full max-w-[90rem] mx-auto px-6 py-6 space-y-6" aria-hidden={expired}>
        {authed === false && (
          <div className="p-3 rounded bg-amber-50 border border-amber-200 text-sm">
            No estás conectado a Salesforce. Algunas vistas pueden mostrar menos datos.
          </div>
        )}
        {tab === "upload" && <UploadLinkView />}
        {tab === "explorer" && <ExplorerView />}
        {tab === "chat" && <ChatView />}
      </main>
      <footer className="mt-10 py-6 text-center text-xs text-slate-500" aria-hidden={expired}>
        © {new Date().getFullYear()} INNODIA — Clinical Trial Support
      </footer>
    </div>
  );
}