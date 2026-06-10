/**
 * Display-only mapping from internal pipeline keys to the Atlas V14 agent names
 * shown to humans.
 *
 * IMPORTANT: this is for UI strings only. Do NOT use these names for API
 * routes, DB columns, form values, or anywhere they affect wiring.
 *
 *   v7 / classic typed    → ATLAS SWIFT   (V14-1)
 *   v10_pro / handwriting → ATLAS VISION  (V14-2)
 *   v11 / orchestrator    → ATLAS V14
 */
export const PIPELINE_DISPLAY: Record<string, string> = {
  v7: 'ATLAS SWIFT', V7: 'ATLAS SWIFT',
  v10_pro: 'ATLAS VISION', V10_PRO: 'ATLAS VISION', v10: 'ATLAS VISION', V10: 'ATLAS VISION',
  v11: 'ATLAS V14', V11: 'ATLAS V14',
  veritas: 'ATLAS SWIFT', scrivener: 'ATLAS VISION', maestro: 'ATLAS V14',
  presto: 'ATLAS SWIFT', swift: 'ATLAS SWIFT',
  scribe: 'ATLAS VISION', vision: 'ATLAS VISION',
  atlas: 'ATLAS V14',
};

export function pipeLabel(s?: string | null): string {
  if (!s) return '';
  return PIPELINE_DISPLAY[s] || PIPELINE_DISPLAY[s.toLowerCase()] || s.toUpperCase();
}
