"""Deployment contract tests for the P9 browser/proxy boundaries.

These read the checked-in deployment artifacts (not a running stack) so a
regression in the CSP storage origin, the trusted-proxy subnet or the local
nginx template fails in the default suite. The resolved-topology counterpart
runs in CI against ``docker compose config`` via
``scripts/assert_deployment_boundaries.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CADDYFILE = REPO_ROOT / "deploy" / "caddy" / "Caddyfile"
HYBRID_COMPOSE = REPO_ROOT / "deploy" / "compose" / "compose.hybrid-vps.yml"
LOCAL_COMPOSE = REPO_ROOT / "deploy" / "compose" / "compose.local.yml"
NGINX_TEMPLATE = REPO_ROOT / "frontend" / "nginx.conf.template"
FRONTEND_DOCKERFILE = REPO_ROOT / "frontend" / "Dockerfile"
PRODUCTION_EXAMPLE = REPO_ROOT / ".env.production.example"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
BOUNDARY_ASSERTION_SCRIPT = REPO_ROOT / "scripts" / "assert_deployment_boundaries.py"


def _load_boundary_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "assert_deployment_boundaries", BOUNDARY_ASSERTION_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The default edge subnet when EDGE_SUBNET is not overridden.
DEFAULT_EDGE_SUBNET = "172.30.0.0/24"


def _csp_line(text: str) -> str:
    for line in text.splitlines():
        if "Content-Security-Policy" in line:
            return line
    raise AssertionError("no Content-Security-Policy directive found")


def test_caddy_csp_injects_the_configured_storage_origin() -> None:
    csp = _csp_line(CADDYFILE.read_text(encoding="utf-8"))
    assert "connect-src 'self' https://api.workos.com {$STORAGE_PUBLIC_ORIGIN}" in csp
    assert "*" not in csp, "the production CSP must not contain a wildcard source"


def test_hybrid_compose_trusts_only_the_edge_subnet_for_forwarded_ips() -> None:
    compose = yaml.safe_load(HYBRID_COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]
    edge_subnet = compose["networks"]["edge"]["ipam"]["config"][0]["subnet"]
    assert edge_subnet == "${EDGE_SUBNET:-" + DEFAULT_EDGE_SUBNET + "}"

    command = services["api"]["command"]
    assert "--proxy-headers" in command
    trusted = command[command.index("--forwarded-allow-ips") + 1]
    assert trusted == edge_subnet, "the API must trust exactly the edge subnet"
    assert trusted != "*"

    assert list(services["caddy"]["networks"]) == ["edge"]
    assert set(services["api"]["networks"]) == {"edge", "backend"}
    for name in ("worker", "coordinator", "redis-broker", "redis-rate-limit"):
        assert list(services[name]["networks"]) == ["backend"]

    assert services["caddy"]["environment"]["STORAGE_PUBLIC_ORIGIN"].startswith(
        "${STORAGE_PUBLIC_ORIGIN:?"
    ), "the edge must fail fast without a configured storage origin"
    assert services["caddy"]["environment"]["STORAGE_PUBLIC_ENDPOINT_URL"].startswith(
        "${STORAGE_PUBLIC_ENDPOINT_URL:?"
    ), "the edge must receive the endpoint so CI can prove the origin matches"


def test_local_nginx_template_substitutes_only_the_storage_origin() -> None:
    csp = _csp_line(NGINX_TEMPLATE.read_text(encoding="utf-8"))
    assert "connect-src 'self' https://api.workos.com ${STORAGE_PUBLIC_ORIGIN}" in csp
    assert "*" not in csp
    assert "/etc/nginx/templates/default.conf.template" in FRONTEND_DOCKERFILE.read_text(
        encoding="utf-8"
    )

    compose = yaml.safe_load(LOCAL_COMPOSE.read_text(encoding="utf-8"))
    environment = compose["services"]["frontend"]["environment"]
    assert environment["STORAGE_PUBLIC_ORIGIN"]
    assert environment["NGINX_ENVSUBST_FILTER"] == "STORAGE_PUBLIC_ORIGIN"


def test_local_seaweedfs_matches_the_configured_browser_storage_contract() -> None:
    """The local stack runs the pinned SeaweedFS gateway, which configures CORS
    server-wide, so the browser direct upload from the frontend origin works in
    `make dev` (plan P9 item 1; SeaweedFS plan P2)."""
    compose = yaml.safe_load(LOCAL_COMPOSE.read_text(encoding="utf-8"))
    service = compose["services"]["seaweedfs"]
    assert service["image"].startswith("chrislusf/seaweedfs:4.48@sha256:"), (
        "the local storage image must be pinned by immutable digest"
    )
    # Compose and CI must run the same pinned artefact, so a digest bump in one
    # file cannot leave the other behind.
    assert service["image"] in CI_WORKFLOW.read_text(encoding="utf-8"), (
        "CI must run the same pinned SeaweedFS image as local Compose"
    )
    command = service["command"]
    assert "mini" in command and "-dir=/data" in command
    assert "-s3.allowedOrigins=${STORAGE_CORS_ALLOWED_ORIGIN:-" in command
    assert "-s3.allowedOrigins=${STORAGE_CORS_ALLOWED_ORIGIN:-*}" not in command
    environment = service["environment"]
    assert environment["AWS_ACCESS_KEY_ID"].startswith("${STORAGE_ACCESS_KEY_ID:")
    assert environment["AWS_SECRET_ACCESS_KEY"].startswith("${STORAGE_SECRET_ACCESS_KEY:")
    assert "http://localhost:9000/healthz" in service["healthcheck"]["test"]
    assert "seaweedfs_data:/data" in service["volumes"]

    # Every application process reaches the service on the compose network,
    # never the host-facing STORAGE_ENDPOINT_URL from `.env`.
    for name in ("api", "worker", "coordinator"):
        app_environment = compose["services"][name]["environment"]
        assert app_environment["STORAGE_ENDPOINT_URL"] == "http://seaweedfs:9000"
    assert ENV_EXAMPLE.read_text(encoding="utf-8").count("STORAGE_CORS_ALLOWED_ORIGIN=") >= 1


def test_production_example_documents_the_browser_storage_contract() -> None:
    example = PRODUCTION_EXAMPLE.read_text(encoding="utf-8")
    assert "STORAGE_PUBLIC_ORIGIN=https://" in example
    assert "STORAGE_PUBLIC_ENDPOINT_URL=https://" in example
    # Object-store CORS is provider-side; the example points operators at it.
    assert "AllowMethods" in example and "AllowOrigins" in example and "AllowHeaders" in example
    # The synchronous ask bound is a documented deployment setting.
    assert "AI_ASK_MAX_SYNCHRONOUS_BYTES=" in example


# --- Plan P9 item 1: the CI origin assertion must reject unsafe values ------


@pytest.mark.parametrize(
    "value",
    [
        "https://storage.example.com/path",
        "https://storage.example.com/path?signature=leak",
        "https://storage.example.com?x=1",
        "https://storage.example.com#frag",
        "https://user:pass@storage.example.com",
        "https://*.example.com",
        "https://storage.example.com/*",
        "http://storage.example.com",
        "storage.example.com",
        "https://",
    ],
)
def test_parse_public_origin_rejects_unsafe_values(value: str) -> None:
    script = _load_boundary_script()
    with pytest.raises(AssertionError):
        script.parse_public_origin(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://storage.example.com", "https://storage.example.com"),
        ("https://storage.example.com/", "https://storage.example.com"),
        ("https://storage.example.com:8443", "https://storage.example.com:8443"),
    ],
)
def test_parse_public_origin_accepts_a_bare_origin(value: str, expected: str) -> None:
    script = _load_boundary_script()
    assert script.parse_public_origin(value) == expected


def _production_document(*, origin: str, endpoint: str) -> dict[str, object]:
    subnet = "172.30.0.0/24"
    return {
        "services": {
            "caddy": {
                "networks": {"edge": None},
                "environment": {
                    "STORAGE_PUBLIC_ORIGIN": origin,
                    "STORAGE_PUBLIC_ENDPOINT_URL": endpoint,
                },
            },
            "api": {
                "networks": {"edge": None, "backend": None},
                "command": [
                    "uvicorn",
                    "app.main:app",
                    "--proxy-headers",
                    "--forwarded-allow-ips",
                    subnet,
                ],
            },
            "worker": {"networks": {"backend": None}},
            "coordinator": {"networks": {"backend": None}},
            "redis-broker": {"networks": {"backend": None}},
            "redis-rate-limit": {"networks": {"backend": None}},
        },
        "networks": {"edge": {"ipam": {"config": [{"subnet": subnet}]}}},
    }


def test_assert_production_accepts_a_matching_bare_origin() -> None:
    script = _load_boundary_script()
    script._assert_production(
        _production_document(
            origin="https://storage.example.com",
            endpoint="https://storage.example.com",
        )
    )


def test_assert_production_rejects_an_origin_that_does_not_match_the_endpoint() -> None:
    script = _load_boundary_script()
    with pytest.raises(AssertionError, match="origin of STORAGE_PUBLIC_ENDPOINT_URL"):
        script._assert_production(
            _production_document(
                origin="https://cdn.example.com",
                endpoint="https://storage.example.com",
            )
        )


def test_assert_production_rejects_a_signed_query_string_origin() -> None:
    script = _load_boundary_script()
    with pytest.raises(AssertionError):
        script._assert_production(
            _production_document(
                origin="https://storage.example.com/path?signature=leak",
                endpoint="https://storage.example.com",
            )
        )


def _local_document(
    *,
    allowed_origins: str = "http://localhost:5173",
    api_endpoint: str = "http://seaweedfs:9000",
) -> dict[str, object]:
    return {
        "services": {
            "frontend": {
                "environment": {
                    "STORAGE_PUBLIC_ORIGIN": "http://localhost:9000",
                    "NGINX_ENVSUBST_FILTER": "STORAGE_PUBLIC_ORIGIN",
                }
            },
            "api": {"environment": {"STORAGE_ENDPOINT_URL": api_endpoint}},
            "seaweedfs": {
                "command": (
                    "mini -dir=/data -s3.port=9000 "
                    f"-s3.allowedOrigins={allowed_origins} "
                    "-master.telemetry=false -admin.ui=false -webdav=false"
                )
            },
        }
    }


def test_assert_local_accepts_the_seaweedfs_storage_contract() -> None:
    script = _load_boundary_script()
    script._assert_local(_local_document())


def test_assert_local_rejects_a_wildcard_cors_origin() -> None:
    script = _load_boundary_script()
    with pytest.raises(AssertionError):
        script._assert_local(_local_document(allowed_origins="*"))


def test_assert_local_rejects_a_wildcard_entry_in_the_cors_list() -> None:
    script = _load_boundary_script()
    with pytest.raises(AssertionError):
        script._assert_local(_local_document(allowed_origins="http://localhost:5173,*"))


def test_assert_local_rejects_a_storage_endpoint_outside_the_compose_network() -> None:
    script = _load_boundary_script()
    with pytest.raises(AssertionError):
        script._assert_local(_local_document(api_endpoint="http://localhost:9000"))
