# OpenG2P Approval Workflow Engine (AWE)

Generic, configurable multi-stage approval workflow service for OpenG2P modules
(Registry, PBMS, etc.). One AWE deployment per module — caller services post
artifacts for approval, AWE resolves stages and approvers, then notifies the
caller via signed webhook callbacks when state changes.

All documentation — design, API, schema, deployment, operational runbook —
lives at:

**→ https://docs.openg2p.org/platform/platform-services/approval-workflow-engine**

Please update only the GitBook documentation; this README is intentionally
a thin pointer and should not duplicate or diverge from the docs site.

---

## License

SPDX-License-Identifier: MPL-2.0

Part of the [OpenG2P](https://www.openg2p.org/) platform.
