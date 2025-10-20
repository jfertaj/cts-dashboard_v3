// src/lib/salesforce.ts

// Base de API (coincide con tu api.ts)
const API_BASE =
  ((import.meta as any).env?.VITE_API_BASE as string | undefined)?.replace(/\/+$/, "") ||
  "";

/** Tipos */
export type SfMeResponse = {
  
  authenticated: boolean;
  instance_url?: string;
  issued_at?: number | string;
  has_refresh?: boolean;
};

/** Wrapper para fetch con cookies (httpOnly) */
async function sfFetch(path: string, init?: RequestInit) {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: {
      ...(init?.headers || {}),
    },
    ...init,
  });
  return res;
}

/** Estado de autenticación Salesforce */
export async function sfMe(): Promise<SfMeResponse> {
  try {
    const res = await sfFetch("/api/salesforce/me");
    if (!res.ok) return { authenticated: false };
    return (await res.json()) as SfMeResponse;
  } catch {
    return { authenticated: false };
  }
}

/** Redirección al login OAuth.
 *  Por defecto vuelve a la ruta actual tras el login.
 */
export function sfLoginRedirect(next?: string) {
  const backTo = next ?? (window.location.pathname + window.location.search);
  const url = `${API_BASE}/api/salesforce/oauth/login?next=${encodeURIComponent(backTo)}`;
  window.location.href = url;
}

/** Cerrar sesión (borra cookie httpOnly en backend) */
export async function sfLogout(): Promise<void> {
  await sfFetch("/api/salesforce/logout", {
    method: "GET",
    credentials: "include",
  });
}

/** Utilidad opcional: asegura que hay sesión; si no, lanza login */
export async function ensureSfSession(next?: string): Promise<boolean> {
  const me = await sfMe();
  if (!me.authenticated) {
    sfLoginRedirect(next);
    return false;
  }
  return true;
}