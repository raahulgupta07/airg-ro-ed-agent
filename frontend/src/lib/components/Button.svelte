<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    variant = 'primary' as 'primary' | 'secondary' | 'danger' | 'ghost' | 'dark',
    size = 'md' as 'sm' | 'md' | 'lg',
    disabled = false,
    onclick = undefined as (() => void) | undefined,
    children,
  }: {
    variant?: 'primary' | 'secondary' | 'danger' | 'ghost' | 'dark';
    size?: 'sm' | 'md' | 'lg';
    disabled?: boolean;
    onclick?: (() => void) | undefined;
    children: Snippet;
  } = $props();

  const styles = $derived({
    primary:   'background: var(--primary); color: var(--on-primary); border: 1px solid var(--primary);',
    secondary: 'background: var(--surface-container-lowest); color: var(--on-surface); border: 1px solid var(--outline);',
    danger:    'background: var(--error); color: #fff; border: 1px solid var(--error);',
    ghost:     'background: transparent; color: var(--on-surface); border: 1px solid transparent;',
    dark:      'background: var(--secondary); color: #fff; border: 1px solid var(--secondary);',
  }[variant]);

  const padding = $derived({
    sm: 'padding: 6px 12px; font-size: 12px; border-radius: var(--radius-sm);',
    md: 'padding: 8px 16px; font-size: 13px; border-radius: var(--radius-md);',
    lg: 'padding: 11px 22px; font-size: 14px; border-radius: var(--radius-md);',
  }[size]);
</script>

<button
  class="press-effect font-medium cursor-pointer transition-all duration-150 hover:opacity-90"
  class:opacity-50={disabled}
  class:cursor-not-allowed={disabled}
  style="{styles} {padding} box-shadow: var(--shadow-xs);"
  {disabled}
  onclick={onclick}
>
  {@render children()}
</button>
