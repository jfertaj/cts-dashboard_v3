import { useEffect, useState } from "react";
import { authMe } from "../lib/auth";
import { listenAuthChange } from "../lib/events";

export function useAuth() {
  const [authed, setAuthed] = useState<boolean | null>(null); // null -> unknown
  const [sessionExpired, setSessionExpired] = useState(false);

  useEffect(() => {
    let alive = true;
    const check = async () => {
      const me = await authMe();
      if (!alive) return;
      setAuthed(me.authenticated);
      // NB: an unauthenticated check drives the sign-in gate (authed===false),
      // NOT the "Session Expired" overlay. sessionExpired is reserved for a
      // session dying mid-use — set by the `app-auth` 401 event and the idle
      // timer — so the gate (authed===false && !sessionExpired) stays reachable.
    };
    void check();
    const poll = window.setInterval(check, 5 * 60 * 1000); // 5 minutes

    const off = listenAuthChange((ok) => {
      if (!ok) setSessionExpired(true);
    });

    // Compatibility: some Explorer/legacy flows still emit the old "sf-auth"
    // event on a mid-use 401. Honor its expiry signal so the overlay reacts
    // without converting every remaining call site.
    const onLegacy = (e: Event) => {
      if ((e as CustomEvent<{ ok?: boolean }>).detail?.ok === false) setSessionExpired(true);
    };
    window.addEventListener("sf-auth", onLegacy as EventListener);

    return () => {
      alive = false;
      window.clearInterval(poll);
      off();
      window.removeEventListener("sf-auth", onLegacy as EventListener);
    };
  }, []);

  return { authed, sessionExpired, setSessionExpired };
}
