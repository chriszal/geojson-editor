"use client";

import dynamic from "next/dynamic";

const MapEditor = dynamic(() => import("../components/MapEditor"), {
  ssr: false,
  loading: () => <div style={{ padding: 16 }}>Loading map…</div>,
});

export default function Page() {
  return <div className="h-screen w-screen"><MapEditor /></div>;
}
