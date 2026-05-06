/** Pipeline metadata for the agent UI selector. */
export type PipelineKey = 'v11' | 'v7' | 'v10';

export interface PipelineConfig {
  key: PipelineKey;
  name: string;
  endpoint: string;
  description: string;
  estCost: string;
  estTime: string;
  isDefault?: boolean;
  badge?: string;
}

export const PIPELINES: Record<PipelineKey, PipelineConfig> = {
  v11: {
    key: 'v11',
    name: 'Smart Router',
    endpoint: '/api/extract-v11',
    description: 'Auto-classifies each page → routes typed→Veritas, handwritten→Scrivener, drops attachments. Best for mixed bundles.',
    estCost: '$0.08–0.40',
    estTime: '60–150s',
    isDefault: true,
    badge: 'Recommended',
  },
  v7: {
    key: 'v7',
    name: 'Fast Typed',
    endpoint: '/api/extract',
    description: 'Cheapest. Best for clean MACCS typed forms. Production-tested, full UI features.',
    estCost: '$0.10',
    estTime: '70s',
  },
  v10: {
    key: 'v10',
    name: 'Handwriting Specialist',
    endpoint: '/api/extract-v10',
    description: 'Multi-DPI 3-model vote on critical fields. Best for ink-pen-filled CUSDEC-1 forms.',
    estCost: '$0.50',
    estTime: '160s',
    badge: 'Premium',
  },
};

export const PIPELINE_ORDER: PipelineKey[] = ['v11', 'v7', 'v10'];

export function getDefaultPipeline(): PipelineKey {
  return (Object.values(PIPELINES).find(p => p.isDefault)?.key) || 'v11';
}
