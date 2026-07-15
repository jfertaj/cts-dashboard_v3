import { useEffect } from "react";
import { endSession } from "../lib/auth";

export function useIdleTimer(
    isExpired: boolean,
    setExpired: (v: boolean) => void,
    idleLimitMs: number = 60 * 60 * 1000 // Default 1h
) {
    useEffect(() => {
        let last = Date.now();
        const mark = () => {
            last = Date.now();
        };
        const events = [
            "mousemove",
            "mousedown",
            "keydown",
            "scroll",
            "touchstart",
            "visibilitychange",
            "focus",
        ];
        events.forEach((ev) => window.addEventListener(ev, mark, { passive: true }));

        const t = window.setInterval(async () => {
            if (isExpired) return; // already expired
            const diff = Date.now() - last;
            if (diff >= idleLimitMs) {
                await endSession();
                setExpired(true);
            }
        }, 60 * 1000); // check every minute

        return () => {
            window.clearInterval(t);
            events.forEach((ev) => window.removeEventListener(ev, mark));
        };
    }, [isExpired, idleLimitMs, setExpired]);
}
