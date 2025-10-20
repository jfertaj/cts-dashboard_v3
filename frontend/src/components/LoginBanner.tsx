import React from "react";
import { sfLoginRedirect } from "../lib/salesforce";

export default function LoginBanner() {
  return (
    <div className="rounded-2xl border bg-white p-6 shadow-sm ring-1 ring-black/5">
      <div className="flex items-start md:items-center gap-4 md:gap-6 md:flex-row flex-col">
        <div className="shrink-0 rounded-full bg-[#003f7d] text-white w-12 h-12 grid place-items-center font-bold">
          SF
        </div>
        <div className="flex-1">
          <h3 className="font-semibold text-lg text-[#0f172a]">Connect with Salesforce</h3>
          <p className="text-sm text-slate-600 mt-1">
            Inicia sesión para buscar cuentas, vincular sitios y explorar datos de profiling / qualification.
          </p>
        </div>
        <button
          onClick={() => sfLoginRedirect()}
          className="px-4 py-2 rounded-md bg-[#00a0a3] text-white hover:brightness-110"
        >
          Login Salesforce
        </button>
      </div>
    </div>
  );
}