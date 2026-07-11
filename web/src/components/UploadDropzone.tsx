"use client";

import { useCallback, useRef, useState } from "react";

export function UploadDropzone({
  onFileSelected,
  disabled,
}: {
  onFileSelected: (file: File) => void;
  disabled?: boolean;
}) {
  const [dragActive, setDragActive] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const handleFiles = useCallback(
    (files: FileList | null) => {
      const file = files?.[0];
      if (!file) return;
      if (file.type !== "application/pdf") return;
      onFileSelected(file);
    },
    [onFileSelected],
  );

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragActive(true);
      }}
      onDragLeave={() => setDragActive(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragActive(false);
        handleFiles(e.dataTransfer.files);
      }}
      onClick={() => inputRef.current?.click()}
      role="button"
      tabIndex={0}
      aria-disabled={disabled}
      className={`flex min-h-[200px] cursor-pointer flex-col items-center justify-center gap-2 rounded-2xl border-2 border-dashed p-8 text-center transition ${
        dragActive ? "border-brand-500 bg-brand-50" : "border-gray-300"
      } ${disabled ? "pointer-events-none opacity-50" : ""}`}
    >
      <span className="text-4xl">📄</span>
      <p className="font-medium text-gray-700">
        Drag & drop an invoice PDF here
      </p>
      <p className="text-sm text-gray-400">
        or tap to browse (Amazon, Swiggy, Zomato…)
      </p>
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        className="hidden"
        onChange={(e) => handleFiles(e.target.files)}
      />
    </div>
  );
}
