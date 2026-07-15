// src/lib/auth.ts

// Same API base the rest of the client uses (api.ts / salesforce.ts): when the
// frontend is not same-origin with the backend (e.g. Vite dev with VITE_API_BASE
// pointing at :8000), auth calls and redirects must target the backend origin, not
// the frontend's — otherwise /api/auth/me returns static content and the gate
// treats the user as signed out. Empty string = same-origin (prod behind the ALB).
const API_BASE =
  ((import.meta as any).env?.VITE_API_BASE as string | undefined)?.replace(/\/+$/, "") || "";

export interface AuthMe {
  authenticated: boolean;
  email?: string;
  name?: string;
}

export async function authMe(): Promise<AuthMe> {
  try {
    const res = await fetch(`${API_BASE}/api/auth/me`, { credentials: "include" });
    if (!res.ok) return { authenticated: false };
    return (await res.json()) as AuthMe;
  } catch {
    return { authenticated: false };
  }
}

export function loginRedirect(
  next: string = window.location.pathname + window.location.search,
): void {
  window.location.href = `${API_BASE}/api/auth/login?next=${encodeURIComponent(next)}`;
}

export async function logout(): Promise<void> {
  try {
    await fetch(`${API_BASE}/api/auth/logout`, { credentials: "include" });
  } finally {
    window.location.href = "/";
  }
}

/**
 * Destroy the server-side app session without navigating away. Used by the idle
 * timer: it expires `cts_session` on the backend (so a refresh cannot restore
 * access) while the caller shows the "session expired" overlay in place.
 */
export async function endSession(): Promise<void> {
  try {
    await fetch(`${API_BASE}/api/auth/logout`, { credentials: "include" });
  } catch {
    // best-effort — the overlay + gate still force re-auth on the next call
  }
}
