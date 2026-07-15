// src/components/Header.tsx
import React, { useEffect, useState } from "react";
import { authMe, loginRedirect, logout } from "../lib/auth";
import { listenAuthChange } from "../lib/events";
import Moby from "../assets/Moby.png";

type Props = {
  active: "upload" | "explorer" | "members" | "chat" | "assignments";
  onTab: (tab: "upload" | "explorer" | "members" | "chat" | "assignments") => void;
};

export default function Header({ active, onTab }: Props) {
  // Signed-in user's email (from /api/auth/me), shown next to Logout.
  const [email, setEmail] = useState<string | null>(null);
  // Connection flag, refreshable via the "app-auth" event.
  const [isConnected, setIsConnected] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    try {
      const me = await authMe();
      setIsConnected(me.authenticated);
      setEmail(me.authenticated ? me.email ?? null : null);
    } catch {
      setIsConnected(false);
      setEmail(null);
    }
  };

  useEffect(() => {
    void refresh();

    // Revalidamos al volver el foco a la pestaña
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);

    // Escuchamos los cambios de auth globales (401 → app-auth)
    const off = listenAuthChange((ok) => {
      setIsConnected(ok);
      if (ok) void refresh();
      else setEmail(null);
    });

    return () => {
      window.removeEventListener("focus", onFocus);
      off();
    };
  }, []);

  const onLogin = () => {
    // Fuerza que el query param refleje la pestaña actual
    const url = new URL(window.location.href);
    url.searchParams.set("tab", active); // "upload" o "explorer"
    const next = url.pathname + url.search; // p.ej. "/?tab=explorer"
    loginRedirect(next || "/");
  };

  const onLogout = async () => {
    setBusy(true);
    try {
      await logout();
    } finally {
      setBusy(false);
    }
  };

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
            <button
              data-testid="tab-members"
              className={`px-3 py-1.5 rounded-full text-sm transition ${
                active === "members" ? "bg-white text-[#003f7d] shadow" : "hover:bg-white/20"
              }`}
              onClick={() => onTab("members")}
            >
              Members
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
            <button
              data-testid="tab-assignments"
              className={`px-3 py-1.5 rounded-full text-sm transition ${
                active === "assignments" ? "bg-white text-[#003f7d] shadow" : "hover:bg-white/20"
              }`}
              onClick={() => onTab("assignments")}
            >
              Referral DB
            </button>
          </nav>
        </div>

        <div className="flex items-center gap-3">
          {isConnected ? (
            <>
              <span className="text-sm opacity-90 hidden md:inline">
                Signed in as <strong>{email ?? "innodia.org"}</strong>
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
              <span className="text-sm opacity-90 hidden md:inline">Not signed in</span>
              <button
                data-testid="btn-login"
                onClick={onLogin}
                className="px-3 py-1.5 rounded-md bg-white/95 text-[#003f7d] hover:bg-white shadow"
                title="Sign in with innodia.org"
              >
                Sign in with innodia.org
              </button>
            </>
          )}
        </div>
      </div>
    </header>
  );
}