"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { IdentityGate } from "@/components/IdentityGate";
import { UploadDropzone } from "@/components/UploadDropzone";
import { api, formatApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { rememberExpense } from "@/lib/local-store";

function UploadContent() {
  const { user } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const groupId = searchParams.get("groupId");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleFile(file: File) {
    if (!user) return;
    setUploading(true);
    setError(null);
    try {
      const expense = await api.uploadExpensePdf({
        file,
        filename: file.name,
        paidBy: user.id,
        groupId,
      });
      rememberExpense(user.id, { id: expense.id, groupId: expense.group_id ?? null });
      router.push(`/expenses/${expense.id}`);
    } catch (err) {
      setError(
        formatApiError(
          err,
          "Upload failed. The backend upload endpoint may not be available yet — try Manual entry instead.",
        ),
      );
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-bold">Upload invoice</h1>
      <UploadDropzone onFileSelected={handleFile} disabled={uploading} />
      {uploading && (
        <p className="text-center text-sm text-gray-500">Uploading…</p>
      )}
      {error && (
        <div className="rounded-lg bg-red-50 p-3 text-sm text-red-700">
          {error}
        </div>
      )}
      <button
        type="button"
        onClick={() =>
          router.push(`/expenses/manual${groupId ? `?groupId=${groupId}` : ""}`)
        }
        className="text-center text-sm font-medium text-brand-700 underline"
      >
        Prefer to enter it manually?
      </button>
    </div>
  );
}

export default function UploadPage() {
  return (
    <IdentityGate>
      <Suspense fallback={null}>
        <UploadContent />
      </Suspense>
    </IdentityGate>
  );
}
