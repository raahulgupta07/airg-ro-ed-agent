/** Pipeline metadata for the agent UI selector. */
export type PipelineKey = 'v11';

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
};

export const PIPELINE_ORDER: PipelineKey[] = ['v11'];

export function getDefaultPipeline(): PipelineKey {
  return (Object.values(PIPELINES).find(p => p.isDefault)?.key) || 'v11';
}
