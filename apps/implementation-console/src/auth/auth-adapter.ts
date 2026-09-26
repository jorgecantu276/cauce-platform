import type { StaffSession } from "../api/contracts";

export interface AuthAdapter {
  restoreSession(): Promise<StaffSession | null>;
  getAccessToken(): Promise<string | null>;
  signIn(): Promise<void>;
  signOut(): Promise<void>;
  /**
   * Drops a locally-held token that the API has rejected (e.g. after a 401),
   * without the full end_session/IdP-logout redirect that signOut() performs.
   * There is no token refresh here: the only recovery from an expired/invalid
   * access token is discarding it and asking the user to sign in again.
   */
  clearSession(): Promise<void>;
}

declare global {
  interface Window { __CANONICAL_STAFF_AUTH__?: AuthAdapter; }
}

type OidcConfiguration = { authorization_endpoint: string; token_endpoint: string; end_session_endpoint?: string };
type Tokens = { accessToken: string; idToken?: string };

const storageKey = "cauce.staff.oidc.tokens";
const transactionKey = "cauce.staff.oidc.transaction";

function fixtureSession(): StaffSession {
  return { subject: "fixture-owner-01", displayName: "Patricia", platformRole: "super_admin", memberships: [{ businessId: "fixture-matamoros-01", businessName: "Negocio de ejemplo", role: "owner", capabilities: ["charges:write", "reviews:read", "reviews:resolve", "refunds:request"] }] };
}

class FixtureAuthAdapter implements AuthAdapter {
  async restoreSession() { return fixtureSession(); }
  async getAccessToken() { return null; }
  async signIn() { return undefined; }
  async signOut() { return undefined; }
  async clearSession() { return undefined; }
}

class UnconfiguredAuthAdapter implements AuthAdapter {
  async restoreSession() { return null; }
  async getAccessToken() { return null; }
  async signIn() { throw new Error("El acceso de equipo no está configurado."); }
  async signOut() { return undefined; }
  async clearSession() { return undefined; }
}

class OidcAuthAdapter implements AuthAdapter {
  private discovery: Promise<OidcConfiguration> | null = null;

  constructor(private readonly issuer: string, private readonly clientId: string, private readonly apiBaseUrl: string, private readonly redirectUri: string) {}

  private configuration(): Promise<OidcConfiguration> {
    this.discovery ??= fetch(`${this.issuer.replace(/\/$/, "")}/.well-known/openid-configuration`, { credentials: "omit" })
      .then(async (response) => {
        if (!response.ok) throw new Error("No pudimos leer la configuración de acceso.");
        return response.json() as Promise<OidcConfiguration>;
      });
    return this.discovery;
  }

  private loadTokens(): Tokens | null {
    try { return JSON.parse(sessionStorage.getItem(storageKey) ?? "null") as Tokens | null; } catch { return null; }
  }

  private saveTokens(tokens: Tokens | null) {
    if (tokens) sessionStorage.setItem(storageKey, JSON.stringify(tokens));
    else sessionStorage.removeItem(storageKey);
  }

  // API Gateway's JWT authorizer validates the registered app-client
  // audience (`aud`). Cognito places that claim on the ID token; the access
  // token carries `client_id` instead. Prefer the ID token for browser-to-API
  // calls while retaining the access token as a compatibility fallback.
  async getAccessToken() {
    const tokens = this.loadTokens();
    return tokens?.idToken ?? tokens?.accessToken ?? null;
  }

  async clearSession() { this.saveTokens(null); }

  async restoreSession(): Promise<StaffSession | null> {
    const params = new URLSearchParams(window.location.search);
    if (params.get("error")) {
      this.clearCallback();
      return null;
    }
    if (params.get("code")) {
      const token = await this.exchangeCallback(params);
      if (!token) return null;
      return this.sessionFor(token);
    }
    const token = await this.getAccessToken();
    return token ? this.sessionFor(token) : null;
  }

  async signIn() {
    const verifier = randomValue();
    const state = randomValue();
    const nonce = randomValue();
    sessionStorage.setItem(transactionKey, JSON.stringify({ verifier, state, nonce }));
    const config = await this.configuration();
    const url = new URL(config.authorization_endpoint);
    url.search = new URLSearchParams({
      response_type: "code", client_id: this.clientId, redirect_uri: this.redirectUri,
      scope: "openid profile email", state, nonce, code_challenge: await challengeFor(verifier), code_challenge_method: "S256",
    }).toString();
    window.location.assign(url.toString());
  }

  async signOut() {
    const idToken = this.loadTokens()?.idToken;
    this.saveTokens(null);
    sessionStorage.removeItem(transactionKey);
    const config = await this.configuration().catch(() => null);
    if (config?.end_session_endpoint) {
      const url = new URL(config.end_session_endpoint);
      url.search = new URLSearchParams({ post_logout_redirect_uri: this.redirectUri, ...(idToken ? { id_token_hint: idToken } : {}) }).toString();
      window.location.assign(url.toString());
    }
  }

  private async exchangeCallback(params: URLSearchParams): Promise<string | null> {
    let transaction: { verifier: string; state: string; nonce: string } | null = null;
    try { transaction = JSON.parse(sessionStorage.getItem(transactionKey) ?? "null"); } catch { /* invalid transaction */ }
    if (!transaction || transaction.state !== params.get("state")) {
      this.clearCallback();
      return null;
    }
    const config = await this.configuration();
    const body = new URLSearchParams({ grant_type: "authorization_code", code: params.get("code") ?? "", redirect_uri: this.redirectUri, client_id: this.clientId, code_verifier: transaction.verifier });
    const response = await fetch(config.token_endpoint, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body, credentials: "omit" });
    if (!response.ok) {
      this.clearCallback();
      return null;
    }
    const result = await response.json() as { access_token?: string; id_token?: string };
    if (!result.access_token) {
      this.clearCallback();
      return null;
    }
    // The nonce we sent binds this specific authorization request to the
    // returned id_token, on top of what `state` already binds to the browser
    // transaction -- it stops a replayed/substituted id_token from a
    // different authorization attempt from being accepted here.
    if (result.id_token && decodeClaims(result.id_token).nonce !== transaction.nonce) {
      this.saveTokens(null);
      this.clearCallback();
      return null;
    }
    this.saveTokens({ accessToken: result.access_token, idToken: result.id_token });
    sessionStorage.removeItem(transactionKey);
    this.clearCallback();
    return result.access_token;
  }

  private clearCallback() {
    const url = new URL(window.location.href);
    ["code", "state", "error", "error_description"].forEach((key) => url.searchParams.delete(key));
    window.history.replaceState({}, document.title, `${url.pathname}${url.search}${url.hash}`);
  }

  private async sessionFor(accessToken: string): Promise<StaffSession | null> {
    const apiToken = this.loadTokens()?.idToken ?? accessToken;
    // The staff API authenticates exclusively with the Bearer token. Sending
    // cross-origin browser cookies makes the response subject to credentialed
    // CORS rules even though no cookie is needed, which turns a valid 200 into
    // an opaque browser failure and appears to the UI as a login loop.
    const response = await fetch(`${this.apiBaseUrl}/session`, { headers: { Authorization: `Bearer ${apiToken}`, Accept: "application/json" } });
    if (!response.ok) {
      this.saveTokens(null);
      return null;
    }
    const value = await response.json() as { memberships?: StaffSession["memberships"]; platformRole?: unknown };
    const claims = decodeClaims(this.loadTokens()?.idToken ?? accessToken);
    return {
      subject: typeof claims.sub === "string" ? claims.sub : "",
      displayName: displayName(claims),
      platformRole: value.platformRole === "super_admin" ? "super_admin" : null,
      memberships: value.memberships ?? [],
    };
  }
}

function randomValue(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return base64url(bytes);
}

async function challengeFor(value: string): Promise<string> {
  return base64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value))));
}

function base64url(bytes: Uint8Array): string {
  let text = "";
  bytes.forEach((value) => { text += String.fromCharCode(value); });
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function decodeClaims(token: string): Record<string, unknown> {
  try {
    const body = token.split(".")[1]?.replace(/-/g, "+").replace(/_/g, "/");
    if (!body) return {};
    return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(body.padEnd(Math.ceil(body.length / 4) * 4, "=")), (value) => value.charCodeAt(0)))) as Record<string, unknown>;
  } catch { return {}; }
}

function displayName(claims: Record<string, unknown>): string {
  for (const key of ["name", "preferred_username", "email", "sub"]) {
    if (typeof claims[key] === "string" && claims[key]) return claims[key];
  }
  return "Equipo";
}

export function getAuthAdapter(): { adapter: AuthAdapter; source: "live" | "fixture" } {
  if (window.__CANONICAL_STAFF_AUTH__) return { adapter: window.__CANONICAL_STAFF_AUTH__, source: "live" };
  const issuer = import.meta.env.VITE_OIDC_ISSUER;
  const clientId = import.meta.env.VITE_OIDC_CLIENT_ID;
  const apiBaseUrl = import.meta.env.VITE_STAFF_API_BASE_URL?.replace(/\/$/, "");
  if (issuer && clientId && apiBaseUrl) {
    const redirectUri = import.meta.env.VITE_OIDC_REDIRECT_URI ?? `${window.location.origin}${window.location.pathname}`;
    return { adapter: new OidcAuthAdapter(issuer, clientId, apiBaseUrl, redirectUri), source: "live" };
  }
  if (import.meta.env.DEV || import.meta.env.VITE_ENABLE_FIXTURES === "true") return { adapter: new FixtureAuthAdapter(), source: "fixture" };
  return { adapter: new UnconfiguredAuthAdapter(), source: "live" };
}
