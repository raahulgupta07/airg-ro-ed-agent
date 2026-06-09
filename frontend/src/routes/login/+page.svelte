<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { auth } from '$lib/stores/auth.svelte';
  import { api } from '$lib/api';

  let username = $state('');
  let password = $state('');
  let error = $state('');
  let loading = $state(false);
  let showPassword = $state(false);
  let redirecting = $state(false);
  let remember = $state(true);

  $effect(() => {
    if (auth.isAuthenticated) {
      if (auth.user?.must_change_password) goto('/change-password');
      else goto('/agent');
    }
  });

  async function handleLocalLogin() {
    error = '';
    loading = true;
    try {
      const res = await api.login(username, password);
      await auth.loginLocal(res.access_token, res.user);
      if (auth.user?.must_change_password) {
        goto('/change-password');
      } else {
        goto('/agent');
      }
    } catch (e: any) {
      error = e.message || 'Authentication failed';
    } finally {
      loading = false;
    }
  }

  async function handleKeycloakLogin() {
    redirecting = true;
    try {
      await auth.initiateLogin();
    } catch {
      redirecting = false;
      error = 'Redirect failed';
    }
  }

  const greeting = (() => {
    const h = new Date().getHours();
    if (h < 12) return 'Good morning';
    if (h < 18) return 'Good afternoon';
    return 'Good evening';
  })();

  const tiles = [
    { icon: 'upload_file',    label: 'Upload PDF'     },
    { icon: 'auto_awesome',   label: 'Classify pages' },
    { icon: 'description',    label: 'Typed extract'  },
    { icon: 'edit_note',      label: 'Handwritten'    },
    { icon: 'merge',          label: 'Merge results'  },
    { icon: 'checklist',      label: 'Review queue'   },
    { icon: 'history_edu',    label: 'Audit edits'    },
    { icon: 'file_download',  label: 'Export'         },
  ];

  // ── Decorative preview animation (login marketing panel only) ──
  const queries = [
    'Extract this customs declaration',
    'Classify printed vs handwritten',
    'Reconcile the item totals',
    'Route this to the review queue',
  ];
  let activeTile = $state(1);
  let queryIdx = $state(0);

  onMount(() => {
    const a = setInterval(() => { activeTile = (activeTile + 1) % tiles.length; }, 1500);
    const b = setInterval(() => { queryIdx = (queryIdx + 1) % queries.length; }, 3400);
    return () => { clearInterval(a); clearInterval(b); };
  });
</script>

<div class="min-h-screen flex flex-col" style="background: var(--surface);">

  <!-- Logo top-left -->
  <div class="px-8 pt-6">
    <img src="/cityagent-logo.png" alt="CityAgent · Release Order" style="width: 190px; height: auto;" />
  </div>

  <!-- Two-column hero -->
  <div class="flex-1 flex items-center justify-center px-6 py-8">
    <div class="w-full max-w-6xl grid lg:grid-cols-2 gap-10 lg:gap-16 items-center">

      <!-- LEFT: greeting + tagline + stats + form -->
      <div class="max-w-md w-full mx-auto lg:mx-0">
        <h1 class="font-serif" style="font-size: 38px; line-height: 1.15; color: var(--on-surface); letter-spacing: -0.02em; font-weight: 500;">
          {greeting},<br/>sign in to CityAgent
        </h1>
        <p class="mt-4 text-base" style="color: var(--on-surface-muted); line-height: 1.55;">
          Myanmar customs intelligence — classify, extract, reconcile and approve
          declarations at the desk.
        </p>
        <div class="mt-4 flex items-center gap-2 text-sm font-mono" style="color: var(--on-surface-muted);">
          <span class="w-2 h-2 rounded-full" style="background: var(--success);"></span>
          Atlas Gen 2 · Maestro router · queue-driven
        </div>

        <!-- Form card -->
        <div class="cl-panel mt-7">
          <div class="cl-bd space-y-4">

        {#if error}
          <div class="text-sm" style="color: var(--error);">
            {error}
          </div>
        {/if}

        <!-- SSO / LDAP top buttons -->
        {#if auth.isKeycloak}
          <button
            type="button"
            onclick={handleKeycloakLogin}
            disabled={redirecting}
            class="cl-btn w-full flex items-center justify-center gap-2"
          >
            <span class="w-1.5 h-1.5 rounded-full" style="background: var(--on-surface-muted);"></span>
            {redirecting ? 'Redirecting…' : 'Continue with SSO (SAML / OIDC)'}
          </button>
        {/if}

        <button
          type="button"
          disabled
          class="cl-btn w-full opacity-60 cursor-not-allowed"
          title="Use credentials below — LDAP auto-cascade on backend"
        >
          Continue with LDAP / Active Directory
        </button>

        <!-- OR divider -->
        <div class="flex items-center gap-3">
          <div class="flex-1 h-px" style="background: var(--line);"></div>
          <span class="text-xs font-medium" style="color: var(--on-surface-subtle); letter-spacing: 0.08em;">OR</span>
          <div class="flex-1 h-px" style="background: var(--line);"></div>
        </div>

        <!-- Username -->
        <div>
          <label class="cl-lbl" for="login-username">Username</label>
          <input
            id="login-username"
            type="text"
            placeholder="Username"
            bind:value={username}
            class="cl-inp"
          />
        </div>

        <!-- Password -->
        <div>
          <label class="cl-lbl" for="login-password">Password</label>
          <div class="relative">
            <input
              id="login-password"
              type={showPassword ? 'text' : 'password'}
              placeholder="Password"
              bind:value={password}
              class="cl-inp pr-16"
              onkeydown={(e) => { if (e.key === 'Enter' && username && password) handleLocalLogin(); }}
            />
            <button
              type="button"
              class="absolute right-3 top-1/2 -translate-y-1/2 text-xs font-medium cursor-pointer"
              style="color: var(--on-surface-muted);"
              onclick={() => showPassword = !showPassword}
            >
              {showPassword ? 'Hide' : 'Show'}
            </button>
          </div>
        </div>

        <!-- Remember -->
        <label class="flex items-center gap-2 cursor-pointer text-sm" style="color: var(--on-surface);">
          <input type="checkbox" bind:checked={remember}
                 class="w-4 h-4 cursor-pointer"
                 style="accent-color: var(--primary);" />
          Remember me on this device
        </label>

        <!-- CTA -->
        <button
          type="button"
          onclick={handleLocalLogin}
          disabled={loading || !username || !password}
          class="cl-btn primary w-full"
          class:opacity-50={loading || !username || !password}
        >
          {loading ? 'Signing in…' : 'Continue with email'}
        </button>
        </div>
      </div>
      </div>

      <!-- RIGHT: animated product preview -->
      <div class="hidden lg:block login-preview">
        <div class="cl-panel" style="background: var(--surface-container-low);">
          <div class="p-6" style="background-image: radial-gradient(var(--line) 1px, transparent 1px); background-size: 22px 22px;">

            <!-- sample query bubble (cycles) -->
            <div class="mb-6" style="min-height: 40px;">
              {#key queryIdx}
                <div class="inline-block px-4 py-2 text-sm font-medium text-white query-bubble"
                     style="background: var(--primary); border-radius: 12px 12px 12px 2px;">
                  “{queries[queryIdx]}”<span class="caret">▌</span>
                </div>
              {/key}
            </div>

            <!-- action tiles (active one cycles) -->
            <div class="grid grid-cols-4 gap-3 mb-6">
              {#each tiles as t, i}
                {@const on = i === activeTile}
                <div class="tile flex flex-col items-center justify-center gap-1.5 py-4 px-1 text-center"
                     class:tile-on={on}
                     style="background: var(--surface-container-lowest); border: 1px solid {on ? 'var(--primary)' : 'var(--line)'}; border-radius: var(--radius-md);">
                  <span class="material-symbols-outlined" style="font-size: 22px; color: {on ? 'var(--primary)' : 'var(--on-surface-muted)'};">{t.icon}</span>
                  <span class="text-xs font-medium" style="color: {on ? 'var(--primary-hover)' : 'var(--on-surface)'};">{t.label}</span>
                </div>
              {/each}
            </div>

            <!-- input bar -->
            <div class="flex items-center gap-2">
              <div class="flex-1 flex items-center gap-2 px-3 py-2.5"
                   style="background: var(--surface-container-lowest); border: 1px solid var(--line); border-radius: var(--radius-md);">
                <span class="material-symbols-outlined" style="font-size: 18px; color: var(--on-surface-subtle);">science</span>
                <span class="text-sm" style="color: var(--on-surface-muted);">CityAgent · Maestro router</span>
                <span class="ml-auto flex gap-1">
                  <span class="dot-pulse" style="animation-delay: 0s;"></span>
                  <span class="dot-pulse" style="animation-delay: .18s;"></span>
                  <span class="dot-pulse" style="animation-delay: .36s;"></span>
                </span>
              </div>
              <button type="button" class="go-btn px-4 py-2.5 text-sm font-medium" style="background: var(--primary-soft); color: var(--primary-hover); border-radius: var(--radius-md); border: none; cursor: default;">
                Let's go →
              </button>
            </div>
          </div>
        </div>
        <p class="mt-4 text-center text-xs" style="color: var(--on-surface-subtle);">
          © 2026 City Holdings Myanmar · CityAgent · Release Order
        </p>
      </div>

    </div>
  </div>
</div>

<style>
  /* Right preview entrance */
  .login-preview { animation: previewIn .6s cubic-bezier(.22,1,.36,1) both; }
  @keyframes previewIn {
    from { opacity: 0; transform: translateY(14px) scale(.985); }
    to   { opacity: 1; transform: translateY(0) scale(1); }
  }

  /* Cycling query bubble */
  .query-bubble { animation: bubbleIn .45s cubic-bezier(.22,1,.36,1) both; box-shadow: var(--shadow-md); }
  @keyframes bubbleIn {
    from { opacity: 0; transform: translateY(-8px) scale(.96); }
    to   { opacity: 1; transform: translateY(0) scale(1); }
  }
  .caret { display: inline-block; margin-left: 1px; animation: caretBlink 1s steps(1) infinite; opacity: .85; }
  @keyframes caretBlink { 50% { opacity: 0; } }

  /* Active tile pulse + lift */
  .tile { transition: transform .35s cubic-bezier(.22,1,.36,1), border-color .35s, box-shadow .35s; }
  .tile-on { transform: translateY(-2px) scale(1.04); box-shadow: 0 0 0 3px var(--primary-tint), var(--shadow-sm); animation: tileGlow 1.5s ease-in-out; }
  @keyframes tileGlow {
    0%   { box-shadow: 0 0 0 0 var(--primary-tint), var(--shadow-sm); }
    40%  { box-shadow: 0 0 0 5px var(--primary-tint), var(--shadow-md); }
    100% { box-shadow: 0 0 0 3px var(--primary-tint), var(--shadow-sm); }
  }

  /* Typing dots in input bar */
  .dot-pulse { width: 5px; height: 5px; border-radius: 50%; background: var(--primary); display: inline-block; animation: dotPulse 1.1s ease-in-out infinite; }
  @keyframes dotPulse { 0%, 60%, 100% { opacity: .25; transform: scale(.8); } 30% { opacity: 1; transform: scale(1); } }

  .go-btn { transition: background .2s; }

  @media (prefers-reduced-motion: reduce) {
    .login-preview, .query-bubble, .tile-on, .caret, .dot-pulse { animation: none; }
  }
</style>
