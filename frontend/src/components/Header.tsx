// src/components/Header.tsx
import React, { useEffect, useState } from "react";
import { sfLoginRedirect, sfMe, sfLogout } from "../lib/salesforce";
import Moby from "../assets/Moby.png";

type Props = {
  active: "upload" | "explorer" | "chat";
  onTab: (tab: "upload" | "explorer" | "chat") => void;
};

export default function Header({ active, onTab }: Props) {
  // Guardamos la info de /me principalmente para el instance_url
  const [sfAuth, setSfAuth] = useState<{ authenticated: boolean; instance_url?: string } | null>(null);
  // Bandera de conexión derivada que puede forzarse con el evento "sf-auth"
  const [isConnected, setIsConnected] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    try {
      const me = (await sfMe()) as any;
      setSfAuth(me);
      setIsConnected(!!me?.authenticated);
    } catch {
      setSfAuth({ authenticated: false });
      setIsConnected(false);
    }
  };

  useEffect(() => {
    void refresh();

    // Revalidamos al volver el foco a la pestaña
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);

    // Escuchamos los cambios de auth que emite ExplorerView
    const onSfAuth = (e: Event) => {
      const detail = (e as CustomEvent<{ ok: boolean }>).detail;
      if (typeof detail?.ok === "boolean") {
        setIsConnected(detail.ok);
        // Si volvemos a estar conectados, refrescamos para recuperar instance_url
        if (detail.ok) void refresh();
      }
    };
    window.addEventListener("sf-auth", onSfAuth as EventListener);

    return () => {
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("sf-auth", onSfAuth as EventListener);
    };
  }, []);

  const onLogin = () => {
    // Fuerza que el query param refleje la pestaña actual
    const url = new URL(window.location.href);
    url.searchParams.set("tab", active); // "upload" o "explorer"
    const next = url.pathname + url.search; // p.ej. "/?tab=explorer"
    sfLoginRedirect(next || "/");
  };

  const onLogout = async () => {
    setBusy(true);
    try {
      await sfLogout();
    } finally {
      await refresh();
      setBusy(false);
    }
  };

  // Texto de estado
  const connectedHost =
    sfAuth?.instance_url?.replace?.(/^https?:\/\//, "") || "salesforce.com";

  return (
    <header className="sticky top-0 z-40 border-b shadow-sm bg-gradient-to-r from-[#003f7d] to-[#00a0a3] text-white">
      <div className="w-full max-w-[90rem] mx-auto px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="font-extrabold text-2xl tracking-tight">CTS Dashboard</div>
          <nav className="flex items-center gap-1 rounded-full bg-white/10 p-1">
            <button
              data-testid="tab-upload"
              className={`px-3 py-1.5 rounded-full text-sm transition ${
                active === "upload" ? "bg-white text-[#003f7d] shadow" : "hover:bg-white/20"
              }`}
              onClick={() => onTab("upload")}
            >
              Upload & Link
            </button>
            <button
              data-testid="tab-explorer"
              className={`px-3 py-1.5 rounded-full text-sm transition ${
                active === "explorer" ? "bg-white text-[#003f7d] shadow" : "hover:bg-white/20"
              }`}
              onClick={() => onTab("explorer")}
            >
              Explorer (Map + Filters)
            </button>
            {/* Moby */}
            <button
              data-testid="tab-chat"
              className={`px-3 py-1.5 rounded-full text-sm transition ${
                active === "chat" ? "bg-white text-[#003f7d] shadow" : "hover:bg-white/20"
              }`}
              onClick={() => onTab("chat")}
              title="Open chat Moby"
            >
              Moby (Chat){" "}
              <img
                src={Moby}
                alt="Moby the cat"
                width={20}
                height={20}
                className="inline-block align-middle ml-1"
              />
            </button>
          </nav>
        </div>

        <div className="flex items-center gap-3">
          {isConnected ? (
            <>
              <span className="text-sm opacity-90 hidden md:inline">
                Connected to <strong>{connectedHost}</strong>
              </span>
              <button
                data-testid="btn-logout"
                onClick={onLogout}
                disabled={busy}
                className="px-3 py-1.5 rounded-md bg-white/95 text-[#003f7d] hover:bg-white disabled:opacity-60"
              >
                Logout
              </button>
            </>
          ) : (
            <>
              <span className="text-sm opacity-90 hidden md:inline">Not connected</span>
              <button
                data-testid="btn-login"
                onClick={onLogin}
                className="px-3 py-1.5 rounded-md bg-white/95 text-[#003f7d] hover:bg-white shadow"
                title="Login a Salesforce"
              >
                Login Salesforce
              </button>
            </>
          )}
        </div>
      </div>
    </header>
  );
}