import { useState, useEffect } from "react";
import { sfMe } from "../lib/salesforce";

export function useSalesforceAuth() {
    const [authed, setAuthed] = useState<boolean | null>(null); // null -> unknown
    const [sessionExpired, setSessionExpired] = useState(false);

    // Initial check
    useEffect(() => {
        (async () => {
            try {
                const me = await sfMe();
                setAuthed(!!me.authenticated);
                if (!me.authenticated) setSessionExpired(true);
            } catch {
                // If initial check fails, we might be offline or erroring, assume not authed or handle gracefully
            }
        })();
    }, []);

    // Periodic polling
    useEffect(() => {
        const poll = window.setInterval(async () => {
            try {
                const me = await sfMe();
                setAuthed(!!me.authenticated);
                if (!me.authenticated) setSessionExpired(true);
            } catch {
                // failed fetch, keep previous state
            }
        }, 5 * 60 * 1000); // 5 minutes

        const onSfAuth = (e: Event) => {
            const ok = (e as CustomEvent<{ ok: boolean }>).detail?.ok;
            if (ok === false) setSessionExpired(true);
        };
        window.addEventListener("sf-auth", onSfAuth as EventListener);

        return () => {
            window.clearInterval(poll);
            window.removeEventListener("sf-auth", onSfAuth as EventListener);
        };
    }, []);

    return { authed, sessionExpired, setSessionExpired };
}
