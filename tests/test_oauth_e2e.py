from examples.oauth_demo import run_demo


async def test_task_oauth_http_call_and_persistent_authorization_reuse(tmp_path):
    report = await run_demo(tmp_path)
    assert report["initial_status"] == "auth_required"
    assert report["after_login"] == report["after_restart"] == "ready"
    assert report["browser_logins"] == report["client_registrations"] == report["ui_events"] == 1
    assert report["result"][0]["text"] == "OAuth protected fixture document"
    assert report["repeat_result"] == report["result"]
    for path in (tmp_path / "credentials").iterdir():
        assert b"fixture-access-token" not in path.read_bytes()
