// components/UploadingOverlay.tsx
import React from "react";

type Props = {
  open: boolean;
  message: string;   // ahora siempre tienes que pasar el texto explícitamente
};

export default function UploadingOverlay({ open, message }: Props) {
  if (!open) return null;
  return (
    <div
      aria-live="polite"
      aria-busy="true"
      className="fixed inset-0 z-[2000] bg-black/60 backdrop-blur-sm flex items-center justify-center"
    >
      <div className="flex flex-col items-center gap-4 rounded-2xl bg-white/95 p-8 shadow-2xl">
        {/* spinner */}
        <svg
          className="h-10 w-10 animate-spin"
          viewBox="0 0 24 24"
          role="img"
          aria-label={message}
        >
          <circle
            cx="12"
            cy="12"
            r="10"
            strokeWidth="4"
            className="opacity-20"
            stroke="currentColor"
            fill="none"
          />
          <path
            d="M22 12a10 10 0 0 0-10-10"
            strokeWidth="4"
            stroke="currentColor"
            fill="none"
          />
        </svg>
        <div className="text-lg font-semibold text-gray-900">{message}</div>
        <p className="text-sm text-gray-500">Please wait…</p>
      </div>
    </div>
  );
}