<script lang="ts">
  let {
    text = '',
    variant = 'info' as 'critical' | 'warning' | 'info' | 'success' | 'normal' | 'secondary' | 'tertiary',
    size = 'sm' as 'sm' | 'md',
  } = $props();

  // Soft tinted backgrounds + readable text
  const palette: Record<string, { bg: string; fg: string }> = {
    critical:  { bg: '#F4DCD8', fg: '#7A2C24' },
    warning:   { bg: '#F4E6CB', fg: '#7A5520' },
    info:      { bg: '#E3E6EA', fg: '#3B4754' },
    success:   { bg: '#DCE8DC', fg: '#2F5A2F' },
    normal:    { bg: '#E8E5DA', fg: '#5A5750' },
    secondary: { bg: '#D8E1E5', fg: '#2F4A52' },
    tertiary:  { bg: '#EFE3F1', fg: '#5A4360' },
  };

  const c = $derived(palette[variant] || palette.info);
  const pad = $derived(size === 'sm'
    ? 'padding: 2px 8px; font-size: 11px;'
    : 'padding: 3px 12px; font-size: 12px;');
</script>

<span
  class="inline-block font-medium"
  style="{pad} background: {c.bg}; color: {c.fg}; border-radius: 999px; letter-spacing: 0.01em;"
>
  {text}
</span>
