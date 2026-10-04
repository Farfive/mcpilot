from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import time
from pathlib import Path

import httpx2
import jwt
import mcp_types as types
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from oauth_fixture import OAuthFixture

from mcpilot import Policy
from mcpilot.catalog import Catalog
from mcpilot.discovery import CALL_TOOL, FIND_TOOLS
from mcpilot.integrations import filesystem
from mcpilot.models import AuthSpec, Integration, ToolRule
from mcpilot.remote import (
    JWTTokenVerifier,
    RemoteGateway,
    StaticTokenVerifier,
    Tenant,
    trusted_header_identity,
)

WORKSPACE = Path(__file__).parent.parent / "examples" / "workspace"
TOKENS = {
    "token-alice": {"sub": "alice", "tenant": "acme"},
    "token-bob": {"sub": "bob", "tenant": "acme"},
    "token-carol": {"sub": "carol", "tenant": "globex"},
    "token-dave": {"sub": "dave", "tenant": "unknown"},
}


def files_tenant(capabilities=("files.read",)) -> Tenant:
    integration = filesystem(WORKSPACE)
    return Tenant(Catalog([integration]), Policy([integration], capabilities=capabilities))


@contextlib.asynccontextmanager
async def running(make_gateway):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    gateway = make_gateway(url)
    server = uvicorn.Server(uvicorn.Config(gateway.app(), log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield gateway, url
    finally:
        server.should_exit = True
        await task
        await gateway.close()
        sock.close()


@contextlib.asynccontextmanager
async def mcp_client(url, token, **kwargs):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http:
        async with Client(streamable_http_client(f"{url}/mcp", http_client=http), **kwargs) as client:
            yield client


async def find(client, task="Przeczytaj lokalny plik README", **extra):
    return (await client.call_tool(FIND_TOOLS, {"task": task, **extra})).structured_content


async def test_unauthenticated_requests_get_protected_resource_challenge(tmp_path):
    def make(url):
        return RemoteGateway(tenants={"acme": files_tenant()}, token_verifier=StaticTokenVerifier(TOKENS),
                             public_url=url, issuer_url="https://idp.example.com", state_dir=tmp_path)

    async with running(make) as (_, url), httpx2.AsyncClient() as http:
        denied = await http.post(f"{url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                 headers={"Accept": "application/json, text/event-stream"})
        assert denied.status_code == 401
        assert "resource_metadata" in denied.headers["www-authenticate"]
        wrong = await http.post(f"{url}/mcp", json={}, headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        metadata = (await http.get(f"{url}/.well-known/oauth-protected-resource/mcp")).json()
        assert metadata["resource"] == f"{url}/mcp"
        assert metadata["authorization_servers"] == ["https://idp.example.com"]
        assert (await http.get(f"{url}/healthz")).json()["status"] == "ok"


async def test_users_are_isolated_and_tenants_have_their_own_policy(tmp_path):
    def make(url):
        return RemoteGateway(
            tenants={"acme": files_tenant(), "globex": files_tenant(capabilities=())},
            token_verifier=StaticTokenVerifier(TOKENS), public_url=url,
            issuer_url="https://idp.example.com", state_dir=tmp_path)

    async with running(make) as (gateway, url):
        async with mcp_client(url, "token-alice") as alice, mcp_client(url, "token-bob") as bob:
            found = await find(alice)
            reader = next(t for t in found["tools"] if t["name"] == "read_file")
            result = await alice.call_tool(CALL_TOOL, {"tool_id": reader["id"], "arguments": {"path": "README.md"}})
            assert "Project Atlas" in json.dumps(result.structured_content)
            stolen = await bob.call_tool(CALL_TOOL, {"tool_id": reader["id"], "arguments": {"path": "README.md"}})
            assert stolen.structured_content["error"] == "PolicyDenied"
            own = next(t for t in (await find(bob))["tools"] if t["name"] == "read_file")
            assert own["id"] != reader["id"]
        async with mcp_client(url, "token-carol") as carol:
            assert (await find(carol))["tools"] == []
        async with mcp_client(url, "token-dave") as dave:
            assert (await find(dave))["error"] == "PolicyDenied"
        assert gateway.active_users() == 3


async def test_rate_limit_and_idle_sessions_are_closed(tmp_path):
    def make(url):
        return RemoteGateway(tenants={"acme": files_tenant()}, token_verifier=StaticTokenVerifier(TOKENS),
                             public_url=url, issuer_url="https://idp.example.com", state_dir=tmp_path,
                             calls_per_minute=2, idle_timeout=0.2)

    async with running(make) as (gateway, url):
        async with mcp_client(url, "token-alice") as alice:
            assert (await find(alice))["tools"]
            assert (await find(alice))["tools"]
            assert (await find(alice))["error"] == "RateLimited"
        assert gateway.active_users() == 1
        for _ in range(100):
            if gateway.active_users() == 0:
                break
            await asyncio.sleep(0.05)
        assert gateway.active_users() == 0


def oauth_tenant(fixture: OAuthFixture) -> Tenant:
    integration = Integration(
        id="fixture/docs", service="docs", publisher="fixture", version="1", transport="http",
        endpoint=fixture.endpoint, support="supported", capabilities=("documents.search",),
        tools={"documents_search": ToolRule(capability="documents.search")},
        auth=AuthSpec(mode="oauth", scopes_by_capability={"documents.search": ("documents.read",)}),
    )
    return Tenant(Catalog([integration]),
                  Policy([integration], capabilities=["documents.search"], allow_loopback=True))


def oauth_gateway(fixture, tmp_path, **kwargs):
    def make(url):
        return RemoteGateway(tenants={"acme": oauth_tenant(fixture)}, token_verifier=StaticTokenVerifier(TOKENS),
                             public_url=url, issuer_url="https://idp.example.com", state_dir=tmp_path,
                             browser_identity=trusted_header_identity("X-Test-User"), **kwargs)
    return make


async def test_url_elicitation_login_completes_and_resumes_the_tool_call(tmp_path):
    with OAuthFixture().serve() as fixture:
        async with running(oauth_gateway(fixture, tmp_path)) as (_, url):
            prompts, browsers = [], []

            async def browser(link: str) -> None:
                async with httpx2.AsyncClient(follow_redirects=False, headers={"X-Test-User": "alice"}) as tab:
                    to_provider = await tab.get(link)
                    assert to_provider.status_code == 302
                    callback = await fixture.approve(to_provider.headers["location"])
                    assert callback.startswith(f"{url}/oauth/callback?")
                    done = await tab.get(callback)
                    assert done.status_code == 200, done.text

            async def on_elicit(context, params):
                prompts.append(params)
                browsers.append(asyncio.create_task(browser(params.url)))  # user opens the link
                return types.ElicitResult(action="accept")

            async with mcp_client(url, "token-alice", elicitation_callback=on_elicit) as alice:
                found = await find(alice, "Szukaj dokumentów", services=["docs"])
                await asyncio.gather(*browsers)
                assert len(prompts) == 1 and prompts[0].mode == "url"
                assert prompts[0].url.startswith(f"{url}/connect/")
                assert "code_challenge" not in prompts[0].url
                assert [t["name"] for t in found["tools"]] == ["documents_search"]
                assert found["needs_user"] == []
                assert "/connect/" not in json.dumps(found) and "authorize" not in json.dumps(found)
                result = await alice.call_tool(CALL_TOOL, {"tool_id": found["tools"][0]["id"],
                                                           "arguments": {"query": "x"}})
                assert result.structured_content["content"][0]["text"] == "OAuth protected fixture document"
            assert fixture.authorization_count == 1


async def test_forwarded_login_link_cannot_attach_another_browser(tmp_path):
    with OAuthFixture().serve() as fixture:
        async with running(oauth_gateway(fixture, tmp_path, login_wait=0.5)) as (_, url):
            outcomes = []

            async def mallory(link: str) -> None:
                async with httpx2.AsyncClient(follow_redirects=False, headers={"X-Test-User": "mallory"}) as tab:
                    outcomes.append((await tab.get(link)).status_code)
                    outcomes.append((await tab.get(f"{url}/oauth/callback?code=x&state=y")).status_code)
                async with httpx2.AsyncClient(follow_redirects=False) as anonymous:
                    outcomes.append((await anonymous.get(link)).status_code)

            async def on_elicit(context, params):
                await mallory(params.url)
                return types.ElicitResult(action="accept")

            async with mcp_client(url, "token-alice", elicitation_callback=on_elicit) as alice:
                found = await find(alice, "Szukaj dokumentów", services=["docs"])
            assert outcomes == [403, 400, 403]
            assert found["tools"] == []
            assert found["needs_user"][0]["action"] == "finish_login"
            assert fixture.authorization_count == 0


async def test_clients_without_url_elicitation_get_a_status_and_static_connect_page(tmp_path):
    with OAuthFixture().serve() as fixture:
        async with running(oauth_gateway(fixture, tmp_path)) as (_, url):
            async with mcp_client(url, "token-alice") as alice:
                found = await find(alice, "Szukaj dokumentów", services=["docs"])
            assert found["needs_user"][0]["action"] == "finish_login"
            assert found["connect_page"] == f"{url}/connect"
            assert "/connect/" not in json.dumps(found)
            async with httpx2.AsyncClient() as http:
                listing = await http.get(f"{url}/connect", headers={"X-Test-User": "alice"})
                assert "fixture/docs" in listing.text
                assert "fixture/docs" not in (await http.get(f"{url}/connect", headers={"X-Test-User": "bob"})).text
                assert (await http.get(f"{url}/connect")).status_code == 401


def test_jwt_verifier_checks_signature_issuer_audience_and_expiry():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    verifier = JWTTokenVerifier(issuer="https://idp.example.com", audience="https://gw.example.com/mcp",
                                jwks={"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]})
    now = int(time.time())
    base = {"iss": "https://idp.example.com", "aud": "https://gw.example.com/mcp", "sub": "alice",
            "exp": now + 300, "scope": "mcpilot", "tenant": "acme"}

    def sign(claims, kid="k1", signer=key):
        return jwt.encode(claims, signer, algorithm="RS256", headers={"kid": kid})

    token = asyncio.run(verifier.verify_token(sign(base)))
    assert (token.subject, token.scopes, token.claims["tenant"]) == ("alice", ["mcpilot"], "acme")
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for bad in (sign({**base, "aud": "https://other/mcp"}), sign({**base, "iss": "https://evil"}),
                sign({**base, "exp": now - 600}), sign({k: v for k, v in base.items() if k != "sub"}),
                sign(base, kid="unknown"), sign(base, signer=other), "not-a-jwt"):
        assert asyncio.run(verifier.verify_token(bad)) is None
    with pytest.raises(ValueError):
        JWTTokenVerifier(issuer="i", audience="a")


def test_public_url_must_be_https_origin(tmp_path):
    for bad in ("http://gateway.example.com", "https://gateway.example.com/mcp", "ftp://x"):
        with pytest.raises(ValueError):
            RemoteGateway(tenants={"acme": files_tenant()}, token_verifier=StaticTokenVerifier(TOKENS),
                          public_url=bad, issuer_url="https://idp.example.com", state_dir=tmp_path)


async def test_legacy_clients_get_url_elicitation_required_error_then_retry(tmp_path):
    from mcp import UrlElicitationRequiredError
    from mcp.shared.exceptions import MCPError

    with OAuthFixture().serve() as fixture:
        async with running(oauth_gateway(fixture, tmp_path)) as (_, url):
            async def on_elicit(context, params):  # declares URL capability; legacy servers may also ask inline
                return types.ElicitResult(action="decline")

            async with mcp_client(url, "token-alice", elicitation_callback=on_elicit, mode="legacy") as alice:
                assert alice.protocol_version < "2026-07-28"
                with pytest.raises(MCPError) as raised:
                    await find(alice, "Szukaj dokumentów", services=["docs"])
                error = UrlElicitationRequiredError.from_error(raised.value.error)
                link = error.elicitations[0]
                assert link.url.startswith(f"{url}/connect/") and link.elicitation_id
                async with httpx2.AsyncClient(follow_redirects=False, headers={"X-Test-User": "alice"}) as tab:
                    callback = await fixture.approve((await tab.get(link.url)).headers["location"])
                    assert (await tab.get(callback)).status_code == 200
                for _ in range(50):
                    found = await find(alice, "Szukaj dokumentów", services=["docs"])
                    if found["tools"]:
                        break
                    await asyncio.sleep(0.05)
                assert [t["name"] for t in found["tools"]] == ["documents_search"]


async def test_remote_config_builds_jwt_protected_gateway_end_to_end(tmp_path):
    from cryptography.fernet import Fernet

    from mcpilot.gateway import build_remote_gateway

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), "kid": "k1", "alg": "RS256"}
    (tmp_path / "acme.json").write_text(json.dumps([filesystem(WORKSPACE).model_dump(mode="json")]))
    environ = {"MCPILOT_SECRET_KEY": Fernet.generate_key().decode()}

    def config(url):
        return {"transport": "http", "public_url": url, "state_dir": "state", "audit_log": "audit.jsonl",
                "auth": {"issuer": "https://idp.example.com", "audience": f"{url}/mcp", "jwks": {"keys": [jwk]},
                         "tenant_claim": "org"},
                "tenants": {"acme": {"manifest": "acme.json", "capabilities": ["files.read"]}}}

    with pytest.raises(ValueError, match="encryption key"):
        build_remote_gateway(config("http://127.0.0.1:1"), tmp_path, environ={})
    with pytest.raises(ValueError, match="Unknown"):
        build_remote_gateway({**config("http://127.0.0.1:1"), "command": "rm"}, tmp_path, environ)

    async with running(lambda url: build_remote_gateway(config(url), tmp_path, environ)) as (gateway, url):
        token = jwt.encode({"iss": "https://idp.example.com", "aud": f"{url}/mcp", "sub": "alice", "org": "acme",
                            "exp": int(time.time()) + 300}, key, algorithm="RS256", headers={"kid": "k1"})
        async with mcp_client(url, token) as alice:
            reader = next(t for t in (await find(alice))["tools"] if t["name"] == "read_file")
            result = await alice.call_tool(CALL_TOOL, {"tool_id": reader["id"], "arguments": {"path": "README.md"}})
            assert "Project Atlas" in json.dumps(result.structured_content)
        assert gateway.state_dir == tmp_path / "state"
        events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
        assert {(e["tenant"], e["user_id"]) for e in events} == {("acme", "https://idp.example.com|alice")}
        assert any(e["action"] == "call" and e["outcome"] == "ok" and e["duration_ms"] is not None for e in events)
        foreign = jwt.encode({"iss": "https://idp.example.com", "aud": "https://other/mcp", "sub": "eve",
                              "org": "acme", "exp": int(time.time()) + 300}, key, algorithm="RS256",
                             headers={"kid": "k1"})
        async with httpx2.AsyncClient() as http:
            denied = await http.post(f"{url}/mcp", json={}, headers={"Authorization": f"Bearer {foreign}"})
            assert denied.status_code == 401


async def test_remote_gateway_demo_report(tmp_path):
    from examples.remote_gateway_demo import run_demo

    report = await run_demo(str(tmp_path))
    assert report["elicitation"]["mode"] == "url"
    assert report["forwarded_to_bob"] == 403 and report["alice_browser"] == 200
    assert report["alice_tools"] == ["documents_search"]
    assert report["alice_result"] == "OAuth protected fixture document"
    assert report["model_saw_login_url"] is False
    assert report["bob_needs_own_login"] == "finish_login"
    assert (report["active_users"], report["provider_logins"]) == (2, 1)


def test_principal_namespaces_and_user_claim_for_proxy_identity():
    from mcp.server.auth.provider import AccessToken

    from mcpilot.remote import default_principal

    token = AccessToken(token="t", client_id="c", scopes=[], subject="123",
                        claims={"iss": "https://idp", "email": "a@example.com", "tenant": "acme"})
    principal = default_principal(token)
    assert (principal.user_id, principal.tenant) == ("https://idp|123", "acme")
    assert default_principal(token, user_claim="email").user_id == "a@example.com"
    with pytest.raises(PermissionError):
        default_principal(token, user_claim="upn")
