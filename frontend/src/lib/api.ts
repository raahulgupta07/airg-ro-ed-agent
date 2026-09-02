import { auth } from './stores/auth.svelte';
import { PIPELINES, type PipelineKey } from './pipelineConfig';

const BASE = '/api';

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  // Pre-request: refresh Keycloak token if about to expire
  await auth.ensureValidToken();

  const headers: Record<string, string> = {
    ...(options.headers as Record<string, string> || {}),
  };

  if (auth.token) {
    headers['Authorization'] = `Bearer ${auth.token}`;
  }

  // Only set Content-Type for non-FormData bodies
  if (options.body && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
  }

  const res = await fetch(`${BASE}${path}`, { ...options, headers });

  if (res.status === 401) {
    // Try one refresh before giving up
    if (auth.isKeycloak && auth.refreshTokenValue) {
      const refreshed = await auth.refreshAccessToken();
      if (refreshed) {
        headers['Authorization'] = `Bearer ${auth.token}`;
        const retry = await fetch(`${BASE}${path}`, { ...options, headers });
        if (retry.ok) {
          const text = await retry.text();
          return JSON.parse(text);
        }
      }
    }
    auth.logout();
    throw new Error('Unauthorized');
  }

  if (!res.ok) {
    const err = await res.text().then(t => { try { return JSON.parse(t); } catch { return { detail: res.statusText }; } });
    throw new Error(err.detail || res.statusText);
  }

  // Use res.text() + JSON.parse() instead of res.json()
  // res.json() hangs in some browsers when response body streaming stalls
  const text = await res.text();
  return JSON.parse(text);
}

export const api = {
  // Auth
  login: (username: string, password: string) =>
    request<any>('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),

  me: () => request<any>('/auth/me'),

  getAuthConfig: () => fetch(`${BASE}/auth/config`).then(r => r.text()).then(t => JSON.parse(t)),

  // Jobs
  listJobs: (limit = 50) => request<any[]>(`/jobs/?limit=${limit}`),
  getJob: (jobId: string) => request<any>(`/jobs/${jobId}`),
  deleteJob: (jobId: string) => request<any>(`/jobs/${jobId}`, { method: 'DELETE' }),
  uploadPdf: (file: File) => {
    const form = new FormData();
    form.append('file', file);
    return request<any>('/jobs/upload', { method: 'POST', body: form });
  },

  // Data
  listItems: (jobId?: string) => request<any[]>(`/data/items${jobId ? `?job_id=${jobId}` : ''}`),
  listDeclarations: (jobId?: string) => request<any[]>(`/data/declarations${jobId ? `?job_id=${jobId}` : ''}`),
  search: (query: string, pdfName?: string) =>
    request<any[]>(`/data/search?query=${encodeURIComponent(query)}${pdfName ? `&pdf_name=${encodeURIComponent(pdfName)}` : ''}`),
  searchPdfs: () => request<string[]>('/data/search/pdfs'),
  stats: () => request<any>('/data/stats'),

  // Users
  listUsers: () => request<any[]>('/users/'),
  createUser: (data: any) => request<any>('/users/', { method: 'POST', body: JSON.stringify(data) }),
  updateUser: (id: number, data: any) => request<any>(`/users/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteUser: (id: number) => request<any>(`/users/${id}`, { method: 'DELETE' }),
  activityLogs: (limit = 200) => request<any[]>(`/users/activity-logs?limit=${limit}`),

  // Groups
  listGroups: () => request<any[]>('/groups/'),
  getGroup: (id: number) => request<any>(`/groups/${id}`),
  createGroup: (data: any) => request<any>('/groups/', { method: 'POST', body: JSON.stringify(data) }),
  updateGroup: (id: number, data: any) => request<any>(`/groups/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteGroup: (id: number) => request<any>(`/groups/${id}`, { method: 'DELETE' }),
  assignUserGroup: (userId: number, groupId: number | null) =>
    request<any>(`/groups/assign/${userId}`, { method: 'PUT', body: JSON.stringify({ group_id: groupId }) }),

  // Settings — Keycloak
  getKeycloakSettings: () => request<any>('/settings/keycloak'),
  saveKeycloakSettings: (data: any) => request<any>('/settings/keycloak', { method: 'PUT', body: JSON.stringify(data) }),
  testKeycloakConnection: (data: any) => request<any>('/settings/keycloak/test', { method: 'POST', body: JSON.stringify(data) }),

  // Marked PDF — the original with every extracted value highlighted on it.
  // `markedPdfStatus` is the cheap question ("is there anything to mark, and if
  // not why not"); the URL below is the two-megabyte answer. Asking first is the
  // only way to avoid offering a link that hands back a 404 body inside the PDF
  // viewer — which is what a scanned declaration does, permanently and correctly.
  markedPdfStatus: (jobId: string) =>
    request<{ available: boolean; marks: number; declaration_marks: number;
              item_marks: number; reason: string | null }>(`/jobs/${jobId}/marks`),
  // An <iframe>/<a download> cannot set an Authorization header, so this route
  // takes the token in the query string — same as /pdf and /page-image.
  markedPdfUrl: (jobId: string) =>
    `${BASE}/jobs/${jobId}/annotated-pdf?token=${encodeURIComponent(auth.token || '')}`,
  // The per-job workbook. NOT a URL helper: unlike /pdf and /annotated-pdf, the
  // route is `/download` and it authenticates with a bearer header, so an
  // `<a href>` would arrive unauthenticated. Fetch it and hand the browser a
  // blob — the same thing the history page already does.
  downloadJobExcel: async (jobId: string, filename: string) => {
    const res = await fetch(`${BASE}/jobs/${jobId}/download`,
                            { headers: { Authorization: `Bearer ${auth.token}` } });
    if (!res.ok) throw new Error(`Excel download failed (${res.status})`);
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement('a');
    a.href = url;
    a.download = `${filename.replace(/\.pdf$/i, '')}.xlsx`;
    a.click();
    URL.revokeObjectURL(url);
  },

  // v2 page extractions + confidence
  getJobPages: (jobId: string) => request<any[]>(`/jobs/${jobId}/pages`),
  getJobConfidence: (jobId: string) => request<any>(`/jobs/${jobId}/confidence`),

  // Corrections (few-shot learning)
  submitCorrection: (data: any) =>
    request<any>('/corrections/', { method: 'POST', body: JSON.stringify(data) }),

  // Review queue
  reviewStats: () => request<any>('/review/stats'),
  reviewQueue: (params: Record<string, string | number | undefined> = {}) => {
    const qs = Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null && v !== '')
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
      .join('&');
    return request<any>(`/review/queue${qs ? `?${qs}` : ''}`);
  },
  reviewDetail: (jobId: string) => request<any>(`/review/${jobId}`),
  reviewApprove: (jobId: string, notes?: string) =>
    request<any>(`/review/${jobId}/approve`, { method: 'POST', body: JSON.stringify({ notes }) }),
  reviewReject: (jobId: string, notes: string) =>
    request<any>(`/review/${jobId}/reject`, { method: 'POST', body: JSON.stringify({ notes }) }),
  reviewBulkApprove: (job_ids: string[], notes?: string) =>
    request<any>('/review/bulk/approve', { method: 'POST', body: JSON.stringify({ job_ids, notes }) }),
  reviewBulkReject: (job_ids: string[], notes: string) =>
    request<any>('/review/bulk/reject', { method: 'POST', body: JSON.stringify({ job_ids, notes }) }),

  // Checks — per-field evidence awaiting a human decision
  evidenceQueue: (params: Record<string, string | number | undefined> = {}) => {
    const qs = Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null && v !== '')
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
      .join('&');
    return request<any>(`/evidence/queue${qs ? `?${qs}` : ''}`);
  },
  evidenceCount: () => request<{ count: number }>('/evidence/count'),
  evidenceForJob: (jobId: string) => request<any>(`/evidence/${jobId}`),
  evidenceResolve: (jobId: string, field: string, value: any,
                    choice: 'kept' | 'alternate' | 'manual', reason?: string) =>
    request<any>(`/evidence/${jobId}/${encodeURIComponent(field)}/resolve`,
                 { method: 'POST', body: JSON.stringify({ value, choice, reason }) }),
  // <img src> cannot send an Authorization header, so the crop takes ?token=
  // (same reason the PDF and page-image routes do).
  evidenceCropUrl: (jobId: string, field: string) =>
    `${BASE}/evidence/${jobId}/${encodeURIComponent(field)}.png?token=${encodeURIComponent(auth.token || '')}`,

  // Engine availability (super-admin enable/disable; all users read)
  getEngines: () => request<any>('/settings/engines'),
  saveEngines: (data: { enabled: string[]; default: string }) =>
    request<any>('/settings/engines', { method: 'PUT', body: JSON.stringify(data) }),

  // Auto-approve settings
  getAutoApprove: () => request<any>('/settings/auto-approve'),
  saveAutoApprove: (data: { enabled: boolean; threshold: number }) =>
    request<any>('/settings/auto-approve', { method: 'PUT', body: JSON.stringify(data) }),

  // Usage dashboard (admin) — KPIs + per-user + per-model + daily, date-ranged
  usageOverview: (dateFrom?: string, dateTo?: string) => {
    const qs = new URLSearchParams();
    if (dateFrom) qs.set('date_from', dateFrom);
    if (dateTo) qs.set('date_to', dateTo);
    const s = qs.toString();
    return request<any>(`/usage/overview${s ? `?${s}` : ''}`);
  },

  // ROVER — Release Order Verification & Extraction Reader
  roverStats: () => request<any>('/rover/stats'),
  roverDocuments: () => request<any[]>('/rover/documents'),
  roverDocument: (docId: string) => request<any>(`/rover/documents/${encodeURIComponent(docId)}`),
  roverProducts: () => request<any[]>('/rover/products'),
  roverReview: () => request<any[]>('/rover/review'),
  roverExtract: (pdfPath: string) =>
    request<any>('/rover/extract', { method: 'POST', body: JSON.stringify({ pdf_path: pdfPath }) }),
  roverUpload: (file: File) => {
    const form = new FormData();
    form.append('file', file);
    return request<any>('/rover/upload', { method: 'POST', body: form });
  },
  roverApply: (docId: string, corrections: Record<string, any>) =>
    request<any>(`/rover/review/${encodeURIComponent(docId)}/apply`, {
      method: 'POST', body: JSON.stringify({ corrections }),
    }),
  roverPending: () => request<any[]>('/rover/pending'),
  roverHistory: () => request<any[]>('/rover/history'),
  roverApprove: (docId: string, corrections: Record<string, any>) =>
    request<any>(`/rover/documents/${encodeURIComponent(docId)}/approve`, {
      method: 'POST', body: JSON.stringify({ corrections }),
    }),
  // annotated PNG needs the auth header, so fetch as a blob and return an object URL
  roverAnnotateUrl: async (docId: string): Promise<string> => {
    await auth.ensureValidToken();
    const res = await fetch(`${BASE}/rover/annotate/${encodeURIComponent(docId)}`, {
      headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {},
    });
    if (!res.ok) throw new Error('annotate failed');
    return URL.createObjectURL(await res.blob());
  },
  // full source PDF (all pages) as an authed object URL for inline <iframe> viewing
  roverPdfUrl: async (docId: string): Promise<string> => {
    await auth.ensureValidToken();
    const res = await fetch(`${BASE}/rover/documents/${encodeURIComponent(docId)}/pdf`, {
      headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {},
    });
    if (!res.ok) throw new Error('pdf failed');
    return URL.createObjectURL(await res.blob());
  },

  // ROVER Report Builder — OLAP-style cube over extracted documents/products
  cubeFields: (grain: string) => request<any>(`/rover/cube/fields?grain=${encodeURIComponent(grain)}`),
  cubeRun: (spec: any) => request<any>('/rover/cube/run', { method: 'POST', body: JSON.stringify(spec) }),
  cubeSave: (spec: any) => request<any>('/rover/cube/save', { method: 'POST', body: JSON.stringify(spec) }),
  cubeList: () => request<any[]>('/rover/cube/list'),
  cubeGet: (name: string) => request<any>(`/rover/cube/${encodeURIComponent(name)}`),
  cubeDelete: (name: string) => request<any>(`/rover/cube/${encodeURIComponent(name)}`, { method: 'DELETE' }),

  // Health
  health: () => request<any>('/health'),
};

export async function extractPDF(
  file: File,
  pipeline: PipelineKey = 'v11',
  token?: string,
  jobId?: string,
  engine: 'auto' | 'presto' | 'classic' | 'atlas' | 'rover' = 'auto'
): Promise<any> {
  const config = PIPELINES[pipeline];
  if (!config) throw new Error(`Unknown pipeline: ${pipeline}`);

  const formData = new FormData();
  formData.append('file', file);
  if (jobId) formData.append('job_id', jobId);
  // V12: typed-page engine choice — 'classic' (V7 Veritas) | 'presto' (fast) | 'auto'
  formData.append('engine', engine);

  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const res = await fetch(config.endpoint, {
    method: 'POST',
    headers,
    body: formData,
  });

  // CRITICAL (per CLAUDE.md): use res.text() not res.json() — body streaming hangs in some browsers
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`Extract failed (${res.status}): ${text.slice(0, 200)}`);
  }
  return JSON.parse(text);
}

/** Adapter: V7 returns title-case keys ("Declaration No"), V10/V11 return snake_case ("declaration_no").
 * Normalize to snake_case for unified UI rendering. */
const V7_TO_SNAKE_DECL: Record<string, string> = {
  'Declaration No': 'declaration_no',
  'Declaration Date': 'declaration_date',
  'Importer (Name)': 'importer_name',
  'Consignor (Name)': 'consignor_name',
  'Invoice Number': 'invoice_number',
  'Invoice Number (Customs Declaration)': 'invoice_number_customs',
  'Invoice Number (Commercial Invoice)': 'invoice_number_commercial',
  'Currency': 'currency',
  'Currency 2': 'currency_2',
  'Exchange Rate': 'exchange_rate',
  'Invoice Price': 'invoice_price',
  'Total Customs Value': 'total_customs_value',
  'Country Origin': 'country_origin',
  'Import/Export Customs Duty': 'customs_duty',
  'Commercial Tax (CT)': 'commercial_tax',
  'Advance Income Tax (AT)': 'advance_income_tax',
  'Security Fee (SF)': 'security_fee',
  'MACCS Service Fee (MF)': 'maccs_service_fee',
  'Exemption/Reduction': 'exemption',
};

const V7_TO_SNAKE_ITEM: Record<string, string> = {
  'Item name': 'item_name',
  'Customs duty rate': 'customs_duty_rate',
  'Quantity (1)': 'quantity',
  'Invoice unit price': 'invoice_unit_price',
  'CIF unit price': 'cif_unit_price',
  'Currency': 'currency',
  'Commercial tax %': 'commercial_tax_pct',
  'Exchange Rate (1)': 'exchange_rate',
  'HS Code': 'hs_code',
  'Origin Country': 'origin',
  'Customs Value (MMK)': 'customs_value_mmk',
};

export function normalizeExtractResult(raw: any): any {
  if (!raw) return raw;
  // If keys already snake (V10/V11), pass through
  const declRaw = raw.declaration || {};
  const declKeys = Object.keys(declRaw);
  const looksTitleCase = declKeys.some(k => k.includes(' ') || k.includes('('));
  if (!looksTitleCase) return raw;

  const decl: any = {};
  for (const [k, v] of Object.entries(declRaw)) {
    decl[V7_TO_SNAKE_DECL[k] || k] = v;
  }
  const items = (raw.items || []).map((it: any) => {
    const out: any = {};
    for (const [k, v] of Object.entries(it)) {
      out[V7_TO_SNAKE_ITEM[k] || k] = v;
    }
    return out;
  });
  return { ...raw, declaration: decl, items };
}
