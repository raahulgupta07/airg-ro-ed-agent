export const decisionColors: Record<string, string> = {
  ACCEPT: '#007518',
  ACCEPTED: '#007518',
  FIX_FIELDS: '#ff9d00',
  FIXED: '#ff9d00',
  FULL_RETRY: '#f95630',
  ESCALATE: '#be2d06',
  ESCALATED: '#be2d06',
};

export function getAccuracyColor(accuracy: number): string {
  if (accuracy >= 90) return '#007518';
  if (accuracy >= 60) return '#ff9d00';
  if (accuracy >= 30) return '#f95630';
  return '#be2d06';
}

// Dynamic page type colors — AI decides categories, colors generated from name
// Same name always produces same color (deterministic hash)
// Works for ANY document type — customs, medical, bank, legal, etc.

function hashString(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    const char = str.charCodeAt(i);
    hash = ((hash << 5) - hash) + char;
    hash = hash & hash; // Convert to 32bit integer
  }
  return Math.abs(hash);
}

// Get the "category" from a page type by extracting key words
function getCategory(pageType: string): string {
  const lower = pageType.toLowerCase();
  // Group similar types together so they get same color family
  if (lower.includes('declaration') || lower.includes('release order') || lower.includes('customs')) return 'customs';
  if (lower.includes('invoice')) return 'invoice';
  if (lower.includes('packing')) return 'packing';
  if (lower.includes('bill of lading') || lower.includes('waybill') || lower.includes('b/l')) return 'shipping';
  if (lower.includes('delivery order') || lower.includes('d/o')) return 'delivery';
  if (lower.includes('licence') || lower.includes('license')) return 'licence';
  if (lower.includes('certificate') || lower.includes('recommendation')) return 'certificate';
  if (lower.includes('insurance') || lower.includes('stamp')) return 'insurance';
  if (lower.includes('valuation')) return 'valuation';
  if (lower.includes('note sheet') || lower.includes('correspondence')) return 'note';
  if (lower.includes('photograph') || lower.includes('photo')) return 'photo';
  if (lower.includes('contract') || lower.includes('agreement')) return 'contract';
  // For unknown types, use the first significant word as category
  const words = lower.split(/[\s_/()-]+/).filter(w => w.length > 3);
  return words[0] || 'other';
}

// 24 distinct hues spread evenly across the color wheel
const HUE_PALETTE = [
  210, 150, 30, 270, 60, 330, 180, 0, 240, 90, 300, 120,
  195, 45, 285, 135, 15, 255, 75, 315, 165, 345, 225, 105,
];

const categoryHueCache: Record<string, number> = {};

function getCategoryHue(category: string): number {
  if (!(category in categoryHueCache)) {
    // Assign next hue from palette (deterministic per session, spread apart)
    const hash = hashString(category);
    categoryHueCache[category] = HUE_PALETTE[hash % HUE_PALETTE.length];
  }
  return categoryHueCache[category];
}

export function getPageTypeColor(pageType: string): { bg: string; text: string } {
  const category = getCategory(pageType);
  const hue = getCategoryHue(category);

  // Dark background, light text — looks good on white page
  const bg = `hsl(${hue}, 45%, 22%)`;
  const text = `hsl(${hue}, 60%, 75%)`;
  return { bg, text };
}
