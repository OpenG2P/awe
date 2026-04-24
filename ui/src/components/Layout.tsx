import { NavLink } from "react-router-dom";
import type { ReactNode } from "react";
import { getCurrentUser, logout } from "../auth";
import "./Layout.css";

const nav = [
  { to: "/policies", label: "Policies" },
  { to: "/requests", label: "Requests" },
  { to: "/deliveries", label: "Webhook Deliveries" },
  { to: "/audit", label: "Audit Log" },
];

export default function Layout({ children }: { children: ReactNode }) {
  const user = getCurrentUser();
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <img
            src="/openg2p-logo-horizontal.svg"
            alt="OpenG2P"
            className="brand-logo"
          />
          <span className="brand-title">Approval Workflow Engine</span>
        </div>
        <nav>
          {nav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                isActive ? "nav-item active" : "nav-item"
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="user-info">
            <div className="user-name">{user.name || user.email || user.sub}</div>
            <div className="user-roles">
              {user.isAdmin ? "AWE_ADMIN" : user.isViewer ? "AWE_VIEWER" : "—"}
              {user.devMode && <span className="dev-badge"> dev</span>}
            </div>
          </div>
          <button className="logout-btn" onClick={logout}>
            Sign out
          </button>
        </div>
      </aside>
      <main className="content">{children}</main>
    </div>
  );
}
