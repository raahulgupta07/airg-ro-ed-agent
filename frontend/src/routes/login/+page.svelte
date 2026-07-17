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
  // Each step pairs a query with the tile it activates (kept in sync).
  const steps = [
    { q: 'Extract this customs declaration', tile: 2 }, // Typed extract
    { q: 'Classify printed vs handwritten',  tile: 1 }, // Classify pages
    { q: 'Reconcile the item totals',        tile: 4 }, // Merge results
    { q: 'Route this to the review queue',   tile: 5 }, // Review queue
  ];
  let step = $state(0);
  const activeTile = $derived(steps[step].tile);
  const activeQuery = $derived(steps[step].q);

  onMount(() => {
    const t = setInterval(() => { step = (step + 1) % steps.length; }, 2600);
    return () => clearInterval(t);
  });
</script>

<div class="login-root" style="background: var(--surface);">

  <!-- Logo top-left -->
  <a href="/" class="login-logo no-underline">
    <img src="/cityagent-logo-web.png" alt="City Agent ROVER · Release Order" />
  </a>

  <!-- Hero: vertically + horizontally centered, responsive -->
  <div class="login-hero">
    <div class="login-grid">

      <!-- LEFT: greeting + tagline + stats + form -->
      <div class="login-left">
        <h1 class="font-serif login-h1">
          {greeting},<br/>sign in to CityAgent
        </h1>
        <p class="login-sub">
          Myanmar customs intelligence — classify, extract, reconcile and approve
          declarations at the desk.
        </p>
        <div class="login-status">
          <span class="w-2 h-2 rounded-full" style="background: var(--success);"></span>
          Atlas V14 · queue-driven router
        </div>

        <!-- Form card -->
        <div class="cl-panel login-card">
          <div class="cl-bd space-y-4">

            {#if error}
              <div class="text-sm px-3 py-2" style="color: var(--error); background: var(--error-soft); border-radius: var(--radius-sm);">
                {error}
              </div>
            {/if}

            <!-- SSO (only when Keycloak configured) -->
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

            <!-- Username -->
            <div>
              <label class="cl-lbl" for="login-username">Username</label>
              <input
                id="login-username"
                type="text"
                placeholder="Your username"
                autocomplete="username"
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
                  placeholder="Your password"
                  autocomplete="current-password"
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

            <!-- Primary CTA -->
            <button
              type="button"
              onclick={handleLocalLogin}
              disabled={loading || !username || !password}
              class="cl-btn primary w-full login-cta"
            >
              {loading ? 'Signing in…' : 'Sign in'}
            </button>

            <!-- LDAP note (de-emphasised; backend auto-cascades) -->
            <p class="text-center text-xs" style="color: var(--on-surface-subtle);">
              LDAP / Active Directory accounts sign in with the same fields.
            </p>
          </div>
        </div>

        <p class="login-foot">© 2026 City Holdings Myanmar · City Agent ROVER · Release Order</p>
      </div>

      <!-- RIGHT: animated product preview -->
      <div class="login-preview">
        <div class="cl-panel" style="background: var(--surface-container-low);">
          <div class="login-preview-bd">

            <div class="flex items-center justify-between mb-5">
              <span class="pill clay">Live preview</span>
              <span class="text-xs font-mono" style="color: var(--on-surface-subtle);">illustrative</span>
            </div>

            <!-- sample query bubble (synced to active tile) -->
            <div class="mb-6" style="min-height: 40px;">
              {#key step}
                <div class="inline-block px-4 py-2 text-sm font-medium text-white query-bubble"
                     style="background: var(--primary); border-radius: 12px 12px 12px 2px;">
                  “{activeQuery}”<span class="caret">▌</span>
                </div>
              {/key}
            </div>

            <!-- action tiles (active one synced to query) -->
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
              <div class="flex-1 flex items-center gap-2 px-3 py-2.5 min-w-0"
                   style="background: var(--surface-container-lowest); border: 1px solid var(--line); border-radius: var(--radius-md);">
                <span class="material-symbols-outlined" style="font-size: 18px; color: var(--on-surface-subtle);">science</span>
                <span class="text-sm truncate" style="color: var(--on-surface-muted);">CityAgent · Atlas V14</span>
                <span class="ml-auto flex gap-1 shrink-0">
                  <span class="dot-pulse" style="animation-delay: 0s;"></span>
                  <span class="dot-pulse" style="animation-delay: .18s;"></span>
                  <span class="dot-pulse" style="animation-delay: .36s;"></span>
                </span>
              </div>
              <span class="go-btn px-4 py-2.5 text-sm font-medium shrink-0" style="background: var(--primary-soft); color: var(--primary-hover); border-radius: var(--radius-md);">
                Let's go →
              </span>
            </div>
          </div>
        </div>
      </div>

    </div>
  </div>
</div>

<style>
  /* ===== Responsive shell — fits any screen ===== */
  .login-root { position: relative; min-height: 100vh; min-height: 100dvh; display: flex; flex-direction: column; }
  .login-logo { position: absolute; top: clamp(16px, 3vw, 28px); left: clamp(16px, 3vw, 36px); z-index: 10; }
  .login-logo img { width: clamp(140px, 14vw, 180px); height: auto; display: block; }

  .login-hero { flex: 1; display: grid; place-items: center;
                padding: clamp(96px, 14vh, 140px) clamp(20px, 5vw, 56px) clamp(32px, 6vh, 64px); }
  .login-grid { width: 100%; max-width: 1160px; display: grid; grid-template-columns: 1fr;
                gap: clamp(28px, 5vw, 64px); align-items: center; }

  .login-left { width: 100%; max-width: 440px; justify-self: center; }
  .login-h1 { font-size: clamp(28px, 4.2vw, 40px); line-height: 1.12; letter-spacing: -0.02em;
              font-weight: 500; color: var(--on-surface); }
  .login-sub { margin-top: 16px; font-size: clamp(14px, 1.4vw, 16px); line-height: 1.55; color: var(--on-surface-muted); }
  .login-status { margin-top: 16px; display: flex; align-items: center; gap: 8px;
                  font-family: 'JetBrains Mono', monospace; font-size: 13px; color: var(--on-surface-muted); }
  .login-card { margin-top: 26px; }
  .login-foot { margin-top: 20px; font-size: 12px; color: var(--on-surface-subtle); }

  /* Neutral (not faded-primary) disabled CTA — fixes "looks broken" */
  .login-cta:disabled { background: var(--surface-container-high); color: var(--on-surface-subtle);
                        border-color: var(--line); cursor: not-allowed; opacity: 1; }

  .login-preview { width: 100%; max-width: 560px; justify-self: center;
                   animation: previewIn .6s cubic-bezier(.22,1,.36,1) both; }
  .login-preview-bd { padding: clamp(16px, 2.2vw, 26px);
                      background-image: radial-gradient(var(--line) 1px, transparent 1px); background-size: 22px 22px; }

  /* Single column under 980px — preview drops below, then hides on small */
  @media (min-width: 980px) { .login-grid { grid-template-columns: 1.05fr 1fr; }
                              .login-left { justify-self: start; } }
  @media (max-width: 979px) { .login-preview { display: none; } }

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
