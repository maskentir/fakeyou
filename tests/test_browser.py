import asyncio
import json
import re
import shutil
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast, override

import pytest
from langchain_core.tools import Tool

import fakeyou.meet.browser as browser_module
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.meet.browser import (
    BROWSER_DEVTOOLS_ARGS,
    BROWSER_LAUNCH_ARGS,
    BROWSER_TOOL_ALLOWLIST,
    BrowserLaunch,
    apply_browser_audio_policy,
    build_mcp_server_parameters,
    open_browser_tools,
    open_direct_page,
    patch_preferences_for_3p_cookies,
    profile_is_fresh,
    profile_lock,
    read_devtools_endpoint,
    select_browser_tools,
    stable_fingerprint_seed,
    warmup_profile,
)
from fakeyou.persistence.lease import LeaseError
from tests._error_support import message_pattern
from tests._meet_browser_fakes import FakeBrowserContext, FakeWarmupContext, FakeWarmupPage


@pytest.fixture(autouse=True)
def _binary_resolution_is_stubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_module, "ensure_binary", lambda **_kwargs: "/fake/chrome")


async def test_open_browser_tools_fails_fast_when_node_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "cli.js"
    script.write_text("probe", encoding="utf-8")

    def launch(*args: Any, **_kwargs: Any) -> Any:
        del args
        pytest.fail("browser must not launch when node is missing")

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.BROWSER_NODE_MISSING)
    ) as error:
        async with open_browser_tools(mcp_script=script, output_dir=tmp_path):
            pytest.fail("missing node cannot provide browser tools")
    assert error.value.code is ErrorCode.BROWSER_NODE_MISSING


async def test_open_browser_tools_resolves_node_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "cli.js"
    script.write_text("probe", encoding="utf-8")
    calls: list[str] = []
    real_to_thread = asyncio.to_thread

    async def spy(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        calls.append(getattr(func, "__name__", "?"))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.BROWSER_NODE_MISSING)
    ) as error:
        async with open_browser_tools(mcp_script=script, output_dir=tmp_path):
            pytest.fail("missing node cannot provide browser tools")
    assert error.value.code is ErrorCode.BROWSER_NODE_MISSING
    assert "resolve_node_binary" in calls


async def test_missing_mcp_server_fails_before_browser_launch(tmp_path: Path) -> None:
    with pytest.raises(FakeYouError, match="npm ci") as error:
        async with open_browser_tools(mcp_script=tmp_path / "missing.js", output_dir=tmp_path):
            pytest.fail("An unavailable MCP server cannot provide browser tools")
    assert error.value.code is ErrorCode.BROWSER_MCP_MISSING


def test_mcp_server_parameters_carry_the_configured_output_dir(tmp_path: Path) -> None:
    output_dir = tmp_path / "mcp-out"

    params = build_mcp_server_parameters(
        script=tmp_path / "cli.js",
        output_dir=output_dir,
        port="9222",
        endpoint="/devtools",
        cwd=tmp_path,
    )

    assert params.command == "node"
    assert params.args is not None
    assert params.args[params.args.index("--output-dir") + 1] == str(output_dir)
    assert ".runtime" not in " ".join(params.args)


def test_mcp_server_parameters_jail_filesystem_and_disable_webmcp(tmp_path: Path) -> None:
    params = build_mcp_server_parameters(
        script=tmp_path / "cli.js",
        output_dir=tmp_path / "mcp-out",
        port="9222",
        endpoint="/devtools",
        cwd=tmp_path / "workspace",
    )

    assert params.args is not None
    assert "--isolated" in params.args
    assert "--no-webmcp" in params.args
    assert params.cwd == tmp_path / "workspace"


def test_mcp_server_runs_without_the_coordinate_vision_capability(tmp_path: Path) -> None:
    params = build_mcp_server_parameters(
        script=tmp_path / "cli.js",
        output_dir=tmp_path / "mcp-out",
        port="9222",
        endpoint="/devtools",
        cwd=tmp_path,
    )

    assert params.args is not None
    assert "vision" not in params.args
    assert "--caps" not in params.args


EXPECTED_BROWSER_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_navigate_back",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_press_key",
        "browser_wait_for",
        "browser_take_screenshot",
        "browser_close",
    }
)


def test_browser_tool_allowlist_blocks_code_execution_and_exfiltration() -> None:
    tools = [
        Tool(name=name, description="probe", func=lambda: "")
        for name in [
            "browser_navigate",
            "browser_navigate_back",
            "browser_snapshot",
            "browser_click",
            "browser_type",
            "browser_press_key",
            "browser_wait_for",
            "browser_take_screenshot",
            "browser_close",
            "browser_run_code_unsafe",
            "browser_evaluate",
            "browser_file_upload",
            "browser_cookie_list",
            "browser_cookie_set",
            "browser_network_requests",
            "browser_route",
            "browser_set_storage_state",
            "browser_webmcp_call",
            "browser_webmcp_list",
        ]
    ]

    selected = select_browser_tools(tools)

    assert BROWSER_TOOL_ALLOWLIST == EXPECTED_BROWSER_TOOLS
    assert {tool.name for tool in selected} == EXPECTED_BROWSER_TOOLS


def test_browser_launch_args_include_automation_hardening() -> None:
    assert "--disable-blink-features=AutomationControlled" in BROWSER_LAUNCH_ARGS
    assert "--use-fake-ui-for-media-stream" in BROWSER_LAUNCH_ARGS
    assert "--use-fake-device-for-media-stream" not in BROWSER_LAUNCH_ARGS
    assert "--autoplay-policy=no-user-gesture-required" in BROWSER_LAUNCH_ARGS
    assert "--lang=en-US" in BROWSER_LAUNCH_ARGS


@pytest.fixture
def cloak_ignore_args() -> Iterator[Any]:
    from cloakbrowser import config as cloakbrowser_config

    saved = list(cloakbrowser_config.IGNORE_DEFAULT_ARGS)
    try:
        yield cloakbrowser_config.IGNORE_DEFAULT_ARGS
    finally:
        cloakbrowser_config.IGNORE_DEFAULT_ARGS[:] = saved


def _set_worker_pulse_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_SOURCE", "meeting_audio.monitor")
    monkeypatch.setenv("PULSE_SINK", "agent_output")


def test_browser_audio_policy_unmutes_worker(
    monkeypatch: pytest.MonkeyPatch, cloak_ignore_args: list[str]
) -> None:
    _set_worker_pulse_env(monkeypatch)

    assert apply_browser_audio_policy() is True
    assert "--mute-audio" in cloak_ignore_args


def test_browser_audio_policy_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, cloak_ignore_args: list[str]
) -> None:
    _set_worker_pulse_env(monkeypatch)

    assert apply_browser_audio_policy() is True
    assert apply_browser_audio_policy() is True
    assert cloak_ignore_args.count("--mute-audio") == 1


def test_browser_audio_policy_skips_without_worker_env(
    monkeypatch: pytest.MonkeyPatch, cloak_ignore_args: list[str]
) -> None:
    monkeypatch.delenv("PULSE_SOURCE", raising=False)
    monkeypatch.delenv("PULSE_SINK", raising=False)

    assert apply_browser_audio_policy() is False
    assert "--mute-audio" not in cloak_ignore_args


def test_browser_audio_policy_skips_on_partial_worker_env(
    monkeypatch: pytest.MonkeyPatch, cloak_ignore_args: list[str]
) -> None:
    monkeypatch.setenv("PULSE_SOURCE", "meeting_audio.monitor")
    monkeypatch.setenv("PULSE_SINK", "something_else")

    assert apply_browser_audio_policy() is False
    assert "--mute-audio" not in cloak_ignore_args


def test_stable_fingerprint_seed_is_deterministic_and_bounded() -> None:
    assert stable_fingerprint_seed("twin-1") == stable_fingerprint_seed("twin-1")
    assert 10000 <= stable_fingerprint_seed("twin-1") <= 99999
    assert stable_fingerprint_seed("twin-1") != stable_fingerprint_seed("twin-2")


def test_cookie_patch_writes_google_exceptions(tmp_path: Path) -> None:
    count = patch_preferences_for_3p_cookies(str(tmp_path))

    assert count == 3
    prefs = json.loads((tmp_path / "Default" / "Preferences").read_text())
    cookies = prefs["profile"]["content_settings"]["exceptions"]["cookies"]
    assert cookies["*,https://[*.]meet.google.com"]["setting"] == 1
    assert len(cookies) == 3


def test_cookie_patch_preserves_existing_preferences(tmp_path: Path) -> None:
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text(json.dumps({"profile": {"name": "keep-me"}}))

    patch_preferences_for_3p_cookies(str(tmp_path))

    prefs = json.loads(prefs_path.read_text())
    assert prefs["profile"]["name"] == "keep-me"
    assert "cookies" in prefs["profile"]["content_settings"]["exceptions"]


def test_cookie_patch_recovers_from_corrupt_preferences(tmp_path: Path) -> None:
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text("not json{")

    count = patch_preferences_for_3p_cookies(str(tmp_path))

    assert count == 3


def test_profile_is_fresh_without_cookies_file(tmp_path: Path) -> None:
    assert profile_is_fresh(str(tmp_path)) is True

    cookies = tmp_path / "Default" / "Cookies"
    cookies.parent.mkdir(parents=True, exist_ok=True)
    cookies.write_text("used")

    assert profile_is_fresh(str(tmp_path)) is False


def test_browser_launch_hides_license_key_in_repr() -> None:
    assert "sekret-license" not in repr(BrowserLaunch(license_key="sekret-license"))


def test_launch_args_omit_remote_debugging_by_default() -> None:
    assert not any("remote-debugging" in arg for arg in BROWSER_LAUNCH_ARGS)
    assert "--remote-debugging-address=127.0.0.1" in BROWSER_DEVTOOLS_ARGS
    assert "--remote-debugging-port=0" in BROWSER_DEVTOOLS_ARGS


async def test_devtools_args_are_added_only_for_browser_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    servers: list[Any] = []

    class McpStartedError(Exception):
        pass

    async def launch(profile: str, **kwargs: Any) -> FakeBrowserContext:
        seen.append(list(kwargs["args"]))
        (Path(profile) / "DevToolsActivePort").write_text("9222\n/devtools/browser/abc\n")
        return FakeBrowserContext()

    @asynccontextmanager
    async def stdio_client(server: Any) -> AsyncIterator[Any]:
        servers.append(server)
        raise McpStartedError
        yield

    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)
    monkeypatch.setattr(browser_module, "resolve_node_binary", lambda: "/usr/bin/node")
    monkeypatch.setattr(browser_module, "stdio_client", stdio_client)
    script = tmp_path / "cli.js"
    script.write_text("")
    profile = BrowserLaunch(headless=True, profile_dir=tmp_path / "profile")

    async with open_direct_page(launch=profile):
        pass
    with pytest.raises(McpStartedError):
        async with open_browser_tools(launch=profile, mcp_script=script, output_dir=tmp_path):
            pass

    assert not any("remote-debugging" in arg for arg in seen[0])
    assert any("remote-debugging-port=0" in arg for arg in seen[1])
    [server] = servers
    assert server.command == "/usr/bin/node"
    assert "ws://127.0.0.1:9222/devtools/browser/abc" in server.args


async def test_direct_page_closes_browser_when_init_script_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = FakeBrowserContext(fail_on_init=True)

    async def launch(*args: Any, **_kwargs: Any) -> FakeBrowserContext:
        del args
        return context

    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)

    with pytest.raises(RuntimeError, match="init script"):
        async with open_direct_page(
            launch=BrowserLaunch(
                headless=True,
                license_key=None,
                fingerprint_seed=None,
                profile_dir=tmp_path,
                warmup=False,
            ),
        ):
            pytest.fail("setup should fail before yielding")

    assert context.closed == 1


async def test_direct_page_closes_browser_when_setup_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = FakeBrowserContext(cancel_on_init=True)

    async def launch(*args: Any, **_kwargs: Any) -> FakeBrowserContext:
        del args
        return context

    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)

    with pytest.raises(asyncio.CancelledError):
        async with open_direct_page(
            launch=BrowserLaunch(
                headless=True,
                license_key=None,
                fingerprint_seed=None,
                profile_dir=tmp_path,
                warmup=False,
            ),
        ):
            pytest.fail("setup should be cancelled before yielding")

    assert context.closed == 1


async def test_direct_page_closes_browser_when_warmup_new_page_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = FakeBrowserContext(fail_on_new_page=True)

    async def launch(*args: Any, **_kwargs: Any) -> FakeBrowserContext:
        del args
        return context

    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)

    with pytest.raises(RuntimeError, match="new page"):
        async with open_direct_page(
            launch=BrowserLaunch(
                headless=True,
                license_key=None,
                fingerprint_seed=None,
                profile_dir=tmp_path,
                warmup=True,
            ),
        ):
            pytest.fail("setup should fail before yielding")

    assert context.closed == 1


async def test_direct_page_captures_freshness_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = FakeBrowserContext()
    warmed = []

    async def launch(*args: Any, **_kwargs: Any) -> FakeBrowserContext:
        del args
        cookies = tmp_path / "Default" / "Cookies"
        cookies.parent.mkdir(parents=True, exist_ok=True)
        cookies.write_text("created by launch")
        return context

    async def warmup(*args: Any, **_kwargs: Any) -> int:
        del args
        warmed.append(True)
        return 1

    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)
    monkeypatch.setattr(browser_module, "warmup_profile", warmup)

    async with open_direct_page(
        launch=BrowserLaunch(
            headless=True,
            license_key=None,
            fingerprint_seed=None,
            profile_dir=tmp_path,
            warmup=True,
        ),
    ):
        pass

    assert warmed == [True]
    assert context.closed == 1


async def test_direct_page_preserves_profile_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = FakeBrowserContext()
    lock_names = ("SingletonLock", "SingletonSocket", "SingletonCookie")
    lock_paths = [tmp_path / name for name in lock_names]
    for path in lock_paths:
        path.write_text("owned by another browser")

    async def launch(*args: Any, **_kwargs: Any) -> FakeBrowserContext:
        del args
        return context

    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)

    async with open_direct_page(
        launch=BrowserLaunch(
            headless=True,
            license_key=None,
            fingerprint_seed=None,
            profile_dir=tmp_path,
            warmup=False,
        ),
    ):
        pass

    assert all(path.is_file() for path in lock_paths)
    assert context.closed == 1


async def test_direct_page_finishes_launch_before_cancellation_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = FakeBrowserContext()
    launch_started = asyncio.Event()
    release_launch = asyncio.Event()
    launch_cancelled = asyncio.Event()

    async def launch(*args: Any, **_kwargs: Any) -> FakeBrowserContext:
        del args
        launch_started.set()
        try:
            await release_launch.wait()
        except asyncio.CancelledError:
            launch_cancelled.set()
            raise
        return context

    async def consume_context() -> None:
        async with open_direct_page(
            launch=BrowserLaunch(
                headless=True,
                license_key=None,
                fingerprint_seed=None,
                profile_dir=tmp_path,
                warmup=False,
            ),
        ):
            await asyncio.Event().wait()

    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)
    running = asyncio.create_task(consume_context())
    try:
        await asyncio.wait_for(launch_started.wait(), timeout=1)
        running.cancel()
        await asyncio.sleep(0)
        release_launch.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=1)
    finally:
        release_launch.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)

    assert not launch_cancelled.is_set()
    assert context.closed == 1


async def test_warmup_profile_visits_urls_and_closes_page() -> None:
    page = FakeWarmupPage()
    context = FakeWarmupContext(page)

    warmed = await warmup_profile(cast(Any, context), ("https://a.example", "https://b.example"))

    assert warmed == 2
    assert page.visited == ["https://a.example", "https://b.example"]
    assert page.closed is True


async def test_warmup_profile_continues_past_failures() -> None:
    page = FakeWarmupPage(fail_on=("https://a.example",))
    context = FakeWarmupContext(page)

    warmed = await warmup_profile(cast(Any, context), ("https://a.example", "https://b.example"))

    assert warmed == 1
    assert page.visited == ["https://a.example", "https://b.example"]
    assert page.closed is True


async def test_devtools_endpoint_reads_port_and_path(tmp_path: Path) -> None:
    (tmp_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/abc\n")

    port, endpoint = await read_devtools_endpoint(str(tmp_path))

    assert port == "9222"
    assert endpoint == "/devtools/browser/abc"


async def test_devtools_endpoint_waits_for_file_to_appear(tmp_path: Path) -> None:
    async def create_late() -> None:
        await asyncio.sleep(0.05)
        (tmp_path / "DevToolsActivePort").write_text("9333\n/devtools/browser/late\n")

    task = asyncio.create_task(create_late())
    try:
        port, endpoint = await read_devtools_endpoint(str(tmp_path), timeout_seconds=5.0)
    finally:
        await task

    assert (port, endpoint) == ("9333", "/devtools/browser/late")


@pytest.mark.parametrize(
    "content",
    [
        "",
        "9222\n",
        "not-a-port\n/devtools/browser/abc\n",
        "99999\n/devtools/browser/abc\n",
        "9222\nmissing-leading-slash\n",
    ],
)
async def test_devtools_endpoint_rejects_invalid_content(tmp_path: Path, content: str) -> None:
    (tmp_path / "DevToolsActivePort").write_text(content)

    with pytest.raises(FakeYouError, match="DevTools endpoint is unavailable") as error:
        await read_devtools_endpoint(str(tmp_path), timeout_seconds=0.0)
    assert error.value.code is ErrorCode.BROWSER_DEVTOOLS_UNAVAILABLE


async def test_devtools_endpoint_times_out_without_file(tmp_path: Path) -> None:
    with pytest.raises(FakeYouError, match="DevTools endpoint is unavailable") as error:
        await read_devtools_endpoint(str(tmp_path), timeout_seconds=0.0)
    assert error.value.code is ErrorCode.BROWSER_DEVTOOLS_UNAVAILABLE
    assert "(FileNotFoundError)" in str(error.value)
    assert str(tmp_path) not in str(error.value)


@pytest.mark.browser
async def test_browser_tools_read_and_operate_a_real_page(tmp_path: Path) -> None:
    async with open_browser_tools(
        launch=BrowserLaunch(headless=True), output_dir=tmp_path
    ) as tools:
        indexed = {tool.name: tool for tool in tools}
        assert {"browser_navigate", "browser_snapshot", "browser_take_screenshot"} <= indexed.keys()
        assert "browser_evaluate" not in indexed
        assert "browser_run_code_unsafe" not in indexed
        result = await indexed["browser_navigate"].ainvoke(
            {
                "url": "data:text/html,"
                "<button onclick=\"this.textContent='Hand raised'\">Raise hand</button>"
            }
        )
        assert "error" not in str(result).lower()
        before = await indexed["browser_snapshot"].ainvoke({})
        button = re.search(r'button "Raise hand" \[ref=(\w+)\]', str(before))
        assert button is not None, before
        await indexed["browser_click"].ainvoke({"element": "Raise hand", "target": button.group(1)})
        snapshot = await indexed["browser_snapshot"].ainvoke({})
        assert "Hand raised" in str(snapshot)
        screenshot = await indexed["browser_take_screenshot"].ainvoke({"type": "png"})
        assert "image" in str(screenshot)


@pytest.mark.browser
async def test_browser_hardening_patches_automation_tells() -> None:
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.goto("about:blank")
        result = await page.evaluate(
            "() => JSON.stringify({webdriver: navigator.webdriver, "
            "deviceMemory: navigator.deviceMemory, "
            "chromeRuntime: !!(window.chrome && window.chrome.runtime)})"
        )
        text = str(result).replace("\\", "")
        assert '"webdriver":false' in text
        assert '"deviceMemory":8' in text
        assert '"chromeRuntime":true' in text


@pytest.mark.browser
async def test_browser_tools_keep_persistent_profile(tmp_path: Path) -> None:
    profile = tmp_path / "twin-profile"

    async with open_browser_tools(
        launch=BrowserLaunch(headless=True, profile_dir=profile), output_dir=tmp_path
    ):
        pass

    assert profile.is_dir()
    assert (profile / "Default" / "Preferences").is_file()


@pytest.mark.browser
async def test_open_direct_page_drives_humanized_page() -> None:
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.goto("about:blank")
        assert await page.evaluate("(() => 1 + 1)()") == 2


def test_errors_raised_inside_a_profile_lock_body_are_not_reported_as_profile_conflicts(
    tmp_path: Path,
) -> None:
    with (
        pytest.raises(LeaseError, match=message_pattern(ErrorCode.LEASE_LOCKED)) as caught,
        profile_lock(tmp_path / "profile"),
    ):
        raise LeaseError(ErrorCode.LEASE_LOCKED, path=tmp_path)

    assert caught.value.code is ErrorCode.LEASE_LOCKED


def test_a_held_profile_lock_is_reported_as_a_profile_in_use(tmp_path: Path) -> None:
    profile = tmp_path / "profile"

    with (
        profile_lock(profile),
        pytest.raises(
            FakeYouError, match=message_pattern(ErrorCode.BROWSER_PROFILE_IN_USE)
        ) as caught,
        profile_lock(profile),
    ):
        pass

    assert caught.value.code is ErrorCode.BROWSER_PROFILE_IN_USE
    assert str(caught.value).startswith("browser profile is in use: LeaseError: lease is locked: ")


def test_corrupt_preferences_are_backed_up_before_the_cookie_patch(tmp_path: Path) -> None:
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text("{not json")

    patch_preferences_for_3p_cookies(str(tmp_path))

    backups = list(prefs_path.parent.glob("Preferences.corrupt-*"))
    assert [backup.read_text() for backup in backups] == ["{not json"]
    exceptions = json.loads(prefs_path.read_text())["profile"]["content_settings"]["exceptions"]
    assert "cookies" in exceptions


class _BuggyWarmupPage(FakeWarmupPage):
    @override
    async def goto(self, url: str, **kwargs: Any) -> None:
        del self
        raise KeyError("navigation bug")


async def test_warmup_profile_bug_propagates() -> None:
    page = _BuggyWarmupPage()
    context = FakeWarmupContext(page)

    with pytest.raises(KeyError, match="navigation bug"):
        await warmup_profile(cast(Any, context), ("https://a.example",))

    assert page.closed is True


async def test_devtools_endpoint_unexpected_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: Path, *args: Any, **_kwargs: Any) -> str:
        del self
        del args
        raise KeyError("read bug")

    monkeypatch.setattr(Path, "read_text", boom)

    with pytest.raises(KeyError, match="read bug"):
        await read_devtools_endpoint(str(tmp_path), timeout_seconds=0.2)


async def test_browser_launch_resolves_the_binary_in_a_worker_thread_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved_off_loop: list[bool] = []

    def resolve(
        license_key: str | None = None,
        browser_version: str | None = None,
        release_channel: str | None = None,
    ) -> str:
        del license_key, browser_version, release_channel
        resolved_off_loop.append(threading.current_thread() is not threading.main_thread())
        return "/fake/chrome"

    async def launch(profile: str, **kwargs: Any) -> FakeBrowserContext:
        del profile, kwargs
        return FakeBrowserContext()

    monkeypatch.setattr(browser_module, "ensure_binary", resolve)
    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)

    async with open_direct_page(
        launch=BrowserLaunch(headless=True, profile_dir=tmp_path / "profile"),
    ):
        pass

    assert resolved_off_loop == [True]


async def test_browser_launch_passes_only_allowlisted_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[dict[str, str]] = []

    async def launch(profile: str, **kwargs: Any) -> FakeBrowserContext:
        del profile
        launched.append(kwargs["env"])
        return FakeBrowserContext()

    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)
    monkeypatch.setenv("FAKEYOU_REPLY__API_KEY", "sk-secret-reply")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-openai")
    monkeypatch.setenv("PULSE_SINK", "agent_output")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/fakeyou-runtime")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("CLOAKBROWSER_AUTO_UPDATE", "false")

    async with open_direct_page(
        launch=BrowserLaunch(headless=True, profile_dir=tmp_path / "profile"),
        environment={"PULSE_SOURCE": "agent_mic"},
    ):
        pass

    [env] = launched
    assert "FAKEYOU_REPLY__API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "sk-secret" not in "".join(env.values())
    assert env["PATH"]
    assert env["PULSE_SINK"] == "agent_output"
    assert env["PULSE_SOURCE"] == "agent_mic"
    assert env["XDG_RUNTIME_DIR"] == "/tmp/fakeyou-runtime"
    assert env["LC_ALL"] == "C.UTF-8"
    assert env["CLOAKBROWSER_AUTO_UPDATE"] == "false"
