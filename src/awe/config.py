"""
Configuration management.

Loads settings from a YAML config file with environment variable overrides.
Mirrors the audit-manager pattern — env vars use `__` as nested delimiter,
e.g. AWE__WEBHOOK__MAX_ATTEMPTS, AWE__KEYCLOAK__BASE_URL.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import List

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class WebhookConfig(BaseModel):
    # Per-attempt deadline for the caller's callback handler.
    timeout_seconds: int = Field(default=10, ge=1)
    # Total number of attempts before marking exhausted.
    max_attempts: int = Field(default=6, ge=1)
    # Backoff schedule (seconds) between successive attempts. Length should
    # equal `max_attempts - 1`. Defaults: 1m, 5m, 15m, 1h, 6h.
    backoff_seconds: List[int] = Field(
        default_factory=lambda: [60, 300, 900, 3600, 21600]
    )
    # How often the dispatcher worker polls the delivery queue.
    poll_interval_seconds: int = Field(default=2, ge=1)
    # Max deliveries claimed per dispatcher tick.
    batch_size: int = Field(default=20, ge=1)


class ResolverConfig(BaseModel):
    # Per-attempt deadline for HTTP-based approver resolver rules.
    http_timeout_seconds: int = Field(default=5, ge=1)


class SlaConfig(BaseModel):
    # How often the SLA monitor scans for expired stages.
    check_interval_seconds: int = Field(default=300, ge=10)


class KeycloakConfig(BaseModel):
    # Base URL of the Keycloak server (e.g. https://keycloak.example.org).
    base_url: str = ""
    # Realm under which AWE's clients and roles are provisioned. Defaults to
    # `staff` to match Registry / PBMS / other OpenG2P modules sharing a
    # commons-keycloak deployment.
    realm: str = "staff"
    # Admin client used to look up role/group members for approver resolution.
    admin_client_id: str = "awe-admin-resolver"
    admin_client_secret: str = ""
    # Issuer URL used to validate inbound bearer tokens — must equal the `iss`
    # claim on the tokens AWE receives. Keycloak stamps `iss` with the URL the
    # token was OBTAINED from, so a user who logs in via the public Keycloak
    # hostname carries the public issuer, while a token minted in-cluster
    # carries the internal one. AWE validates user tokens forwarded by the
    # registry, so this is normally the PUBLIC value.
    issuer: str = ""
    # Extra issuers ALSO accepted (e.g. the in-cluster URL when `issuer` is the
    # public one, for service-to-service callers). A token validates if its
    # `iss` matches `issuer` OR any entry here. Empty by default, so leaving it
    # unset preserves the previous single-issuer behaviour exactly.
    additional_issuers: List[str] = []
    # JWKS URL — usually `${base_url}/realms/${realm}/protocol/openid-connect/certs`.
    # This is where AWE FETCHES signing keys, an in-cluster server-to-server
    # call, so it should point at the internal Keycloak address regardless of
    # what `issuer` is — it does not need to match the token's `iss`.
    jwks_url: str = ""
    # Required audience claim. Empty disables the audience check (dev only).
    audience: str = ""
    # Whether to verify SSL certificates for all Keycloak HTTP calls (JWKS
    # fetch, admin token exchange, admin REST API). Set to False when using
    # self-signed certificates or an internal network without valid TLS.
    # Never disable in production.
    verify_ssl: bool = True


class NotifierConfig(BaseModel):
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_address: str = "no-reply@openg2p.org"
    use_tls: bool = True


class AweConfig(BaseModel):
    # Service metadata
    service_id: str = "openg2p.awe"
    api_version: str = "1.0"
    # Logical "module" this AWE deployment serves (registry, pbms, ...).
    # Embedded in webhook signatures and audit events for traceability.
    module: str = "default"

    webhook: WebhookConfig = WebhookConfig()
    resolver: ResolverConfig = ResolverConfig()
    sla: SlaConfig = SlaConfig()
    keycloak: KeycloakConfig = KeycloakConfig()
    notifier: NotifierConfig = NotifierConfig()


class Settings(BaseSettings):
    awe: AweConfig = AweConfig()

    model_config = {"env_nested_delimiter": "__"}


def _find_config_path() -> Path:
    config_path = os.environ.get("CONFIG_PATH", None)
    if config_path:
        return Path(config_path)

    cwd_config = Path.cwd() / "config" / "default.yaml"
    if cwd_config.exists():
        return cwd_config

    src_config = Path(__file__).parent.parent.parent / "config" / "default.yaml"
    if src_config.exists():
        return src_config

    raise FileNotFoundError(
        "Config file not found. Set CONFIG_PATH env var or place "
        "config/default.yaml in the working directory."
    )


@lru_cache
def get_settings() -> Settings:
    config_path = _find_config_path()
    with open(config_path) as f:
        yaml_data = yaml.safe_load(f) or {}
    return Settings(**yaml_data)
