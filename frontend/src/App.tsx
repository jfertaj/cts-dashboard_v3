import React, { useEffect, useState } from "react";
import Header from "./components/Header";
import LinkAuthView from "./pages/UploadLinkView";
import ChatView from "./pages/ChatView";
import ExplorerView from "./pages/ExplorerView";
import MembersView from "./pages/MembersView";
import AssignmentsView from "./pages/AssignmentsView";
import { loginRedirect } from "./lib/auth";
import { useAuth } from "./hooks/useAuth";
import { useIdleTimer } from "./hooks/useIdleTimer";
import { Tab } from "./types";



function getTabFromURL(): Tab {
  // 1) Permite /explorer y /chat directamente en la URL
  const path = (window.location.pathname || "").toLowerCase();
  if (path === "/explorer") return "explorer";
  if (path === "/members") return "members";
  if (path === "/chat") return "chat";
  if (path === "/assignments") return "assignments";
  // 2) Fallback al query param ?tab=...
  const p = new URLSearchParams(window.location.search);
  const t = (p.get("tab") || "").toLowerCase();
  return t === "explorer" ? "explorer" : t === "members" ? "members" : t === "chat" ? "chat" : t === "assignments" ? "assignments" : "upload";
}

export default function App() {
  const [tab, setTab] = useState<Tab>(getTabFromURL());

  // Custom hooks for Auth & Idle
  const { authed, sessionExpired, setSessionExpired } = useAuth();
  useIdleTimer(sessionExpired, setSessionExpired);

  // Pre-fetch Members + warm Explorer cache as soon as auth is confirmed
  const [membersPrefetch, setMembersPrefetch] = useState<any[] | null>(null);
  useEffect(() => {
    if (authed !== true) return;
    fetch("/api/members/bootstrap", { credentials: "include" })
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (data?.rows?.length) setMembersPrefetch(data.rows); })
      .catch(() => {});
    fetch("/api/salesforce/map/bootstrap", { credentials: "include" }).catch(() => {});
  }, [authed]);



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
    const nextPath = next === "explorer" ? "/explorer" : next === "members" ? "/members" : next === "chat" ? "/chat" : next === "assignments" ? "/assignments" : "/";
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


  // Unauthenticated gate: no active innodia.org session and not merely expired.
  if (authed === false && !sessionExpired) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#f6f9fb]">
        <div className="max-w-md w-full rounded-xl bg-white shadow-2xl border p-6 text-center">
          <h1 className="text-lg font-semibold text-gray-900">CTS Dashboard</h1>
          <p className="mt-2 text-sm text-gray-700">
            Sign in with your innodia.org account to continue.
          </p>
          <button
            data-testid="signin-innodia"
            className="mt-4 rounded-md bg-[#0072CE] text-white px-4 py-2 text-sm font-medium hover:opacity-90"
            onClick={() => loginRedirect()}
          >
            Sign in with innodia.org
          </button>
        </div>
      </div>
    );
  }

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
                Your session has expired due to inactivity. Please sign in again with your innodia.org account to continue.
              </p>
              <div className="mt-4 flex items-center justify-center gap-2">
                <button
                  className="rounded-md bg-[#0072CE] text-white px-4 py-2 text-sm font-medium hover:opacity-90"
                  onClick={() => loginRedirect()}
                >
                  Sign in with innodia.org
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Main content (dimmed visually by overlay above) */}
      <Header active={tab} onTab={goTab} />
      <main data-testid="app-main" className="w-full max-w-[90rem] mx-auto px-6 py-6 space-y-6" aria-hidden={sessionExpired}>
        {tab === "upload" && <LinkAuthView />}
        {tab === "explorer" && <ExplorerView />}
        {tab === "members" && <MembersView prefetchedRows={membersPrefetch} />}
        {tab === "chat" && <ChatView />}
        {tab === "assignments" && <AssignmentsView />}
      </main>
      <footer className="mt-10 py-6 text-center text-xs text-slate-500" aria-hidden={sessionExpired}>
        © {new Date().getFullYear()} INNODIA — Clinical Trial Support
      </footer>
    </div>
  );
}