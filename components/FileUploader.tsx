"use client";
import React, { useRef, useState } from "react";

export default function FileUploader({ onUploaded }: { onUploaded: ()=>void }) {
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const submit = async () => {
    const file = fileRef.current?.files?.[0];
    if (!file) return alert("Choose a .geojson file first");
    const fd = new FormData();
    fd.append("file", file);
    try {
      setBusy(true);
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.error || "Upload failed");
      alert(`Uploaded. Set current.json and version ${data.versionId}`);
      onUploaded();
    } catch (e:any) { alert(e.message); }
    finally { setBusy(false); }
  };

  return (
    <div className="flex items-center gap-2 text-sm">
      <input type="file" accept=".geojson,application/geo+json,application/json" ref={fileRef} />
      <button onClick={submit} disabled={busy} className="px-3 py-1 rounded border bg-slate-900 text-white">Upload & set current</button>
      {busy && <span>Uploading…</span>}
    </div>
  );
}