import React, { useEffect, useState } from "react";
import Header from "./components/Header";
import UploadLinkView from "./pages/UploadLinkView";
import ChatView from "./pages/ChatView";
import ExplorerView from "./pages/ExplorerView";
import { sfMe } from "./lib/salesforce";

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

  // Lee auth al cargar
  useEffect(() => {
    (async () => {
      const me = await sfMe();
      setAuthed(!!me.authenticated);
    })();
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
    <div className="min-h-screen bg-[#f6f9fb] text-[#0f172a]">
      <Header active={tab} onTab={goTab} />
      <main className="w-full max-w-[90rem] mx-auto px-6 py-6 space-y-6">
        {authed === false && (
          <div className="p-3 rounded bg-amber-50 border border-amber-200 text-sm">
            You are not logged in in Salesfoce. Some views can missing data.
          </div>
        )}
        {tab === "upload" && <UploadLinkView />}
        {tab === "explorer" && <ExplorerView />}
        {tab === "chat" && <ChatView />}
      </main>
      <footer className="mt-10 py-6 text-center text-xs text-slate-500">
        © {new Date().getFullYear()} INNODIA — Clinical Trial Support
      </footer>
    </div>
  );
}