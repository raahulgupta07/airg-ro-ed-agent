/**
 * Display-only mapping from internal pipeline keys (v7 / v10_pro / v11) to
 * the fancy agent names shown to humans (Combo A naming).
 *
 * IMPORTANT: this is for UI strings only. Do NOT use these names for API
 * routes, DB columns, form values, or anywhere they affect wiring.
 *
 *   v7      → VERITAS
 *   v10_pro → SCRIVENER   (v10 also maps here for legacy display)
 *   v11     → MAESTRO
 */
export const PIPELINE_DISPLAY: Record<string, string> = {
  v7: 'VERITAS', V7: 'VERITAS',
  v10_pro: 'SCRIVENER', V10_PRO: 'SCRIVENER', v10: 'SCRIVENER', V10: 'SCRIVENER',
  v11: 'MAESTRO', V11: 'MAESTRO',
  veritas: 'VERITAS', scrivener: 'SCRIVENER', maestro: 'MAESTRO',
};

export function pipeLabel(s?: string | null): string {
  if (!s) return '';
  return PIPELINE_DISPLAY[s] || PIPELINE_DISPLAY[s.toLowerCase()] || s.toUpperCase();
}
