"use client";

import { useState, useRef, useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { useWorkspaces } from "@/lib/hooks/use-workspaces";
import { CreateNoosphereModal } from "./create-noosphere-modal";
import { switchNoosphere } from "@/lib/navigation";
import { downloadGalaxyExport, getExportInfo } from "@/lib/api";

const MAGOS_ID = process.env.NEXT_PUBLIC_MAGOS_WORKSPACE_ID;

export function NoosphereSwitcher({ currentId }: { currentId: string }) {
  const [open, setOpen] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportNote, setExportNote] = useState<string | null>(null);
  const { session } = useAuth();
  const { workspaces } = useWorkspaces();
  const router = useRouter();
  const pathname = usePathname();
  const dropdownRef = useRef<HTMLDivElement>(null);

  const currentName =
    workspaces.find((w) => w.id === currentId)?.name ??
    (currentId === MAGOS_ID ? "Magos Noo's Noosphere" : "Noosphere");

  const isAdmin = session?.role === "admin";

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  // Export the current noosphere's galaxy as a self-contained static site. A whole
  // large graph makes a page nobody can load, so ask first and cap rather than
  // handing back something unusable.
  async function handleExport() {
    setExporting(true);
    setExportNote(null);
    try {
      const info = await getExportInfo();
      if (!info.viz_assets_available) throw new Error("viz assets unavailable on the server");
      await downloadGalaxyExport(info.exportable ? undefined : { maxEntities: info.limit });
      setExportNote(
        info.exportable
          ? `Exported ${info.entities} entities`
          : `Capped to ${info.limit} of ${info.entities} entities so the page stays usable`,
      );
    } catch (e) {
      setExportNote(e instanceof Error ? e.message : "Export failed");
    } finally {
      setExporting(false);
    }
  }

  function handleSwitch(id: string) {
    setOpen(false);
    if (id === currentId) return;
    switchNoosphere(pathname, currentId, id, router);
  }

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 px-3 py-1 rounded border border-border/30 text-muted-foreground hover:text-foreground hover:border-border/50 text-xs transition-colors"
      >
        <span className="max-w-[180px] truncate">{currentName}</span>
        <span className="text-muted-foreground/70">▾</span>
      </button>

      {open && (
        <div className="absolute top-full left-0 mt-1 w-64 rounded border border-border/50 bg-card shadow-xl z-50 py-1">
          {/* Magos Noosphere */}
          {MAGOS_ID && (
            <>
              <button
                onClick={() => handleSwitch(MAGOS_ID)}
                className="w-full flex items-center justify-between px-3 py-2 text-xs text-amber-400/70 hover:bg-accent/30 transition-colors"
              >
                <span className="flex items-center gap-2">
                  <span>✦</span>
                  <span>Magos Noo&apos;s Noosphere</span>
                </span>
                <span className="text-muted-foreground/30 text-[10px]">demo</span>
              </button>
              <div className="border-t border-border/30 my-1" />
            </>
          )}

          {/* User workspaces */}
          {workspaces.map((ws) => (
            <button
              key={ws.id}
              onClick={() => handleSwitch(ws.id)}
              className="w-full flex items-center justify-between px-3 py-2 text-xs text-muted-foreground hover:bg-accent/30 hover:text-foreground transition-colors"
            >
              <span className="truncate">{ws.name}</span>
              {ws.id === currentId && (
                <span className="text-emerald-400 text-[10px]">✓</span>
              )}
            </button>
          ))}

          {/* Export the galaxy as a static site */}
          <div className="border-t border-border/30 my-1" />
          <button
            onClick={handleExport}
            disabled={exporting}
            className="w-full flex items-center gap-2 px-3 py-2 text-xs text-muted-foreground hover:bg-accent/30 hover:text-foreground transition-colors disabled:opacity-50"
          >
            <span>⤓</span>
            <span>{exporting ? "Building export…" : "Export as HTML"}</span>
          </button>
          {exportNote && (
            <div className="px-3 pb-2 text-[10px] leading-snug text-muted-foreground/70">
              {exportNote}
            </div>
          )}

          {/* Create new (admin only) */}
          {isAdmin && (
            <>
              <div className="border-t border-border/30 my-1" />
              <button
                onClick={() => {
                  setOpen(false);
                  setShowCreate(true);
                }}
                className="w-full flex items-center gap-2 px-3 py-2 text-xs text-accent-foreground/60 hover:text-accent-foreground hover:bg-accent/30 transition-colors"
              >
                <span>+</span>
                <span>New Noosphere</span>
              </button>
            </>
          )}
        </div>
      )}

      {showCreate && (
        <CreateNoosphereModal
          onClose={() => setShowCreate(false)}
          onCreated={(id) => {
            setShowCreate(false);
            switchNoosphere(pathname, currentId, id, router);
          }}
        />
      )}
    </div>
  );
}
