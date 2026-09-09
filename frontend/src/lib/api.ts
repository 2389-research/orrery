// Module-level workspace ID — set by auth context, read by fetchAPI
let _currentWorkspaceId: string | null = null;

export function setApiWorkspaceId(id: string | null) {
  _currentWorkspaceId = id;
}

function buildHeaders(options?: RequestInit): Record<string, string> {
  const headers: Record<string, string> = {
    ...(options?.headers as Record<string, string> || {}),
  };
  if (_currentWorkspaceId) {
    headers["X-Workspace-Id"] = _currentWorkspaceId;
  }
  return headers;
}

// ── Static HTML export ──────────────────────────────────────────────────────────
// The galaxy viz is already a static Canvas app whose only backend call is GET
// /graph, so a noosphere can be exported as a self-contained site. The download
// goes through fetch() rather than a plain <a href> because workspace scope
// travels as an X-Workspace-Id HEADER, which a link cannot set.

export interface ExportInfo {
  entities: number;
  collections: number;
  domains: number;
  routes: number;
  limit: number;
  exportable: boolean;
  viz_assets_available: boolean;
}

export async function getExportInfo(): Promise<ExportInfo> {
  return fetchAPI<ExportInfo>("/export/info");
}

/** Download this noosphere's galaxy as a zip that runs with no services behind it.
 *  `maxEntities` caps the render set (entities cost framerate); `minRouteWeight`
 *  prunes domain trade routes (they are most of the bytes but cost no framerate). */
export async function downloadGalaxyExport(opts?: {
  maxEntities?: number;
  minRouteWeight?: number;
}): Promise<void> {
  const q = new URLSearchParams();
  if (opts?.maxEntities) q.set("max_entities", String(opts.maxEntities));
  if (opts?.minRouteWeight) q.set("min_route_weight", String(opts.minRouteWeight));
  const qs = q.toString();
  const res = await fetch(`/api/export/html${qs ? `?${qs}` : ""}`, {
    headers: buildHeaders(),
  });
  if (!res.ok) {
    let msg = `Export failed (${res.status})`;
    try {
      const body = await res.json();
      const d = body?.detail;
      if (d?.error) msg = `${d.error}: ${d.entities} entities (limit ${d.limit})`;
      else if (typeof d === "string") msg = d;
    } catch {
      /* non-JSON error body — keep the status message */
    }
    throw new Error(msg);
  }
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = "orrery-galaxy-export.zip";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function fetchAPI<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, { ...options, headers: buildHeaders(options) });
  if (!res.ok) throw new Error(`API error: ${res.status} ${await res.text()}`);
  return res.json();
}

async function fetchAPIText(path: string, options?: RequestInit): Promise<string> {
  const res = await fetch(`/api${path}`, { ...options, headers: buildHeaders(options) });
  if (!res.ok) throw new Error(`API error: ${res.status} ${await res.text()}`);
  return res.text();
}

export const api = {
  getStats: () => fetchAPI<import("./types").Stats>("/stats"),
  getDocuments: () => fetchAPI<import("./types").DocumentSummary[]>("/documents"),
  getDocument: (id: string) => fetchAPI<import("./types").DocumentDetail>(`/documents/${id}`),
  getDocumentFile: (id: string) => fetchAPIText(`/documents/${id}/file`),
  deleteDocument: (id: string) => fetchAPI<{ deleted: boolean; entities_removed: string[] }>(`/documents/${id}`, { method: "DELETE" }),
  getDomains: () => fetchAPI<import("./types").DomainInfo[]>("/domains"),
  getEntities: (params?: { type?: string; domain?: string; job_id?: string; limit?: number }) => {
    const query = new URLSearchParams();
    if (params?.type) query.set("type", params.type);
    if (params?.domain) query.set("domain", params.domain);
    if (params?.job_id) query.set("job_id", params.job_id);
    if (params?.limit) query.set("limit", String(params.limit));
    return fetchAPI<(import("./types").EntitySummary & { is_new?: boolean })[]>(`/entities?${query}`);
  },
  getJobs: () => fetchAPI<(import("./types").JobInfo & { results?: import("./types").BatchResults })[]>("/jobs"),
  getJob: (jobId: string) =>
    fetchAPI<import("./types").JobInfo & { results?: import("./types").BatchResults }>(`/jobs/${jobId}`),
  ingestFile: async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return fetchAPI<import("./types").IngestResult>("/ingest", { method: "POST", body: form });
  },
  ingestDirectory: (path: string) =>
    fetchAPI<{ documents: import("./types").IngestResult[]; total: number }>("/ingest/directory", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }),
  getJobIterations: (jobId: string) => fetchAPI<import("./types").SimmerJobDetail>(`/jobs/${jobId}/iterations`),
  triggerGeneralSimmer: () => fetchAPI<{ job_id: string }>("/simmer/general", { method: "POST" }),
  triggerDomainSimmer: (domain: string) => fetchAPI<{ job_id: string }>(`/simmer/${domain}`, { method: "POST" }),
  triggerDomainImageSimmer: (domain: string) => fetchAPI<{ job_id: string }>(`/simmer/${domain}/image`, { method: "POST" }),
  triggerNormalization: () =>
    fetchAPI<{
      plural_merges: number;
      embedding_merges: number;
      queued_for_review: number;
      total_entities_before: number;
      total_entities_after: number;
    }>("/normalize", { method: "POST" }),
  getNormalizationSummary: () =>
    fetchAPI<{
      merges_by_method: Record<string, number>;
      total_merges: number;
      pending_reviews: number;
      recent_merges: { from: string; to: string; method: string; similarity: number; date: string }[];
    }>("/normalize/summary"),
  getReviewQueue: () =>
    fetchAPI<{ id: string; entity_a: string; entity_b: string; similarity: number }[]>("/normalize/review"),
  resolveReview: (reviewId: string, action: "merge" | "keep_separate") =>
    fetchAPI<{ status: string }>(`/normalize/review/${reviewId}?action=${action}`, { method: "POST" }),
  getCorrections: () =>
    fetchAPI<import("./types").Correction[]>("/corrections"),
  triggerJudgeCorrections: () =>
    fetchAPI<{ job_id: string; status: string }>("/corrections/judge", { method: "POST" }),
  resolveCorrection: (id: string, action: "approve" | "reject") =>
    fetchAPI<{ status: string; applied?: boolean }>(`/corrections/review/${id}?action=${action}`, { method: "POST" }),
  getEntity: (entityId: string) =>
    fetchAPI<{
      id: string;
      canonical_name: string;
      type: string;
      sources: { document_id: string; chunk_id: string; extraction_pass: string; spec_version: number | null; job_id: string | null; title: string | null; content_type: string }[];
      merge_history: string[];
    }>(`/entities/${entityId}`),
  getEntityCooccurrences: (entityId: string) =>
    fetchAPI<{ id: string; canonical_name: string; type: string; weight: number }[]>(
      `/entities/${entityId}/cooccurrences`
    ),
  getCollectionSummary: (collectionId: string) =>
    fetchAPI<import("./types").CollectionSummaryResponse>(
      `/collections/${encodeURIComponent(collectionId)}/summary`),
  /** Magos Lex commentary for a node, or null when none has been generated.
   *
   *  Returns null rather than throwing on 404: commentary is optional decoration and
   *  a missing entry is the normal case, not an error. Takes an AbortSignal so the
   *  caller can cancel a request superseded by the next focus.
   */
  getCommentary: async (nodeType: string, id: string, opts?: { signal?: AbortSignal }) => {
    const res = await fetch(
      `/api/commentary/${encodeURIComponent(nodeType)}/${encodeURIComponent(id)}`,
      { headers: buildHeaders(), signal: opts?.signal },
    );
    if (!res.ok) return null;
    return res.json() as Promise<{ comments: import("./types").MagosComment[] }>;
  },
  getDocumentReader: (docId: string) =>
    fetchAPI<{
      document: { id: string; title: string; status: string; domains: string[] };
      entities: {
        id: string;
        canonical_name: string;
        type: string;
        source_count: number;
        mention_count: number;
        positions: number[];
        snippets: string[];
        merge_history: string[];
        is_new: boolean;
      }[];
      segments: {
        type: string;
        text: string;
        entity_id?: string;
        entity_name?: string;
        entity_type?: string;
        is_new?: boolean;
      }[];
      total_mentions: number;
    }>(`/documents/${docId}/reader`),

  // Workspace CRUD
  listWorkspaces: () =>
    fetchAPI<{ id: string; name: string; description?: string; status?: string }[]>("/workspaces"),
  createWorkspace: (name: string, description: string = "") =>
    fetchAPI<{ workspaceId: string; name: string }>("/workspaces", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, description }),
    }),
  updateWorkspace: (id: string, name: string) =>
    fetchAPI<{ updated: boolean }>(`/workspaces/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  archiveWorkspace: (id: string) =>
    fetchAPI<{ archived: boolean }>(`/workspaces/${id}`, { method: "DELETE" }),

  // Invites
  getInvites: () =>
    fetchAPI<{ id: string; email: string; role: string; createdAt: string; status: string }[]>("/invites"),
  createInvite: (email: string, role: string) =>
    fetchAPI<{ inviteId: string; email: string; role: string }>("/invites", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, role }),
    }),
  revokeInvite: (id: string) =>
    fetchAPI<{ revoked: boolean }>(`/invites/${id}`, { method: "DELETE" }),
};
