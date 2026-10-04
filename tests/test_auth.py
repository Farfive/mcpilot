from __future__ import annotations

import json
import logging
import os
import time

import httpx2
import pytest
from cryptography.fernet import Fernet
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from oauth_fixture import OAuthFixture

from mcpilot.auth import (
    AuthFailure,
    AuthManager,
    AuthRequired,
    CredentialKey,
    EncryptedFileSecretStore,
    MemorySecretStore,
    OAuthConfig,
)
from mcpilot.models import AuthSpec, Integration


def integration(mode="oauth", endpoint=OAuthFixture.endpoint):
    return Integration(
        id="fixture/documents", service="documents", publisher="fixture", version="1.0.0",
        transport="http", endpoint=endpoint, capabilities=("documents.search",),
        auth=AuthSpec(mode=mode, scopes_by_capability={"documents.search": ("documents.read",)}),
    )


def config(server, **kwargs):
    return OAuthConfig(redirect_handler=server.redirect, callback_handler=server.callback, **kwargs)


async def request(handle, server):
    async with httpx2.AsyncClient(auth=handle.http_auth, transport=httpx2.MockTransport(server)) as client:
        return await client.get(server.endpoint, headers={"MCP-Protocol-Version": "2026-07-28"})


async def test_oauth_pkce_discovery_minimal_scope_and_reuse():
    server = OAuthFixture()
    store = MemorySecretStore()
    manager = AuthManager(store, config(server))
    handle = await manager.prepare("alice", integration(), "work", ("documents.search",))
    assert (await request(handle, server)).status_code == 200
    assert server.authorization_count == server.token_count == server.registration_count == 1
    assert server.registration_scope == "documents.read"
    assert "fixture-access-token" not in repr(handle)
    assert "fixture-access-token" not in json.dumps(handle.public_state())

    # Fresh manager, no browser callbacks: persisted registration and grant suffice.
    restarted = AuthManager(store, OAuthConfig())
    second = await restarted.prepare("alice", integration(), "work", ("documents.search",))
    assert second.status == "ready"
    assert (await request(second, server)).status_code == 200
    assert server.authorization_count == server.registration_count == 1


async def test_refresh_after_restart_preserves_expiry_and_discovered_token_endpoint():
    server = OAuthFixture()
    store = MemorySecretStore()
    manager = AuthManager(store, config(server))
    handle = await manager.prepare("alice", integration(), "work", ("documents.search",))
    await request(handle, server)
    key = CredentialKey("alice", integration().id, "work", server.endpoint)
    record = await store.get(key)
    record["expires_at"] = time.time() - 10
    await store.set(key, record)
    restarted = AuthManager(store, OAuthConfig())
    second = await restarted.prepare("alice", integration(), "work", ("documents.search",))
    assert (await request(second, server)).status_code == 200
    assert server.refresh_count == 1
    assert server.authorization_count == 1
    after = await store.get(key)
    assert after["tokens"]["refresh_token"] == "fixture-refresh-token"
    assert after["tokens"]["scope"] == "documents.read"
    assert after["expires_at"] > time.time()


@pytest.mark.parametrize("mismatch", ["fail_state", "fail_issuer"])
async def test_oauth_rejects_state_and_issuer_mismatch(mismatch, caplog):
    server = OAuthFixture()
    setattr(server, mismatch, True)
    manager = AuthManager(oauth_config=config(server))
    handle = await manager.prepare("alice", integration(), "work", ("documents.search",))
    with caplog.at_level(logging.DEBUG, logger="mcp.client.auth.oauth2"):
        with pytest.raises(AuthFailure) as caught:
            await request(handle, server)
    assert server.token_count == 0
    assert "wrong-state" not in str(caught.value)
    assert "wrong-state" not in caplog.text
    assert "wrong-issuer" not in caplog.text
    assert server.authorization["state"][0] not in caplog.text


async def test_oauth_error_bodies_are_not_logged_or_exposed(caplog):
    server = OAuthFixture()
    server.token_error = "SUPER_SECRET_BODY_FROM_PROVIDER"
    manager = AuthManager(oauth_config=config(server))
    handle = await manager.prepare("alice", integration(), "work", ("documents.search",))
    with caplog.at_level(logging.DEBUG, logger="mcp.client.auth.oauth2"):
        with pytest.raises(AuthFailure) as caught:
            await request(handle, server)
    assert server.token_error not in caplog.text + str(caught.value) + repr(handle)


async def test_explicit_scope_escalation_is_blocked_before_browser():
    server = OAuthFixture()
    server.challenge_scope = "documents.read documents.write"
    manager = AuthManager(oauth_config=config(server))
    handle = await manager.prepare("alice", integration(), "work", ("documents.search",))
    with pytest.raises(AuthRequired):
        await request(handle, server)
    assert server.authorization_count == 0


async def test_missing_granted_scope_cannot_be_reported_as_ready():
    server = OAuthFixture()
    server.granted_scope = "different.permission"
    manager = AuthManager(oauth_config=config(server))
    handle = await manager.prepare("alice", integration(), "work", ("documents.search",))
    with pytest.raises(AuthRequired):
        await request(handle, server)


async def test_bearer_isolation_endpoint_user_account_and_revocation():
    manager = AuthManager()
    target = integration("bearer")
    await manager.set_secret("alice", target.id, "work", "secret-fixture", endpoint=target.endpoint)
    connected = await manager.prepare("alice", target, "work", ("documents.search",))
    assert connected.headers == {"Authorization": "Bearer secret-fixture"}
    assert "secret-fixture" not in repr(connected)
    for user, account, endpoint in [
        ("bob", "work", target.endpoint), ("alice", "personal", target.endpoint),
        ("alice", "work", "https://other.fixture.test/mcp"),
    ]:
        other = await manager.prepare(user, integration("bearer", endpoint), account, ("documents.search",))
        assert other.status == "auth_required"
        assert other.connection_id != connected.connection_id
    revoked = []

    async def revoker(secret):
        revoked.append(secret)

    await manager.revoke("alice", target.id, "work", endpoint=target.endpoint, provider_revoker=revoker)
    assert revoked == ["secret-fixture"]
    assert (await manager.prepare("alice", target, "work", ())).status == "auth_required"


async def test_encrypted_store_persists_without_plaintext_and_uses_private_files(tmp_path):
    encryption_key = Fernet.generate_key()
    key = CredentialKey("alice", "fixture/documents", "work", OAuthFixture.endpoint)
    directory = tmp_path / "credentials"
    store = EncryptedFileSecretStore(directory, encryption_key)
    assert not directory.exists()
    await store.set(key, {"secret": "ENCRYPT_ME"})
    contents = list(directory.iterdir())
    assert len(contents) == 1
    assert b"ENCRYPT_ME" not in contents[0].read_bytes()
    assert os.stat(contents[0]).st_mode & 0o777 == 0o600
    reopened = EncryptedFileSecretStore(directory, encryption_key)
    assert await reopened.get(key) == {"secret": "ENCRYPT_ME"}
    with pytest.raises(AuthFailure, match="could not be decrypted"):
        await EncryptedFileSecretStore(directory, Fernet.generate_key()).get(key)
    await reopened.delete(key)
    assert await store.get(key) is None


async def test_missing_ui_and_preregistered_client_setup():
    target = integration()
    assert (await AuthManager().prepare("alice", target, "work", ())).status == "auth_required"
    server = OAuthFixture()
    manager = AuthManager(oauth_config=config(server, client_id="public-client"))
    assert (await manager.prepare("alice", target, "work", ())).status == "setup_required"


async def test_host_receives_connection_specific_authorization_request():
    server = OAuthFixture()
    events = []

    async def ui(event):
        events.append(event)
        await server.redirect(event.url)

    manager = AuthManager(oauth_config=OAuthConfig(authorization_handler=ui, callback_handler=server.callback))
    handle = await manager.prepare("alice", integration(), "work", ("documents.search",))
    await request(handle, server)
    assert events[0].connection_id == handle.connection_id
    assert events[0].account == "work"
    assert "code_challenge" not in repr(events[0])


async def test_real_loopback_http_oauth_then_mcp_tool_call_and_reconnect():
    """E2E with real TCP and SDK, fixture identity provider, no vendor account."""
    with OAuthFixture().serve() as server:
        store = MemorySecretStore()
        manager = AuthManager(store, config(server))
        target = integration(endpoint=server.endpoint)
        handle = await manager.prepare("alice", target, "work", ("documents.search",))
        async with httpx2.AsyncClient(auth=handle.http_auth) as http:
            async with Client(streamable_http_client(server.endpoint, http_client=http)) as client:
                tools = await client.list_tools()
                assert tools.tools[0].name == "documents_search"
                result = await client.call_tool("documents_search", {"query": "project"})
                assert result.content[0].text == "OAuth protected fixture document"
        assert server.authorization_count == server.token_count == 1
        restarted = AuthManager(store, OAuthConfig())
        reused = await restarted.prepare("alice", target, "work", ("documents.search",))
        async with httpx2.AsyncClient(auth=reused.http_auth) as http:
            async with Client(streamable_http_client(server.endpoint, http_client=http)) as client:
                assert (await client.list_tools()).tools[0].name == "documents_search"
        assert server.authorization_count == 1
