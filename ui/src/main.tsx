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
    // Very rare — happens only if /config.json is unreachable or the browser
    // blocked the Keycloak redirect. Show a minimal error page.
    console.error("Auth init failed:", err);
    document.getElementById("root")!.innerHTML =
      '<div style="padding: 32px; font-family: sans-serif;">' +
      "<h1>Could not start the admin portal</h1>" +
      `<p>${String(err)}</p>` +
      "</div>";
  });
