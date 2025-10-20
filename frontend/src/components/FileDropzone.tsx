// src/components/FileDropzone.tsx
import React, { useRef, useState } from "react";

export default function FileDropzone({ onFiles, accept = ".xlsx,.xls" }: { onFiles: (files: File[]) => void; accept?: string; }) {
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const files = Array.from(e.dataTransfer.files || []).filter((f) =>
      accept.split(",").some((ext) => f.name.toLowerCase().endsWith(ext.trim()))
    );
    if (files.length) onFiles(files);
  };

  return (
    <div
      onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
      onDragLeave={() => setDragOver(false)}
      onDrop={onDrop}
      className={`border-2 border-dashed rounded-md p-6 text-center cursor-pointer ${dragOver ? "bg-gray-100" : "bg-white"}`}
      onClick={() => inputRef.current?.click()}
    >
      <p><strong>Arrastra y suelta</strong> Excel aquí o haz click para seleccionar.</p>
      <p className="text-sm text-gray-500 mt-1">Formatos: .xlsx, .xls</p>
      <input
        ref={inputRef}
        type="file"
        className="hidden"
        accept={accept}
        multiple
        onChange={(e) => {
          const files = Array.from(e.target.files || []);
          if (files.length) onFiles(files);
          e.currentTarget.value = "";
        }}
      />
    </div>
  );
}