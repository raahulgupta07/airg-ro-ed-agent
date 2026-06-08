<script lang="ts">
  let {
    label = '',
    type = 'text' as 'text' | 'password' | 'email' | 'number',
    placeholder = '',
    value = $bindable(''),
    disabled = false,
  } = $props();
</script>

<div>
  {#if label}
    <label class="block mb-1.5 text-xs font-medium" style="color: var(--on-surface-muted);">{label}</label>
  {/if}
  <input
    {type}
    {placeholder}
    {disabled}
    bind:value
    class="w-full text-sm transition-colors focus:outline-none"
    style="
      padding: 10px 14px;
      font-family: 'Inter', sans-serif;
      background: {disabled ? 'var(--surface-container)' : 'var(--surface-container-lowest)'};
      border: 1px solid var(--outline-variant);
      border-radius: var(--radius-md);
      color: {disabled ? 'var(--on-surface-subtle)' : 'var(--on-surface)'};
      {disabled ? 'cursor: not-allowed;' : ''}
    "
    onfocus={(e) => { e.currentTarget.style.borderColor = 'var(--primary)'; e.currentTarget.style.boxShadow = '0 0 0 3px var(--primary-container)'; }}
    onblur={(e) => { e.currentTarget.style.borderColor = 'var(--outline-variant)'; e.currentTarget.style.boxShadow = 'none'; }}
  />
</div>
