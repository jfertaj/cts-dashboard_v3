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
