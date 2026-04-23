import { NavLink } from "react-router-dom";
import type { ReactNode } from "react";
import "./Layout.css";

const nav = [
  { to: "/policies", label: "Policies" },
  { to: "/requests", label: "Requests" },
  { to: "/deliveries", label: "Webhook Deliveries" },
];

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <img
            src="/v1/awe/admin/openg2p-logo-horizontal.svg"
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
      </aside>
      <main className="content">{children}</main>
    </div>
  );
}
