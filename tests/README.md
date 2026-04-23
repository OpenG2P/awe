# AWE Tests

Smoke tests covering every public API surface plus a focused unit test for the
HMAC webhook signing contract.

## Layout

- `conftest.py` — shared fixtures (`client`, `admin_token`, `user_token`,
  `service_token`); puts `src/` on `sys.path` and points the engine at an
  in-memory SQLite database via `DATABASE_URL`.
- `fixtures/test-config.yaml` — test-mode `awe` config (issuer empty → unsigned
  JWTs are accepted).
- `test_health.py` — `/v1/awe/health`, `/version`, `/config`.
- `test_policies.py` — policy CRUD, list, activate, simulate, role gating.
- `test_requests_and_tasks.py` — full lifecycle: create policy → request →
  approver decisions → terminal state; cancel; reject; search.
- `test_webhook_signing.py` — HMAC signature stability + replay-safety.

## Run

```sh
pip install -e '.[test]'
pytest -v
```

Tests are hermetic — no Postgres, no Kafka, no Keycloak. Production uses the
real backends; the schema and code paths are exercised against SQLite via
the same `Base.metadata.create_all` call that runs at startup in production.
