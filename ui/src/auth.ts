/**
 * Keycloak-backed auth for the AWE admin SPA.
 *
 * Flow on boot:
 *   1. Fetch /config.json (nginx-served; Helm overwrites via ConfigMap).
 *   2. If `keycloak.url` is set → init keycloak-js with `onLoad: login-required`.
 *      Browser redirects to Keycloak for login; on return we have a token
 *      and the app renders.
 *   3. If `keycloak.url` is empty (dev mode) → skip Keycloak entirely and
 *      fall back to an unsigned dev token carrying AWE_ADMIN. The backend's
 *      dev-mode auth (`issuer=""`) accepts it. Never reachable in prod.
 *
 * The rest of the app never imports keycloak-js directly — use `getToken()`,
 * `getRoles()`, `hasRole()`, `logout()` below.
 */

import Keycloak from "keycloak-js";

// ---------------------------------------------------------------------------
// Config loading
// ---------------------------------------------------------------------------
export interface AuthConfig {
  keycloak: {
    url: string;
    realm: string;
    clientId: string;
  };
}

let config: AuthConfig | null = null;
let keycloak: Keycloak | null = null;

async function loadConfig(): Promise<AuthConfig> {
  if (config) return config;
  const resp = await fetch("/config.json", { cache: "no-store" });
  if (!resp.ok) {
    throw new Error(`Failed to load /config.json: ${resp.status}`);
  }
  config = (await resp.json()) as AuthConfig;
  return config;
}

// ---------------------------------------------------------------------------
// Dev-mode fallback
// ---------------------------------------------------------------------------
// Unsigned JWT with AWE_ADMIN role. Only usable when the backend's
// keycloak.issuer is empty (dev mode). Do not rely on this in production.
const DEV_TOKEN =
  "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0." +
  btoa(
    JSON.stringify({
      sub: "dev-admin",
      email: "dev-admin@local",
      realm_access: { roles: ["AWE_ADMIN"] },
    })
  )
    .replace(/=+$/, "")
    .replace(/\+/g, "-")
    .replace(/\//g, "_") +
  ".";

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------
export interface CurrentUser {
  sub: string;
  email?: string;
  name?: string;
  roles: string[];
  isAdmin: boolean;
  isViewer: boolean;
  devMode: boolean;
}

let currentUser: CurrentUser | null = null;

/**
 * Initialise auth. Call once at app startup before rendering <App/>.
 * Returns when the user is authenticated (in prod) or on dev fallback.
 */
export async function initAuth(): Promise<CurrentUser> {
  const cfg = await loadConfig();

  if (!cfg.keycloak.url) {
    // Dev mode — no Keycloak.
    currentUser = {
      sub: "dev-admin",
      email: "dev-admin@local",
      roles: ["AWE_ADMIN"],
      isAdmin: true,
      isViewer: true,
      devMode: true,
    };
    return currentUser;
  }

  // Stash on window so the error page in main.tsx can show the operator
  // exactly which Keycloak URL / realm / clientId the SPA was trying to use.
  (window as any).__AWE_AUTH_DEBUG__ = {
    keycloakUrl: cfg.keycloak.url,
    realm: cfg.keycloak.realm,
    clientId: cfg.keycloak.clientId,
    origin: window.location.origin,
  };

  keycloak = new Keycloak({
    url: cfg.keycloak.url,
    realm: cfg.keycloak.realm,
    clientId: cfg.keycloak.clientId,
  });

  // keycloak-js sometimes rejects init() with no argument (network error,
  // malformed token, redirect-URI mismatch, etc.). Wrap it so the catch
  // handler in main.tsx always gets a real Error with context.
  let authenticated: boolean;
  try {
    authenticated = await keycloak.init({
      onLoad: "login-required",
      checkLoginIframe: false,
      pkceMethod: "S256",
    });
  } catch (err) {
    const detail =
      err instanceof Error
        ? err.message
        : typeof err === "string"
        ? err
        : "keycloak-js init() rejected with no detail";
    throw new Error(
      `Keycloak init failed (url=${cfg.keycloak.url}, realm=${cfg.keycloak.realm}, clientId=${cfg.keycloak.clientId}): ${detail}`
    );
  }

  if (!authenticated) {
    // keycloak.init with login-required normally redirects; if we got here
    // without being authenticated, fail loud.
    throw new Error("Keycloak login did not complete (user not authenticated after init)");
  }

  const parsed = keycloak.tokenParsed || {};
  const realmRoles: string[] = (parsed as any).realm_access?.roles ?? [];
  const clientRoles: string[] =
    (parsed as any).resource_access?.[cfg.keycloak.clientId]?.roles ?? [];
  const roles = Array.from(new Set([...realmRoles, ...clientRoles]));

  currentUser = {
    sub: (parsed as any).sub ?? "",
    email: (parsed as any).email,
    name: (parsed as any).name ?? (parsed as any).preferred_username,
    roles,
    isAdmin: roles.includes("AWE_ADMIN"),
    isViewer: roles.includes("AWE_VIEWER") || roles.includes("AWE_ADMIN"),
    devMode: false,
  };

  // Auto-refresh the token ~30s before expiry.
  setInterval(() => {
    keycloak?.updateToken(60).catch(() => keycloak?.login());
  }, 30_000);

  return currentUser;
}

export function getCurrentUser(): CurrentUser {
  if (!currentUser) {
    throw new Error("initAuth() has not completed yet");
  }
  return currentUser;
}

export function getToken(): string {
  if (keycloak?.token) return keycloak.token;
  return DEV_TOKEN;
}

export function hasRole(role: string): boolean {
  return getCurrentUser().roles.includes(role);
}

export function logout(): void {
  if (keycloak) {
    keycloak.logout({ redirectUri: window.location.origin });
  } else {
    // Dev mode — no real session, just reload.
    window.location.reload();
  }
}
