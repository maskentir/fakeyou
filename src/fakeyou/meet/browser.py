import asyncio
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from cloakbrowser import ensure_binary, launch_persistent_context_async
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError

from fakeyou.catalog.binaries import resolve_node_binary
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.foundation.invariants import AGENT_SINK_NAME, MEETING_MONITOR_SOURCE_NAME
from fakeyou.observability.provider_faults import trim_detail
from fakeyou.persistence.files import write_atomically
from fakeyou.persistence.layout import LOCK_FILE_SUFFIX
from fakeyou.persistence.lease import LOCK_STALE_AFTER_SECONDS, LeaseError, file_lease, leased

BROWSER_LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--lang=en-US",
    "--accept-lang=en-US,en",
)

BROWSER_DEVTOOLS_ARGS = (
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=0",
)

_MUTE_AUDIO_ARG = "--mute-audio"
BROWSER_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "DBUS_SESSION_BUS_ADDRESS",
        "LD_LIBRARY_PATH",
        "FONTCONFIG_PATH",
        "FONTCONFIG_FILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
    }
)
BROWSER_ENV_PREFIXES = ("LC_", "XDG_", "PULSE_", "CLOAKBROWSER_")


def browser_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    inherited = {
        name: value
        for name, value in os.environ.items()
        if name in BROWSER_ENV_ALLOWLIST or name.startswith(BROWSER_ENV_PREFIXES)
    }
    return {**inherited, **(overrides or {})}


THIRD_PARTY_COOKIE_ALLOW_PATTERNS = (
    "https://[*.]meet.google.com",
    "https://[*.]google.com",
    "https://[*.]googleusercontent.com",
)

_CHROME_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000
_EXCEPTION_LIFETIME_US = 90 * 24 * 60 * 60 * 1_000_000

WARMUP_URLS = (
    "https://www.google.com",
    "https://www.wikipedia.org",
    "https://meet.google.com",
)

_DEVTOOLS_PORT_TIMEOUT_SECONDS = 30.0
_DEVTOOLS_PORT_POLL_SECONDS = 0.25

BROWSER_TOOL_ALLOWLIST = frozenset(
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


class ProfileInUseError(FakeYouError):
    pass


def profile_is_fresh(profile_dir: str) -> bool:
    return not (Path(profile_dir) / "Default" / "Cookies").is_file()


def profile_lock_path(profile_dir: Path) -> Path:
    resolved = profile_dir.resolve()
    return resolved.parent / f"{resolved.name}{LOCK_FILE_SUFFIX}"


def _profile_in_use(error: LeaseError) -> ProfileInUseError:
    reason = trim_detail(f"{type(error).__name__}: {error}") or type(error).__name__
    return ProfileInUseError(ErrorCode.BROWSER_PROFILE_IN_USE, reason=reason)


@contextmanager
def profile_lock(
    profile_dir: Path, *, stale_after_seconds: float = LOCK_STALE_AFTER_SECONDS
) -> Iterator[str]:
    lease = file_lease(
        profile_lock_path(profile_dir),
        purpose="meeting",
        stale_after_seconds=stale_after_seconds,
    )
    with leased(lease, _profile_in_use) as token:
        yield token


def apply_browser_audio_policy() -> bool:
    if os.environ.get("PULSE_SOURCE") != MEETING_MONITOR_SOURCE_NAME:
        return False
    if os.environ.get("PULSE_SINK") != AGENT_SINK_NAME:
        return False
    from cloakbrowser import config as cloakbrowser_config

    if _MUTE_AUDIO_ARG not in cloakbrowser_config.IGNORE_DEFAULT_ARGS:
        cloakbrowser_config.IGNORE_DEFAULT_ARGS.append(_MUTE_AUDIO_ARG)
    return True


async def warmup_profile(context: BrowserContext, urls: tuple[str, ...] = WARMUP_URLS) -> int:
    warmed = 0
    page = await context.new_page()
    try:
        for url in urls:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(1500)
            except PlaywrightError:
                continue
            warmed += 1
    finally:
        await page.close()
    return warmed


def stable_fingerprint_seed(stable_id: str) -> int:
    digest = hashlib.sha256(stable_id.encode("utf-8")).digest()
    return 10000 + (int.from_bytes(digest[:4], "big") % 90000)


def _load_preferences(prefs_path: Path) -> dict[str, Any]:
    if not prefs_path.is_file():
        return {}
    try:
        loaded = json.loads(prefs_path.read_bytes())
    except ValueError:
        loaded = None
    if isinstance(loaded, dict):
        return loaded
    prefs_path.rename(prefs_path.with_name(f"Preferences.corrupt-{time.time_ns()}"))
    return {}


def patch_preferences_for_3p_cookies(profile_dir: str) -> int:
    prefs_path = Path(profile_dir) / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs = _load_preferences(prefs_path)
    now = time.time_ns() // 1000 + _CHROME_EPOCH_OFFSET_US
    cookies_node: dict[str, Any] = (
        prefs.setdefault("profile", {})
        .setdefault("content_settings", {})
        .setdefault("exceptions", {})
        .setdefault("cookies", {})
    )
    for pattern in THIRD_PARTY_COOKIE_ALLOW_PATTERNS:
        cookies_node[f"*,{pattern}"] = {
            "setting": 1,
            "lifetime": str(_EXCEPTION_LIFETIME_US),
            "expiration": str(now + _EXCEPTION_LIFETIME_US),
            "last_modified": str(now),
        }
    write_atomically(prefs_path, json.dumps(prefs, indent=2).encode("utf-8"))
    return len(THIRD_PARTY_COOKIE_ALLOW_PATTERNS)


HARDENING_SCRIPT = """(() => {
  try {
    if (window.navigator && window.navigator.deviceMemory === undefined) {
      Object.defineProperty(window.navigator, "deviceMemory", {
        get: () => 8,
        configurable: true,
      });
    }
  } catch {}
  try {
    if (window.chrome && !window.chrome.runtime) {
      window.chrome.runtime = {};
    }
  } catch {}
  try {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl");
    const debug = gl ? gl.getExtension("WEBGL_debug_renderer_info") : null;
    const renderer = debug ? String(gl.getParameter(debug.UNMASKED_RENDERER_WEBGL)) : "";
    if (renderer.indexOf("SwiftShader") === -1) return;
    const spoofed = {
      37445: "Google Inc. (Intel)",
      37446: "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)",
    };
    for (const proto of [
      window.WebGLRenderingContext && window.WebGLRenderingContext.prototype,
      window.WebGL2RenderingContext && window.WebGL2RenderingContext.prototype,
    ]) {
      if (!proto || !proto.getParameter) continue;
      const original = proto.getParameter;
      proto.getParameter = function (param) {
        return param in spoofed ? spoofed[param] : original.call(this, param);
      };
    }
  } catch {}
})();"""


@dataclass(frozen=True, slots=True)
class BrowserLaunch:
    headless: bool = False
    license_key: str | None = field(default=None, repr=False)
    fingerprint_seed: int | None = None
    profile_dir: str | Path | None = None
    warmup: bool = False


DEFAULT_BROWSER_LAUNCH = BrowserLaunch()


@asynccontextmanager
async def _prepared_browser_context(
    *,
    launch_profile: BrowserLaunch,
    humanize: bool,
    environment: Mapping[str, str] | None = None,
    devtools: bool = False,
) -> AsyncIterator[tuple[BrowserContext, str]]:
    with ExitStack() as stack:
        if launch_profile.profile_dir is None:
            profile = stack.enter_context(TemporaryDirectory(prefix="fakeyou-browser-"))
        else:
            profile = str(launch_profile.profile_dir)
        fresh_profile = profile_is_fresh(profile)
        await asyncio.to_thread(patch_preferences_for_3p_cookies, profile)
        launch_args = list(BROWSER_LAUNCH_ARGS)
        if devtools:
            launch_args.extend(BROWSER_DEVTOOLS_ARGS)
        if launch_profile.fingerprint_seed is not None:
            launch_args.append(f"--fingerprint={launch_profile.fingerprint_seed}")
        apply_browser_audio_policy()
        await asyncio.to_thread(ensure_binary, license_key=launch_profile.license_key or None)
        launch = asyncio.create_task(
            launch_persistent_context_async(
                profile,
                headless=launch_profile.headless,
                license_key=launch_profile.license_key or None,
                locale="en-US",
                args=launch_args,
                humanize=humanize,
                env=browser_environment(environment),
            )
        )
        try:
            context: BrowserContext = await asyncio.shield(launch)
        except asyncio.CancelledError as cancelled:
            try:
                context = await launch
                await context.close()
            except Exception as error:
                raise cancelled from error
            raise
        try:
            await context.add_init_script(HARDENING_SCRIPT)
            if launch_profile.warmup and fresh_profile:
                await warmup_profile(context)
            yield context, profile
        finally:
            await context.close()


@asynccontextmanager
async def open_direct_page(
    *,
    launch: BrowserLaunch = DEFAULT_BROWSER_LAUNCH,
    environment: Mapping[str, str] | None = None,
) -> AsyncIterator[Page]:
    async with _prepared_browser_context(
        launch_profile=launch,
        humanize=True,
        environment=environment,
    ) as (context, _profile):
        yield await context.new_page()


def _parse_devtools_address(raw: str) -> tuple[str, str] | None:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    try:
        port = int(lines[0])
    except ValueError:
        return None
    if not 1 <= port <= 65535 or not lines[1].startswith("/"):
        return None
    return str(port), lines[1]


async def read_devtools_endpoint(
    profile: str, timeout_seconds: float = _DEVTOOLS_PORT_TIMEOUT_SECONDS
) -> tuple[str, str]:
    path = Path(profile) / "DevToolsActivePort"
    deadline = time.monotonic() + timeout_seconds
    detail = "DevToolsActivePort file did not appear"
    while True:
        try:
            raw = await asyncio.to_thread(path.read_text)
        except OSError as error:
            detail = f"reading DevToolsActivePort failed: ({type(error).__name__})"
        else:
            parsed = _parse_devtools_address(raw)
            if parsed is not None:
                return parsed
            detail = f"invalid DevToolsActivePort content: {raw.strip()!r}"
        if time.monotonic() >= deadline:
            raise FakeYouError(ErrorCode.BROWSER_DEVTOOLS_UNAVAILABLE, reason=detail)
        await asyncio.sleep(_DEVTOOLS_PORT_POLL_SECONDS)


def build_mcp_server_parameters(
    *, script: Path, output_dir: Path, port: str, endpoint: str, cwd: Path
) -> StdioServerParameters:
    return StdioServerParameters(
        command="node",
        args=[
            str(script),
            "--cdp-endpoint",
            f"ws://127.0.0.1:{port}{endpoint}",
            "--output-dir",
            str(output_dir),
            "--isolated",
            "--no-webmcp",
        ],
        cwd=cwd,
    )


def select_browser_tools(tools: Sequence[BaseTool]) -> list[BaseTool]:
    return [tool for tool in tools if tool.name in BROWSER_TOOL_ALLOWLIST]


@asynccontextmanager
async def open_browser_tools(
    *,
    launch: BrowserLaunch = DEFAULT_BROWSER_LAUNCH,
    mcp_script: Path = Path("node_modules/@playwright/mcp/cli.js"),
    output_dir: Path,
) -> AsyncIterator[list[BaseTool]]:
    if not await asyncio.to_thread(mcp_script.is_file):
        raise FakeYouError(ErrorCode.BROWSER_MCP_MISSING)
    node = await asyncio.to_thread(resolve_node_binary)

    script_path = await asyncio.to_thread(mcp_script.resolve)
    output_path = await asyncio.to_thread(output_dir.resolve)

    with TemporaryDirectory(prefix="fakeyou-mcp-") as workspace:
        async with _prepared_browser_context(
            launch_profile=launch,
            humanize=False,
            devtools=True,
        ) as (_context, profile):
            port, endpoint = await read_devtools_endpoint(profile)
            server = build_mcp_server_parameters(
                script=script_path,
                output_dir=output_path,
                port=port,
                endpoint=endpoint,
                cwd=Path(workspace),
            )
            server.command = node
            async with (
                stdio_client(server) as (read, write),
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session,
            ):
                await session.initialize()
                yield select_browser_tools(await load_mcp_tools(session))
