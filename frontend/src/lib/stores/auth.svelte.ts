import { goto } from '$app/navigation';

const TOKEN_KEY = 'ro_ed_token';
const REFRESH_KEY = 'ro_ed_refresh';
const USER_KEY = 'ro_ed_user';
const EXPIRY_KEY = 'ro_ed_expiry';
const MODE_KEY = 'ro_ed_auth_mode';
const VERIFIER_KEY = 'ro_ed_pkce_verifier';

interface Permissions {
  pages: Record<string, boolean>;
  actions: Record<string, boolean>;
  data_scope: string;
}

interface User {
  id: number;
  username: string;
  role: string;
  display_name?: string;
  email?: string;
  auth_type?: string;
  group?: { id: number; name: string } | null;
  permissions?: Permissions | null;
  must_change_password?: boolean;
}

interface OIDCConfig {
  auth_url: string;
  token_url: string;
  logout_url: string;
  client_id: string;
}

class AuthStore {
  token = $state<string | null>(null);
  refreshTokenValue = $state<string | null>(null);
  user = $state<User | null>(null);
  tokenExpiry = $state(0);
  authMode = $state<'local' | 'keycloak'>('local');
  oidcConfig = $state<OIDCConfig | null>(null);
  initializing = $state(true);

  isAuthenticated = $derived(!!this.token && !!this.user);
  isAdmin = $derived(this.user?.role === 'admin');
  isKeycloak = $derived(this.authMode === 'keycloak');

  canPage(page: string): boolean {
    if (this.user?.role === 'admin') return true;
    return this.user?.permissions?.pages?.[page] ?? true;
  }

  canAction(action: string): boolean {
    if (this.user?.role === 'admin') return true;
    return this.user?.permissions?.actions?.[action] ?? false;
  }

  private _refreshTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    if (typeof window !== 'undefined') {
      this._restoreFromStorage();
    }
  }

  private _restoreFromStorage() {
    this.token = localStorage.getItem(TOKEN_KEY);
    this.refreshTokenValue = localStorage.getItem(REFRESH_KEY);
    const saved = localStorage.getItem(USER_KEY);
    if (saved) {
      try { this.user = JSON.parse(saved); } catch { this.user = null; }
    }
    this.tokenExpiry = parseInt(localStorage.getItem(EXPIRY_KEY) || '0', 10);
    this.authMode = (localStorage.getItem(MODE_KEY) as 'local' | 'keycloak') || 'local';
  }

  private _persist() {
    if (this.token) localStorage.setItem(TOKEN_KEY, this.token);
    else localStorage.removeItem(TOKEN_KEY);

    if (this.refreshTokenValue) localStorage.setItem(REFRESH_KEY, this.refreshTokenValue);
    else localStorage.removeItem(REFRESH_KEY);

    if (this.user) localStorage.setItem(USER_KEY, JSON.stringify(this.user));
    else localStorage.removeItem(USER_KEY);

    localStorage.setItem(EXPIRY_KEY, String(this.tokenExpiry));
    localStorage.setItem(MODE_KEY, this.authMode);
  }

  private _clearStorage() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(REFRESH_KEY);
    localStorage.removeItem(USER_KEY);
    localStorage.removeItem(EXPIRY_KEY);
    localStorage.removeItem(MODE_KEY);
    sessionStorage.removeItem(VERIFIER_KEY);
  }

  // ── Local auth ──

  async loginLocal(token: string, user: User) {
    this.authMode = 'local';
    this.token = token;
    this.user = user;
    this.tokenExpiry = Date.now() + 60 * 60 * 1000; // 1hr
    this.refreshTokenValue = null;
    // Fetch full user info with permissions
    try {
      const res = await fetch('/api/auth/me', { headers: { 'Authorization': `Bearer ${token}` } });
      if (res.ok) this.user = await res.text().then(t => JSON.parse(t));
    } catch {}
    this._persist();
  }

  // ── Keycloak OIDC PKCE ──

  async fetchAuthConfig(): Promise<void> {
    try {
      const res = await fetch('/api/auth/config');
      const data = await res.text().then(t => JSON.parse(t));
      this.authMode = data.mode;
      if (data.mode === 'keycloak') {
        this.oidcConfig = {
          auth_url: data.auth_url,
          token_url: data.token_url,
          logout_url: data.logout_url,
          client_id: data.client_id,
        };
      } else {
        this.oidcConfig = null;
      }
      localStorage.setItem(MODE_KEY, data.mode);
    } catch {
      // Network error — use last known mode
    }
  }

  async initiateLogin(): Promise<void> {
    if (!this.oidcConfig) return;

    const { codeVerifier, codeChallenge } = await _generatePKCE();
    sessionStorage.setItem(VERIFIER_KEY, codeVerifier);

    const params = new URLSearchParams({
      response_type: 'code',
      client_id: this.oidcConfig.client_id,
      redirect_uri: window.location.origin + '/',
      code_challenge: codeChallenge,
      code_challenge_method: 'S256',
      scope: 'openid profile email',
    });

    window.location.href = `${this.oidcConfig.auth_url}?${params.toString()}`;
  }

  async handleCallback(code: string): Promise<boolean> {
    const codeVerifier = sessionStorage.getItem(VERIFIER_KEY);
    if (!codeVerifier) return false;
    sessionStorage.removeItem(VERIFIER_KEY);

    try {
      const res = await fetch('/api/auth/token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          code,
          redirect_uri: window.location.origin + '/',
          code_verifier: codeVerifier,
        }),
      });

      if (!res.ok) return false;

      const data = await res.text().then(t => JSON.parse(t));
      this.token = data.access_token;
      this.refreshTokenValue = data.refresh_token || null;
      this.tokenExpiry = Date.now() + (data.expires_in || 300) * 1000;
      this.authMode = 'keycloak';

      // Decode JWT payload (no verification needed client-side)
      const payload = _decodeJwtPayload(data.access_token);
      if (payload) {
        this.user = {
          id: 0,  // Will be set by /me call
          username: payload.preferred_username || payload.sub || '',
          role: (payload.realm_access?.roles || []).includes('admin') ? 'admin' : 'user',
          display_name: payload.name || payload.preferred_username || '',
        };
      }

      // Get actual user info (including SQLite id, permissions, group)
      try {
        const meRes = await fetch('/api/auth/me', {
          headers: { 'Authorization': `Bearer ${data.access_token}` },
        });
        if (meRes.ok) {
          const me = await meRes.text().then(t => JSON.parse(t));
          this.user = me;
        }
      } catch { /* payload user is good enough */ }

      this._persist();
      this._scheduleRefresh();

      // Clean URL
      window.history.replaceState({}, '', window.location.pathname);
      return true;
    } catch {
      return false;
    }
  }

  async refreshAccessToken(): Promise<boolean> {
    if (!this.refreshTokenValue) return false;

    try {
      const res = await fetch('/api/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: this.refreshTokenValue }),
      });

      if (!res.ok) {
        this.logout();
        return false;
      }

      const data = await res.text().then(t => JSON.parse(t));
      this.token = data.access_token;
      this.refreshTokenValue = data.refresh_token || this.refreshTokenValue;
      this.tokenExpiry = Date.now() + (data.expires_in || 300) * 1000;
      this._persist();
      this._scheduleRefresh();
      return true;
    } catch {
      return false;
    }
  }

  private _scheduleRefresh() {
    if (this._refreshTimer) clearTimeout(this._refreshTimer);
    if (this.authMode !== 'keycloak' || !this.refreshTokenValue) return;

    const msUntilExpiry = this.tokenExpiry - Date.now();
    const refreshIn = Math.max(msUntilExpiry - 60_000, 5_000); // 60s before expiry, min 5s

    this._refreshTimer = setTimeout(() => {
      this.refreshAccessToken();
    }, refreshIn);
  }

  async ensureValidToken(): Promise<void> {
    if (this.authMode !== 'keycloak') return;
    if (this.tokenExpiry - Date.now() < 30_000) {
      await this.refreshAccessToken();
    }
  }

  // ── Init (call on app mount) ──

  async init(): Promise<void> {
    this.initializing = true;

    await this.fetchAuthConfig();

    // Check for OIDC callback (?code=...)
    if (typeof window !== 'undefined') {
      const params = new URLSearchParams(window.location.search);
      const code = params.get('code');
      if (code && this.authMode === 'keycloak') {
        const ok = await this.handleCallback(code);
        if (ok) {
          this.initializing = false;
          return;
        }
      }
    }

    // Restore existing session
    if (this.token && this.user) {
      if (this.authMode === 'keycloak' && this.tokenExpiry < Date.now()) {
        const ok = await this.refreshAccessToken();
        if (!ok) {
          this._clear();
        }
      } else {
        this._scheduleRefresh();
      }
    }

    this.initializing = false;
  }

  // ── Logout ──

  logout() {
    const wasKeycloak = this.authMode === 'keycloak';
    const logoutUrl = this.oidcConfig?.logout_url;
    const clientId = this.oidcConfig?.client_id;

    // Best-effort backend logout
    if (this.token) {
      fetch('/api/auth/logout', {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${this.token}` },
      }).catch(() => {});
    }

    this._clear();

    if (wasKeycloak && logoutUrl && clientId) {
      const params = new URLSearchParams({
        client_id: clientId,
        post_logout_redirect_uri: window.location.origin + '/login',
      });
      window.location.href = `${logoutUrl}?${params.toString()}`;
    } else {
      goto('/login');
    }
  }

  private _clear() {
    if (this._refreshTimer) clearTimeout(this._refreshTimer);
    this.token = null;
    this.refreshTokenValue = null;
    this.user = null;
    this.tokenExpiry = 0;
    this._clearStorage();
  }
}

// ── PKCE helpers ──

async function _generatePKCE(): Promise<{ codeVerifier: string; codeChallenge: string }> {
  const array = new Uint8Array(32);
  crypto.getRandomValues(array);
  const codeVerifier = _base64UrlEncode(array);

  const encoder = new TextEncoder();
  const data = encoder.encode(codeVerifier);
  const hash = await crypto.subtle.digest('SHA-256', data);
  const codeChallenge = _base64UrlEncode(new Uint8Array(hash));

  return { codeVerifier, codeChallenge };
}

function _base64UrlEncode(bytes: Uint8Array): string {
  let binary = '';
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function _decodeJwtPayload(token: string): any {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    const payload = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(atob(payload));
  } catch {
    return null;
  }
}

export const auth = new AuthStore();
