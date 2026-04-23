# AWE Admin UI

Admin SPA for the OpenG2P Approval Workflow Engine — policy configuration,
request browser, webhook delivery ops.

Stack: Vite + React 18 + TypeScript + @tanstack/react-query.

## Branding

Colors and fonts follow `/branding/guidelines.txt`:

- Yellow `#F5BB1A` (accent/primary), Black `#061327` (text/chrome), Orange
  `#F07B1A` (medium), Magenta `#88498F` (sparingly).
- Fonts: Roboto (body), Roboto Slab (headings).

Tokens live in [`src/styles/theme.css`](src/styles/theme.css) and are consumed
via CSS variables (`--color-yellow`, `--font-heading`, etc.).

## Dev

```sh
npm install
npm run dev
```

The dev server runs at http://localhost:5173/v1/awe/admin/ and proxies
`/v1/awe/*` to the FastAPI app at http://localhost:8000.

## Build → embed in the service image

```sh
npm run build
```

Outputs to `../src/awe/admin_ui/static/`. The FastAPI app mounts that
directory at the configured `admin_ui.mount_path` (default `/v1/awe/admin`),
so the Docker image ships with the SPA bundled when the UI is built before
`docker build`.
