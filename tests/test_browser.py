import asyncio
import json
import re
from pathlib import Path

import pytest

import fakeyou.meet.browser as browser_module
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.meet.browser import (
    BROWSER_LAUNCH_ARGS,
    apply_browser_audio_policy,
    open_browser_tools,
    open_direct_page,
    patch_preferences_for_3p_cookies,
    profile_is_fresh,
    profile_lock,
    stable_fingerprint_seed,
    warmup_profile,
)
from fakeyou.persistence.ingestion_status import LeaseError
from tests.fakes import FakeBrowserContext, FakeWarmupContext, FakeWarmupPage


async def test_missing_mcp_server_fails_before_browser_launch(tmp_path: Path) -> None:
    with pytest.raises(FakeYouError, match="npm ci") as error:
        async with open_browser_tools(mcp_script=tmp_path / "missing.js"):
            pytest.fail("An unavailable MCP server cannot provide browser tools")
    assert error.value.code is ErrorCode.BROWSER_MCP_MISSING


def test_browser_launch_args_include_automation_hardening() -> None:
    assert "--disable-blink-features=AutomationControlled" in BROWSER_LAUNCH_ARGS
    assert "--use-fake-ui-for-media-stream" in BROWSER_LAUNCH_ARGS
    assert "--use-fake-device-for-media-stream" not in BROWSER_LAUNCH_ARGS
    assert "--autoplay-policy=no-user-gesture-required" in BROWSER_LAUNCH_ARGS
    assert "--lang=en-US" in BROWSER_LAUNCH_ARGS


@pytest.fixture
def cloak_ignore_args():
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
    monkeypatch: pytest.MonkeyPatch, cloak_ignore_args: list
) -> None:
    _set_worker_pulse_env(monkeypatch)

    assert apply_browser_audio_policy() is True
    assert "--mute-audio" in cloak_ignore_args


def test_browser_audio_policy_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, cloak_ignore_args: list
) -> None:
    _set_worker_pulse_env(monkeypatch)

    assert apply_browser_audio_policy() is True
    assert apply_browser_audio_policy() is True
    assert cloak_ignore_args.count("--mute-audio") == 1


def test_browser_audio_policy_skips_without_worker_env(
    monkeypatch: pytest.MonkeyPatch, cloak_ignore_args: list
) -> None:
    monkeypatch.delenv("PULSE_SOURCE", raising=False)
    monkeypatch.delenv("PULSE_SINK", raising=False)

    assert apply_browser_audio_policy() is False
    assert "--mute-audio" not in cloak_ignore_args


def test_browser_audio_policy_skips_on_partial_worker_env(
    monkeypatch: pytest.MonkeyPatch, cloak_ignore_args: list
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


async def test_prepared_context_closes_browser_when_init_script_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = FakeBrowserContext(fail_on_init=True)

    async def launch(*args, **kwargs):
        return context

    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)

    with pytest.raises(RuntimeError, match="init script"):
        async with browser_module._prepared_browser_context(
            headless=True,
            license_key=None,
            fingerprint_seed=None,
            profile_dir=tmp_path,
            warmup=False,
            humanize=True,
        ):
            pytest.fail("setup should fail before yielding")

    assert context.closed == 1


async def test_prepared_context_closes_browser_when_setup_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = FakeBrowserContext(cancel_on_init=True)

    async def launch(*args, **kwargs):
        return context

    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)

    with pytest.raises(asyncio.CancelledError):
        async with browser_module._prepared_browser_context(
            headless=True,
            license_key=None,
            fingerprint_seed=None,
            profile_dir=tmp_path,
            warmup=False,
            humanize=True,
        ):
            pytest.fail("setup should be cancelled before yielding")

    assert context.closed == 1


async def test_prepared_context_closes_browser_when_warmup_new_page_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = FakeBrowserContext(fail_on_new_page=True)

    async def launch(*args, **kwargs):
        return context

    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)

    with pytest.raises(RuntimeError, match="new page"):
        async with browser_module._prepared_browser_context(
            headless=True,
            license_key=None,
            fingerprint_seed=None,
            profile_dir=tmp_path,
            warmup=True,
            humanize=True,
        ):
            pytest.fail("setup should fail before yielding")

    assert context.closed == 1


async def test_prepared_context_captures_freshness_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = FakeBrowserContext()
    warmed = []

    async def launch(*args, **kwargs):
        cookies = tmp_path / "Default" / "Cookies"
        cookies.parent.mkdir(parents=True, exist_ok=True)
        cookies.write_text("created by launch")
        return context

    async def warmup(*args, **kwargs):
        warmed.append(True)
        return 1

    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)
    monkeypatch.setattr(browser_module, "warmup_profile", warmup)

    async with browser_module._prepared_browser_context(
        headless=True,
        license_key=None,
        fingerprint_seed=None,
        profile_dir=tmp_path,
        warmup=True,
        humanize=True,
    ):
        pass

    assert warmed == [True]
    assert context.closed == 1


async def test_prepared_context_preserves_profile_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = FakeBrowserContext()
    lock_names = ("SingletonLock", "SingletonSocket", "SingletonCookie")
    lock_paths = [tmp_path / name for name in lock_names]
    for path in lock_paths:
        path.write_text("owned by another browser")

    async def launch(*args, **kwargs):
        return context

    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)

    async with browser_module._prepared_browser_context(
        headless=True,
        license_key=None,
        fingerprint_seed=None,
        profile_dir=tmp_path,
        warmup=False,
        humanize=True,
    ):
        pass

    assert all(path.is_file() for path in lock_paths)
    assert context.closed == 1


async def test_prepared_context_finishes_launch_before_cancellation_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = FakeBrowserContext()
    launch_started = asyncio.Event()
    release_launch = asyncio.Event()
    launch_cancelled = asyncio.Event()

    async def launch(*args, **kwargs):
        launch_started.set()
        try:
            await release_launch.wait()
        except asyncio.CancelledError:
            launch_cancelled.set()
            raise
        return context

    async def consume_context() -> None:
        async with browser_module._prepared_browser_context(
            headless=True,
            license_key=None,
            fingerprint_seed=None,
            profile_dir=tmp_path,
            warmup=False,
            humanize=True,
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

    warmed = await warmup_profile(context, ("https://a.example", "https://b.example"))

    assert warmed == 2
    assert page.visited == ["https://a.example", "https://b.example"]
    assert page.closed is True


async def test_warmup_profile_continues_past_failures() -> None:
    page = FakeWarmupPage(fail_on=("https://a.example",))
    context = FakeWarmupContext(page)

    warmed = await warmup_profile(context, ("https://a.example", "https://b.example"))

    assert warmed == 1
    assert page.visited == ["https://a.example", "https://b.example"]
    assert page.closed is True


async def test_devtools_endpoint_reads_port_and_path(tmp_path: Path) -> None:
    (tmp_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/abc\n")

    port, endpoint = await browser_module._read_devtools_endpoint(str(tmp_path))

    assert port == "9222"
    assert endpoint == "/devtools/browser/abc"


async def test_devtools_endpoint_waits_for_file_to_appear(tmp_path: Path) -> None:
    async def create_late() -> None:
        await asyncio.sleep(0.05)
        (tmp_path / "DevToolsActivePort").write_text("9333\n/devtools/browser/late\n")

    task = asyncio.create_task(create_late())
    try:
        port, endpoint = await browser_module._read_devtools_endpoint(
            str(tmp_path), timeout_seconds=5.0
        )
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
        await browser_module._read_devtools_endpoint(str(tmp_path), timeout_seconds=0.0)
    assert error.value.code is ErrorCode.BROWSER_DEVTOOLS_UNAVAILABLE


async def test_devtools_endpoint_times_out_without_file(tmp_path: Path) -> None:
    with pytest.raises(FakeYouError, match="DevTools endpoint is unavailable") as error:
        await browser_module._read_devtools_endpoint(str(tmp_path), timeout_seconds=0.0)
    assert error.value.code is ErrorCode.BROWSER_DEVTOOLS_UNAVAILABLE
    assert "(FileNotFoundError)" in str(error.value)
    assert str(tmp_path) not in str(error.value)


@pytest.mark.browser
async def test_browser_tools_read_and_operate_a_real_page() -> None:
    async with open_browser_tools(headless=True) as tools:
        indexed = {tool.name: tool for tool in tools}
        assert {"browser_navigate", "browser_snapshot", "browser_take_screenshot"} <= indexed.keys()
        result = await indexed["browser_navigate"].ainvoke({"url": "about:blank"})
        assert "error" not in str(result).lower()
        await indexed["browser_evaluate"].ainvoke(
            {
                "function": "() => { document.body.innerHTML = '<button>Raise hand</button>'; "
                "document.querySelector('button').onclick = event => "
                "{ event.target.textContent = 'Hand raised'; }; }"
            }
        )
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
    async with open_browser_tools(headless=True) as tools:
        indexed = {tool.name: tool for tool in tools}
        await indexed["browser_navigate"].ainvoke({"url": "about:blank"})
        result = await indexed["browser_evaluate"].ainvoke(
            {
                "function": "() => JSON.stringify({webdriver: navigator.webdriver, "
                "deviceMemory: navigator.deviceMemory, "
                "chromeRuntime: !!(window.chrome && window.chrome.runtime)})"
            }
        )
        text = str(result).replace("\\", "")
        assert '"webdriver":false' in text
        assert '"deviceMemory":8' in text
        assert '"chromeRuntime":true' in text


@pytest.mark.browser
async def test_browser_tools_keep_persistent_profile(tmp_path: Path) -> None:
    profile = tmp_path / "twin-profile"

    async with open_browser_tools(headless=True, profile_dir=profile):
        pass

    assert profile.is_dir()
    assert (profile / "Default" / "Preferences").is_file()


@pytest.mark.browser
async def test_open_direct_page_drives_humanized_page() -> None:
    async with open_direct_page(headless=True) as page:
        await page.goto("about:blank")
        assert await page.evaluate("(() => 1 + 1)()") == 2


def test_errors_raised_inside_a_profile_lock_body_are_not_reported_as_profile_conflicts(
    tmp_path: Path,
) -> None:
    with pytest.raises(LeaseError) as caught, profile_lock(tmp_path / "profile"):
        raise LeaseError(ErrorCode.LEASE_LOCKED, path=tmp_path)

    assert caught.value.code is ErrorCode.LEASE_LOCKED


def test_a_held_profile_lock_is_reported_as_a_profile_in_use(tmp_path: Path) -> None:
    profile = tmp_path / "profile"

    with profile_lock(profile), pytest.raises(FakeYouError) as caught, profile_lock(profile):
        pass

    assert caught.value.code is ErrorCode.BROWSER_PROFILE_IN_USE


def test_corrupt_preferences_are_backed_up_before_the_cookie_patch(tmp_path: Path) -> None:
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text("{not json")

    patch_preferences_for_3p_cookies(str(tmp_path))

    backups = list(prefs_path.parent.glob("Preferences.corrupt-*"))
    assert [backup.read_text() for backup in backups] == ["{not json"]
    exceptions = json.loads(prefs_path.read_text())["profile"]["content_settings"]["exceptions"]
    assert "cookies" in exceptions
