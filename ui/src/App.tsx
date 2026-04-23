import { Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout";
import PoliciesPage from "./pages/PoliciesPage";
import PolicyEditorPage from "./pages/PolicyEditorPage";
import PolicyFormPage from "./pages/PolicyFormPage";
import SimulatePage from "./pages/SimulatePage";
import RequestsPage from "./pages/RequestsPage";
import WebhookDeliveriesPage from "./pages/WebhookDeliveriesPage";

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Navigate to="/policies" replace />} />
        <Route path="/policies" element={<PoliciesPage />} />
        <Route path="/policies/new" element={<PolicyFormPage mode="create" />} />
        <Route path="/policies/:policyKey" element={<PolicyEditorPage />} />
        <Route
          path="/policies/:policyKey/versions/new"
          element={<PolicyFormPage mode="add-version" />}
        />
        <Route
          path="/policies/:policyKey/versions/:version/edit"
          element={<PolicyFormPage mode="edit-draft" />}
        />
        <Route
          path="/policies/:policyKey/versions/:version/simulate"
          element={<SimulatePage />}
        />
        <Route path="/requests" element={<RequestsPage />} />
        <Route path="/deliveries" element={<WebhookDeliveriesPage />} />
      </Routes>
    </Layout>
  );
}
