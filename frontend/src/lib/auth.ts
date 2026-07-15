// src/lib/auth.ts
export interface AuthMe {
  authenticated: boolean;
  email?: string;
  name?: string;
}

export async function authMe(): Promise<AuthMe> {
  try {
    const res = await fetch("/api/auth/me", { credentials: "include" });
    if (!res.ok) return { authenticated: false };
    return (await res.json()) as AuthMe;
  } catch {
    return { authenticated: false };
  }
}

export function loginRedirect(next: string = window.location.pathname): void {
  window.location.href = `/api/auth/login?next=${encodeURIComponent(next)}`;
}

export async function logout(): Promise<void> {
  try {
    await fetch("/api/auth/logout", { credentials: "include" });
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
    await fetch("/api/auth/logout", { credentials: "include" });
  } catch {
    // best-effort — the overlay + gate still force re-auth on the next call
  }
}
