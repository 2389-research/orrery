"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { GalaxyPanel } from "@/components/galaxy/galaxy-panel";
import { ReaderPane } from "@/components/reader/reader-pane";
import { ImagePane } from "@/components/image-pane";
import { api } from "@/lib/api";
import { getAuthToken } from "@/lib/firebase";
import { useNoosphereId } from "@/lib/hooks/use-noosphere-id";
import { useSearchParams } from "next/navigation";
import { useScreensaverSettings } from "@/lib/screensaver-settings";
import { useSearchBroadcast } from "@/lib/use-search-broadcast";
import { isSameOriginMessage } from "@/lib/viz-message";
import { MagosOverlay } from "@/components/magos-overlay";
import { ScreensaverSettingsPanel } from "@/components/screensaver-settings-panel";

interface SelectedNode {
  nodeType: string;
  data: Record<string, unknown>;
}

interface SearchResult {
  query: string;
  entities: { id: string; name: string; type: string; source_count: number; score: number; paths: string[] }[];
  chunks: { chunk_id: string; document_id: string; document_title: string; text: string; score: number }[];
  images?: { document_id: string; title: string; description: string; score: number }[];
  total_entities: number;
  total_chunks: number;
}

type ViewMode = "galaxy" | "star" | "collection";

interface Breadcrumb {
  label: string;
  action: () => void;
}

// Idle "attract mode" (screensaver) timings. Idle-start and beat now come from
// device-local screensaver settings; the star-dwell stays fixed.
const ATTRACT_STAR_LINGER_MS = 16000;  // dwell time inside an entity's star view

export default function VizPage() {
  const noosphereId = useNoosphereId();
  // Optional params forwarded to the viz iframes:
  //   ?scale=0.66  — render-resolution override (e.g. a 4K TV on a weak GPU)
  //   ?fps=1       — show the FPS meter (can also toggle with the 'f' key)
  // Omitted → iframe uses the device's native devicePixelRatio / meter hidden.
  const searchParams = useSearchParams();
  const scaleParam = searchParams.get("scale");
  const fpsParam = searchParams.get("fps");
  // Device-local screensaver settings (idle/beat/magos/fps/scale). URL params win
  // over the stored scale for kiosk deep-links. FPS is driven live via postMessage.
  const [screensaver, setScreensaver] = useScreensaverSettings();
  const settingsRef = useRef(screensaver);
  useEffect(() => { settingsRef.current = screensaver; }, [screensaver]);
  const effectiveScale = scaleParam ?? (screensaver.scale !== 1 ? String(screensaver.scale) : "");
  const vizSuffix =
    (effectiveScale ? `&scale=${encodeURIComponent(effectiveScale)}` : "") +
    (fpsParam ? `&fps=${encodeURIComponent(fpsParam)}` : "");
  const [viewMode, setViewMode] = useState<ViewMode>("galaxy");
  const [starEntityId, setStarEntityId] = useState<string | null>(null);
  const [starEntityName, setStarEntityName] = useState<string>("");
  const [collectionId, setRepoId] = useState<string | null>(null);
  const [collectionName, setRepoName] = useState<string>("");
  const [selectedNode, setSelectedNode] = useState<SelectedNode | null>(null);
  const [selectedDocId, setSelectedDocId] = useState<string | null>(null);
  const [selectedDocType, setSelectedDocType] = useState<string>("text");
  const [domainColors, setDomainColors] = useState<Record<string, string>>({});
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult | null>(null);
  const [searching, setSearching] = useState(false);
  const [includeImages, setIncludeImages] = useState(false);
  const [hasImages, setHasImages] = useState(false);
  const [fading, setFading] = useState(false);
  const [authToken, setAuthToken] = useState<string>("");
  const galaxyRef = useRef<HTMLIFrameElement>(null);
  const starRef = useRef<HTMLIFrameElement>(null);
  const collectionRef = useRef<HTMLIFrameElement>(null);

  // Get auth token for iframe API calls
  const isNoop = process.env.NEXT_PUBLIC_AUTH_MODE === "noop";
  useEffect(() => {
    if (isNoop) {
      setAuthToken("noop");  // sentinel — iframes render, no auth header sent
      return;
    }
    getAuthToken().then(t => { if (t) setAuthToken(t); }).catch(() => {});
  }, [isNoop]);

  // Check if workspace has images (to show/hide toggle)
  useEffect(() => {
    api.getStats().then(s => { if (s.image_count > 0) setHasImages(true); }).catch(() => {});
  }, []);

  // Current iframe ref
  const activeRef = viewMode === "star" ? starRef : viewMode === "collection" ? collectionRef : galaxyRef;

  // Breadcrumbs — galaxy only.
  //
  // There were `star` and `collection` entries here, but the bar itself is hidden in
  // exactly those two modes (see its `display` below), so they could never render. Each
  // drill-in view draws its OWN `#nav` inside its iframe (star.html / collection.html),
  // which is what actually shows the entity or collection name — so the fix is to drop
  // the dead entries, not to reveal a second, competing breadcrumb.
  const breadcrumbs: Breadcrumb[] = [
    { label: "Galaxy", action: () => exitToGalaxy() },
  ];

  // Enter star view with fade — refresh auth token first
  const enterStarView = useCallback(async (entityId: string, entityName: string) => {
    // Refresh token before loading star iframe (tokens expire after ~1hr)
    try {
      const fresh = await getAuthToken();
      if (fresh) setAuthToken(fresh);
    } catch {}
    setFading(true);
    setTimeout(() => {
      setStarEntityId(entityId);
      setStarEntityName(entityName);
      setViewMode("star");
      setSelectedNode(null);
      setSearchResults(null);
      setSelectedDocId(null);
      setTimeout(() => setFading(false), 50);
    }, 300);
  }, []);

  // Exit star view
  const exitStarView = useCallback(() => {
    setFading(true);
    setTimeout(() => {
      setViewMode("galaxy");
      setStarEntityId(null);
      setStarEntityName("");
      setSelectedNode(null);
      setSelectedDocId(null);
      setTimeout(() => setFading(false), 50);
    }, 300);
  }, []);

  // Enter collection view with fade — refresh auth token first
  const enterRepoView = useCallback(async (id: string, name: string) => {
    try {
      const fresh = await getAuthToken();
      if (fresh) setAuthToken(fresh);
    } catch {}
    setFading(true);
    setTimeout(() => {
      setRepoId(id);
      setRepoName(name);
      setViewMode("collection");
      setSelectedNode(null);
      setSearchResults(null);
      setSelectedDocId(null);
      setTimeout(() => setFading(false), 50);
    }, 300);
  }, []);

  // Exit collection view
  const exitCollectionView = useCallback(() => {
    setFading(true);
    setTimeout(() => {
      setViewMode("galaxy");
      setRepoId(null);
      setRepoName("");
      setSelectedNode(null);
      setSelectedDocId(null);
      setTimeout(() => setFading(false), 50);
    }, 300);
  }, []);

  // Exit whichever drill-in view is active back to the galaxy
  const exitToGalaxy = useCallback(() => {
    if (viewMode === "star") exitStarView();
    else if (viewMode === "collection") exitCollectionView();
  }, [viewMode, exitStarView, exitCollectionView]);

  // Reset to galaxy home
  const resetGalaxy = useCallback(() => {
    const wasDrilledIn = viewMode === "star" || viewMode === "collection";
    if (wasDrilledIn) {
      exitToGalaxy();
    }
    setTimeout(() => {
      galaxyRef.current?.contentWindow?.postMessage({ type: "reset_galaxy" }, "*");
    }, wasDrilledIn ? 400 : 0);
  }, [viewMode, exitToGalaxy]);

  // Listen for postMessage events
  useEffect(() => {
    const handler = (e: MessageEvent) => {
      if (!isSameOriginMessage(e)) return;
      if (e.data?.type === "node_selected") {
        if (e.data.nodeType === "document") {
          setSelectedDocId(e.data.data.id as string);
          setSelectedDocType((e.data.data.content_type as string) || "text");
          setSelectedNode(null);
        } else {
          setSelectedNode({ nodeType: e.data.nodeType, data: e.data.data });
          setSelectedDocId(null);
          setSearchResults(null);
        }
      } else if (e.data?.type === "node_cleared") {
        setSelectedNode(null);
        setSelectedDocId(null);
      } else if (e.data?.type === "enter_star") {
        enterStarView(e.data.entityId, e.data.entityName);
      } else if (e.data?.type === "enter_collection") {
        enterRepoView(e.data.collectionId, e.data.collectionName);
      } else if (e.data?.type === "navigate_galaxy") {
        exitToGalaxy();
      } else if (e.data?.type === "reset_home") {
        exitToGalaxy();
        // After fade back, reset galaxy camera to full overview
        setTimeout(() => {
          galaxyRef.current?.contentWindow?.postMessage({ type: "reset_galaxy" }, "*");
        }, 400);
      }
    };
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [enterStarView, enterRepoView, exitToGalaxy]);

  // ── Idle "attract mode" (screensaver) ──────────────────────────────────────
  // After ATTRACT_IDLE_MS with no real user input, the orrery roams on its own:
  // glides to random domains/entities and selects them, and ~20% of hops dive
  // into an entity's star system, linger, and return. Any real input (mouse /
  // key / touch, from the shell OR either iframe) exits instantly; background
  // API calls don't count. Trigger on demand via a `trigger_sleep` postMessage,
  // a `?sleep=1` URL param, or window.__sleep().
  const attractRef = useRef(false);
  const idleTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const beatTimer = useRef<ReturnType<typeof setInterval> | undefined>(undefined);
  const lingerTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const viewModeRef = useRef(viewMode);
  const exitToGalaxyRef = useRef(exitToGalaxy);
  useEffect(() => { viewModeRef.current = viewMode; }, [viewMode]);
  useEffect(() => { exitToGalaxyRef.current = exitToGalaxy; }, [exitToGalaxy]);

  const stopAttract = useCallback(() => {
    if (!attractRef.current) return;
    attractRef.current = false;
    clearInterval(beatTimer.current);
    clearTimeout(lingerTimer.current);
    galaxyRef.current?.contentWindow?.postMessage({ type: "attract_stop" }, "*");
  }, []);

  const startAttract = useCallback(() => {
    if (attractRef.current) return;
    attractRef.current = true;
    if (viewModeRef.current !== "galaxy") exitToGalaxyRef.current();
    // Let any drill-in exit finish its fade, then begin roaming.
    setTimeout(() => {
      if (!attractRef.current) return;
      const g = galaxyRef.current?.contentWindow;
      g?.postMessage({ type: "attract_start" }, "*");
      g?.postMessage({ type: "attract_hop" }, "*");
    }, 450);
    beatTimer.current = setInterval(() => {
      if (viewModeRef.current === "galaxy") {
        galaxyRef.current?.contentWindow?.postMessage({ type: "attract_hop" }, "*");
      }
    }, Math.max(3, settingsRef.current.beatSeconds) * 1000);
  }, []);

  const bumpIdle = useCallback(() => {
    if (attractRef.current) stopAttract();
    clearTimeout(idleTimer.current);
    idleTimer.current = setTimeout(startAttract, Math.max(1, settingsRef.current.idleMinutes) * 60 * 1000);
  }, [startAttract, stopAttract]);

  useEffect(() => {
    const onActivity = () => bumpIdle();
    const events: (keyof WindowEventMap)[] = ["mousemove", "mousedown", "wheel", "keydown", "touchstart"];
    events.forEach(ev => window.addEventListener(ev, onActivity, { passive: true }));
    const onMsg = (e: MessageEvent) => {
      if (!isSameOriginMessage(e)) return;
      if (e.data?.type === "user_activity") bumpIdle();
      else if (e.data?.type === "trigger_sleep") startAttract();
      else if (e.data?.type === "enter_star" && attractRef.current) {
        // attract dove into a star — dwell, then return to the map and resume.
        clearTimeout(lingerTimer.current);
        lingerTimer.current = setTimeout(() => {
          if (attractRef.current) exitToGalaxyRef.current();
        }, ATTRACT_STAR_LINGER_MS);
      }
    };
    window.addEventListener("message", onMsg);
    (window as unknown as { __sleep?: () => void }).__sleep = startAttract;
    const params = new URLSearchParams(window.location.search);
    if (params.get("sleep") === "1") startAttract();
    else bumpIdle();
    return () => {
      events.forEach(ev => window.removeEventListener(ev, onActivity));
      window.removeEventListener("message", onMsg);
      clearTimeout(idleTimer.current);
      clearInterval(beatTimer.current);
      clearTimeout(lingerTimer.current);
      // Reset so a re-mount (React StrictMode double-invokes effects in dev)
      // restarts cleanly — otherwise startAttract()'s `if (attractRef.current)
      // return` guard leaves the beat interval dead after remount.
      attractRef.current = false;
    };
  }, [bumpIdle, startAttract]);

  // Push the FPS-meter toggle to the galaxy iframe whenever the setting changes.
  useEffect(() => {
    galaxyRef.current?.contentWindow?.postMessage({ type: "set_fps", on: screensaver.fps }, "*");
  }, [screensaver.fps, viewMode]);

  // Real-time search broadcasts → whichever iframe is currently on top. The
  // callback closes over `activeRef`, so it always targets the live view without
  // the subscription needing to know that viewMode exists.
  useSearchBroadcast(({ entities, doc_ids }) => {
    activeRef.current?.contentWindow?.postMessage(
      { type: "search_result", entities, doc_ids },
      "*",
    );
  });

  // Fetch domain colors
  useEffect(() => {
    (async () => {
      try {
        const domains = await api.getDomains();
        const palette = ["#378ADD","#7F77DD","#1D9E75","#BA7517","#D85A30","#5DCAA5","#9c9a92","#4a90d9","#9b59b6","#2ecc71"];
        const colors: Record<string, string> = {};
        domains.forEach((d, i) => { colors[d.path] = palette[i % palette.length]; });
        setDomainColors(colors);
      } catch { /* cosmetic */ }
    })();
  }, []);

  const handleSearch = useCallback(async () => {
    if (!searchQuery.trim()) return;
    setSearching(true);
    setSelectedNode(null);
    setSelectedDocId(null);
    try {
      const token = await getAuthToken();
      const headers: Record<string, string> = {};
      if (token) headers["Authorization"] = `Bearer ${token}`;
      if (noosphereId) headers["X-Workspace-Id"] = noosphereId;

      const resp = await fetch(`/api/search?q=${encodeURIComponent(searchQuery.trim())}&top_k=20&expand=false&include_images=${includeImages}`, { headers });
      const results: SearchResult = await resp.json();
      setSearchResults(results);

      // Fire glow in active viz
      const entityNames = (results.entities || []).slice(0, 10).map(e => e.name);
      const docIds = [...new Set((results.chunks || []).map(c => c.document_id))];
      activeRef.current?.contentWindow?.postMessage({
        type: "search_result",
        entities: entityNames,
        doc_ids: docIds,
      }, "*");
    } catch (e) {
      console.error("Search failed:", e);
    }
    setSearching(false);
  }, [searchQuery, viewMode, includeImages]);

  // Re-search when image toggle changes (if there's an active search)
  const includeImagesRef = useRef(includeImages);
  useEffect(() => {
    if (includeImagesRef.current !== includeImages && searchQuery.trim()) {
      includeImagesRef.current = includeImages;
      handleSearch();
    }
  }, [includeImages, searchQuery, handleSearch]);

  // Click search result → navigate to it
  const flyToEntity = useCallback((entityId: string) => {
    if (viewMode === "star") {
      // In star mode, navigate to that entity's star view
      enterStarView(entityId, "");
    } else {
      galaxyRef.current?.contentWindow?.postMessage({
        type: "fly_to_entity", entityId,
      }, "*");
    }
    setSearchResults(null);
  }, [viewMode, enterStarView]);

  const flyToDomain = useCallback((domainPath: string) => {
    if (viewMode === "star") exitStarView();
    setTimeout(() => {
      galaxyRef.current?.contentWindow?.postMessage({
        type: "fly_to_domain", domainPath,
      }, "*");
    }, viewMode === "star" ? 400 : 0);
    setSearchResults(null);
  }, [viewMode, exitStarView]);

  const showPanel = selectedNode !== null;
  const showDoc = selectedDocId !== null;
  const showSearchResults = searchResults !== null && !showPanel && !showDoc;

  const typeColors: Record<string, string> = {
    Person: "#378ADD", Organization: "#7F77DD", Product: "#1D9E75",
    Technology: "#BA7517", Event: "#D85A30", Concept: "#9c9a92", Location: "#5DCAA5",
  };

  return (
    <div style={{ height: "calc(100vh - 57px)", position: "relative", overflow: "hidden", margin: "-24px" }}>
      {/* Screensaver: Magos Lex commentary overlay + settings gear */}
      <MagosOverlay enabled={screensaver.magos} workspaceId={noosphereId} />
      <ScreensaverSettingsPanel settings={screensaver} update={setScreensaver} />
      {/* Search bar — top center */}
      <div style={{
        position: "absolute", top: 12, left: "50%", transform: "translateX(-50%)",
        zIndex: 20, display: "flex", gap: 6,
      }}>
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSearch()}
          placeholder="Search the knowledge graph"
          style={{
            width: 320, padding: "8px 14px", fontSize: 12,
            fontFamily: "'Courier New', monospace",
            background: "rgba(6,13,34,0.85)", color: "#e8eaf0",
            border: "1px solid rgba(100,180,255,0.2)", borderRadius: 6,
            outline: "none",
          }}
        />
        <button
          onClick={handleSearch}
          disabled={searching}
          style={{
            padding: "8px 14px", fontSize: 11,
            fontFamily: "'Courier New', monospace",
            background: "rgba(100,180,255,0.1)", color: "rgba(100,180,255,0.7)",
            border: "1px solid rgba(100,180,255,0.2)", borderRadius: 6,
            cursor: "pointer",
          }}
        >
          {searching ? "..." : "search"}
        </button>
        {hasImages && (
        <button
          onClick={() => setIncludeImages(prev => !prev)}
          title={includeImages ? "Click to search text only" : "Click to include image results"}
          style={{
            padding: "8px 10px", fontSize: 11,
            fontFamily: "'Courier New', monospace",
            background: includeImages ? "rgba(100,200,180,0.15)" : "rgba(100,180,255,0.05)",
            color: includeImages ? "rgba(100,200,180,0.8)" : "rgba(100,180,255,0.4)",
            border: `1px solid ${includeImages ? "rgba(100,200,180,0.3)" : "rgba(100,180,255,0.15)"}`,
            borderRadius: 6,
            cursor: "pointer",
          }}
        >
          {includeImages ? "📷 on" : "📷 off"}
        </button>
        )}
      </div>

      {/* Breadcrumb + nav — top left, hidden when panel is open */}
      <div style={{
        position: "absolute", top: 12, left: 14, zIndex: 20,
        display: (showPanel || showDoc || showSearchResults || viewMode === "star" || viewMode === "collection") ? "none" : "flex",
        alignItems: "center", gap: 8,
        fontFamily: "'Courier New', monospace",
      }}>
        <button
          onClick={resetGalaxy}
          style={{
            background: "none", border: "none", cursor: "pointer",
            color: "rgba(100,180,255,0.7)", fontSize: 14,
          }}
          title="Reset to galaxy view"
        >⌂</button>
        {breadcrumbs.map((bc, i) => (
          <span key={i} style={{ display: "flex", alignItems: "center", gap: 6 }}>
            {i > 0 && <span style={{ color: "rgba(100,180,255,0.3)", fontSize: 10 }}>›</span>}
            <button
              onClick={bc.action}
              style={{
                background: "none", border: "none", cursor: i < breadcrumbs.length - 1 ? "pointer" : "default",
                color: i < breadcrumbs.length - 1 ? "rgba(100,180,255,0.6)" : "rgba(100,180,255,0.9)",
                fontSize: 10, letterSpacing: "0.05em",
                fontFamily: "'Courier New', monospace",
              }}
            >{bc.label}</button>
          </span>
        ))}
      </div>

      {/* Fade overlay */}
      <div style={{
        position: "absolute", top: 0, left: 0, width: "100%", height: "100%",
        background: "#01040a", zIndex: 15, pointerEvents: "none",
        opacity: fading ? 1 : 0,
        transition: "opacity 0.3s ease",
      }} />

      {/* Galaxy/Sector iframe (always mounted, hidden when in star mode) */}
      {authToken && <iframe
        ref={galaxyRef}
        src={`/viz/index.html?api=${encodeURIComponent("/api")}&token=${encodeURIComponent(authToken)}&workspace=${encodeURIComponent(noosphereId)}${vizSuffix}`}
        onLoad={() => galaxyRef.current?.contentWindow?.postMessage({ type: "set_fps", on: settingsRef.current.fps }, "*")}
        style={{
          width: "100%", height: "100%", border: "none",
          position: "absolute", top: 0, left: 0,
          display: viewMode === "galaxy" ? "block" : "none",
        }}
        title="Galaxy View"
      />}

      {/* Star iframe (mounted when in star mode, only after auth token is ready) */}
      {viewMode === "star" && starEntityId && authToken && (
        <iframe
          ref={starRef}
          src={`/viz/entity-organic.html?entity=${encodeURIComponent(starEntityId)}&api=${encodeURIComponent("/api")}&token=${encodeURIComponent(authToken)}&workspace=${encodeURIComponent(noosphereId)}${vizSuffix}`}
          style={{
            width: "100%", height: "100%", border: "none",
            position: "absolute", top: 0, left: 0,
          }}
          title="Star View"
        />
      )}

      {/* Collection iframe (mounted in collection mode, once the auth token is ready) */}
      {viewMode === "collection" && collectionId && authToken && (
        <iframe
          ref={collectionRef}
          src={`/viz/collection.html?collection=${encodeURIComponent(collectionId)}&api=${encodeURIComponent("/api")}&token=${encodeURIComponent(authToken)}&workspace=${encodeURIComponent(noosphereId)}${vizSuffix}`}
          style={{
            width: "100%", height: "100%", border: "none",
            position: "absolute", top: 0, left: 0,
          }}
          title="Collection View"
        />
      )}

      {/* Left panel: node details / doc reader / search results */}
      {showPanel && (
        <div style={{ position: "absolute", top: 0, left: 0, height: "100%", zIndex: 10 }}>
          <GalaxyPanel
            selectedNode={selectedNode}
            domainColors={domainColors}
            onClose={() => {
              setSelectedNode(null);
              activeRef.current?.contentWindow?.postMessage({ type: "panel_closed" }, "*");
            }}
            onNavigateToEntity={(entityId) => {
              galaxyRef.current?.contentWindow?.postMessage({
                type: "fly_to_entity", entityId,
              }, "*");
            }}
            onNavigateToDomain={(domainPath) => {
              galaxyRef.current?.contentWindow?.postMessage({
                type: "fly_to_domain", domainPath,
              }, "*");
            }}
          />
        </div>
      )}

      {showDoc && (
        <div style={{
          position: "absolute", top: 0, left: 0, height: "100%", zIndex: 10,
          width: "50%", maxWidth: 600, minWidth: 360,
          background: "rgba(6,13,34,0.98)",
          borderRight: "1px solid rgba(100,200,180,0.12)",
          overflowY: "auto",
        }}>
          {selectedDocType === "image" ? (
            <ImagePane
              documentId={selectedDocId!}
              onClose={() => setSelectedDocId(null)}
              onNavigateEntity={(entityId) => {
                setSelectedDocId(null);
                enterStarView(entityId, "");
              }}
              onNavigateDomain={(domainPath) => {
                setSelectedDocId(null);
                flyToDomain(domainPath);
              }}
            />
          ) : (
            <ReaderPane
              documentId={selectedDocId!}
              onClose={() => setSelectedDocId(null)}
              onNavigateEntity={(entityId) => {
                setSelectedDocId(null);
                enterStarView(entityId, "");
              }}
            />
          )}
        </div>
      )}

      {showSearchResults && (
        <div style={{
          position: "absolute", top: 0, left: 0, height: "100%", zIndex: 10,
          width: 340, background: "rgba(6,13,34,0.97)",
          borderRight: "1px solid rgba(100,180,255,0.12)",
          overflowY: "auto", fontFamily: "'Courier New', monospace",
          scrollbarWidth: "thin",
        }}>
          {/* Header */}
          <div style={{ padding: "16px 16px 12px", borderBottom: "1px solid rgba(100,180,255,0.08)" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div style={{ fontSize: 9, color: "rgba(140,200,255,0.6)", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                Search Results
              </div>
              <button
                onClick={() => setSearchResults(null)}
                style={{ background: "none", border: "none", color: "rgba(140,200,255,0.6)", cursor: "pointer", fontSize: 14 }}
              >×</button>
            </div>
            <div style={{ fontSize: 14, color: "#e8eaf0", marginTop: 4 }}>
              &ldquo;{searchResults.query}&rdquo;
            </div>
            <div style={{ fontSize: 10, color: "rgba(140,200,255,0.6)", marginTop: 4 }}>
              {searchResults.total_entities} entities · {searchResults.total_chunks} chunks
            </div>
          </div>

          {/* Image results — shown first when present */}
          {searchResults.images && searchResults.images.length > 0 && (
            <div style={{ padding: "12px 16px", borderBottom: includeImages ? "none" : "1px solid rgba(100,200,180,0.15)" }}>
              <div style={{ fontSize: 9, color: "rgba(100,200,180,0.6)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8 }}>
                Image Results · {searchResults.images.length}
              </div>
              {searchResults.images.slice(0, 10).map((img, i) => (
                <button
                  key={i}
                  onClick={() => {
                    setSelectedDocId(img.document_id);
                    setSelectedDocType("image");
                  }}
                  style={{
                    display: "block", width: "100%", textAlign: "left",
                    padding: 0, marginBottom: 8,
                    background: "none", border: "none", cursor: "pointer",
                    fontFamily: "'Courier New', monospace",
                  }}
                >
                  <div style={{
                    borderRadius: 4, overflow: "hidden",
                    border: "1px solid rgba(100,200,180,0.2)",
                    background: "rgba(100,200,180,0.03)",
                  }}>
                    <img
                      src={`/api/images/${img.document_id}`}
                      alt={img.title}
                      style={{ width: "100%", height: 120, objectFit: "cover", display: "block" }}
                      onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
                    />
                    <div style={{ padding: "6px 10px" }}>
                      <div style={{ fontSize: 10, color: "rgba(100,200,180,0.7)", display: "flex", alignItems: "center", gap: 4 }}>
                        {img.title}
                        <span style={{ marginLeft: "auto", fontSize: 9, color: "rgba(100,200,180,0.4)" }}>{img.score.toFixed(2)}</span>
                      </div>
                    </div>
                  </div>
                </button>
              ))}
            </div>
          )}

          {/* Entity results — hidden when in image-only mode */}
          {!includeImages && (
          <div style={{ padding: "12px 16px", borderBottom: "1px solid rgba(100,180,255,0.08)" }}>
            <div style={{ fontSize: 9, color: "rgba(140,200,255,0.6)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8 }}>
              Entities — click to locate
            </div>
            {searchResults.entities.slice(0, 12).map((e) => (
              <div
                key={e.id}
                onClick={() => flyToEntity(e.id)}
                style={{
                  display: "flex", alignItems: "center", gap: 8, padding: "5px 4px",
                  borderBottom: "1px solid rgba(100,180,255,0.04)",
                  cursor: "pointer", borderRadius: 3,
                  transition: "background 0.1s",
                }}
                onMouseEnter={(ev) => (ev.currentTarget.style.background = "rgba(100,180,255,0.06)")}
                onMouseLeave={(ev) => (ev.currentTarget.style.background = "transparent")}
              >
                <span style={{
                  width: 6, height: 6, borderRadius: "50%",
                  background: typeColors[e.type] || "#9c9a92", flexShrink: 0,
                }} />
                <span style={{ fontSize: 11, color: "#e8eaf0", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {e.name}
                </span>
                <span style={{ fontSize: 9, color: "rgba(140,200,255,0.6)", flexShrink: 0 }}>
                  {e.type}
                </span>
              </div>
            ))}
          </div>
          )}

          {/* Document excerpts — hidden when in image-only mode */}
          {!includeImages && (
          <div style={{ padding: "12px 16px" }}>
            <div style={{ fontSize: 9, color: "rgba(140,200,255,0.6)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8 }}>
              Document Excerpts
            </div>
            {searchResults.chunks
              .filter(c => !c.document_title.match(/\.(jpg|jpeg|png|webp|gif)$/i))
              .slice(0, 5).map((c, i) => (
              <div key={i} style={{
                padding: "8px 10px", marginBottom: 8,
                borderLeft: "2px solid rgba(0,200,180,0.4)",
                background: "rgba(100,180,255,0.03)", borderRadius: "0 3px 3px 0",
              }}>
                <div style={{ fontSize: 10, color: "rgba(140,200,255,0.6)", marginBottom: 4 }}>
                  {c.document_title}
                </div>
                <div style={{ fontSize: 11, color: "rgba(200,215,235,0.85)", lineHeight: 1.5 }}>
                  {c.text}
                </div>
              </div>
            ))}
          </div>
          )}
        </div>
      )}
    </div>
  );
}
