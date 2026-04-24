import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { initAuth } from "./auth";
import "./styles/theme.css";

const queryClient = new QueryClient();

// Initialise Keycloak (or dev-mode fallback) BEFORE rendering the app — so
// every component can assume the user is authenticated and roles are known.
initAuth()
  .then(() => {
    ReactDOM.createRoot(document.getElementById("root")!).render(
      <React.StrictMode>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter basename="/">
            <App />
          </BrowserRouter>
        </QueryClientProvider>
      </React.StrictMode>
    );
  })
  .catch((err) => {
    // keycloak-js will often reject init() with no argument — we deliberately
    // surface as much as possible so operators can figure out what's wrong
    // without having to open the browser dev console.
    console.error("Auth init failed:", err);
    const message =
      err instanceof Error && err.message
        ? err.message
        : typeof err === "string" && err
        ? err
        : "(keycloak-js rejected init() without a reason — see browser console + Keycloak server logs)";
    const stack = err instanceof Error && err.stack ? err.stack : "";
    const cfg = (window as any).__AWE_AUTH_DEBUG__ ?? {};
    document.getElementById("root")!.innerHTML =
      '<div style="padding: 32px; font-family: Roboto, sans-serif; max-width: 800px;">' +
      "<h1>Could not start the admin portal</h1>" +
      `<p><strong>${escapeHtml(message)}</strong></p>` +
      "<h3>Config loaded from <code>/config.json</code></h3>" +
      `<pre style="background:#f6f6f6;padding:12px;border-radius:4px;overflow:auto;">${escapeHtml(JSON.stringify(cfg, null, 2))}</pre>` +
      (stack
        ? `<details><summary>Stack</summary><pre style="background:#f6f6f6;padding:12px;border-radius:4px;overflow:auto;font-size:11px;">${escapeHtml(stack)}</pre></details>`
        : "") +
      '<h3>What to check</h3>' +
      '<ul>' +
      '<li>Browser Network tab — did <code>/config.json</code> load with the expected Keycloak URL?</li>' +
      '<li>Can you reach <code>{keycloak.url}/realms/{realm}/.well-known/openid-configuration</code> directly?</li>' +
      '<li>Does the <code>awe-admin-portal</code> client in Keycloak have a Valid Redirect URI matching <code>' +
      escapeHtml(window.location.origin) +
      '/*</code>?</li>' +
      '<li>Are AWE UI and Keycloak on the same protocol (both HTTPS)? Mixed content breaks silently.</li>' +
      '<li>CORS — is Web Origins <code>+</code> or this host on the Keycloak client?</li>' +
      '</ul>' +
      "</div>";
  });

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
