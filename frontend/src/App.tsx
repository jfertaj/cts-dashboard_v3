import React, { useEffect, useState } from "react";
import Header from "./components/Header";
import LinkAuthView from "./pages/UploadLinkView";
import ChatView from "./pages/ChatView";
import ExplorerView from "./pages/ExplorerView";
import { sfLoginRedirect } from "./lib/salesforce";
import { useSalesforceAuth } from "./hooks/useSalesforceAuth";
import { useIdleTimer } from "./hooks/useIdleTimer";
import { Tab } from "./types";



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

  // Custom hooks for Auth & Idle
  const { authed, sessionExpired, setSessionExpired } = useSalesforceAuth();
  useIdleTimer(sessionExpired, setSessionExpired);



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
      {sessionExpired && (
        <div className="fixed inset-0 z-[2000]">
          <div className="fixed inset-0 backdrop-blur-sm bg-black/20" />
          <div className="fixed inset-0 flex items-center justify-center p-4">
            <div className="max-w-md w-full rounded-xl bg-white shadow-2xl border p-5 text-center">
              <h2 className="text-lg font-semibold text-gray-900">Session Expired</h2>
              <p className="mt-2 text-sm text-gray-700">
                Your Salesforce session has expired due to inactivity or disconnection. Please log in again to continue.
              </p>
              <div className="mt-4 flex items-center justify-center gap-2">
                <button
                  className="rounded-md bg-[#0072CE] text-white px-4 py-2 text-sm font-medium hover:opacity-90"
                  onClick={() => sfLoginRedirect()}
                >
                  Log In Again
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Main content (dimmed visually by overlay above) */}
      <Header active={tab} onTab={goTab} />
      <main data-testid="app-main" className="w-full max-w-[90rem] mx-auto px-6 py-6 space-y-6" aria-hidden={sessionExpired}>
        {authed === false && (
          <div className="p-3 rounded bg-amber-50 border border-amber-200 text-sm">
            You are not connected to Salesforce. Some views may show limited data.
          </div>
        )}
        {tab === "upload" && <LinkAuthView />}
        {tab === "explorer" && <ExplorerView />}
        {tab === "chat" && <ChatView />}
      </main>
      <footer className="mt-10 py-6 text-center text-xs text-slate-500" aria-hidden={sessionExpired}>
        © {new Date().getFullYear()} INNODIA — Clinical Trial Support
      </footer>
    </div>
  );
}