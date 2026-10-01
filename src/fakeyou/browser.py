import asyncio
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import ExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from cloakbrowser import launch_persistent_context_async
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from playwright.async_api import BrowserContext, Page

from fakeyou.invariants import AGENT_SINK_NAME, MEETING_MONITOR_SOURCE_NAME

BROWSER_LAUNCH_ARGS = (
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=0",
    "--disable-blink-features=AutomationControlled",
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--lang=en-US",
    "--accept-lang=en-US,en",
)

_MUTE_AUDIO_ARG = "--mute-audio"

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


def profile_is_fresh(profile_dir: str) -> bool:
    return not (Path(profile_dir) / "Default" / "Cookies").is_file()


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
            except Exception:
                continue
            warmed += 1
    finally:
        await page.close()
    return warmed


def stable_fingerprint_seed(stable_id: str) -> int:
    digest = hashlib.sha256(stable_id.encode("utf-8")).digest()
    return 10000 + (int.from_bytes(digest[:4], "big") % 90000)


def patch_preferences_for_3p_cookies(profile_dir: str) -> int:
    prefs_path = Path(profile_dir) / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs: dict[str, Any] = {}
    if prefs_path.is_file():
        try:
            loaded = json.loads(prefs_path.read_text())
        except (ValueError, OSError):
            loaded = {}
        if isinstance(loaded, dict):
            prefs = loaded
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
    prefs_path.write_text(json.dumps(prefs, indent=2))
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


@asynccontextmanager
async def _prepared_browser_context(
    *,
    headless: bool,
    license_key: str | None,
    fingerprint_seed: int | None,
    profile_dir: str | Path | None,
    warmup: bool,
    humanize: bool,
    environment: Mapping[str, str] | None = None,
) -> AsyncIterator[tuple[BrowserContext, str]]:
    with ExitStack() as stack:
        if profile_dir is None:
            profile = stack.enter_context(TemporaryDirectory(prefix="fakeyou-browser-"))
        else:
            profile = str(profile_dir)
        fresh_profile = profile_is_fresh(profile)
        await asyncio.to_thread(patch_preferences_for_3p_cookies, profile)
        launch_args = list(BROWSER_LAUNCH_ARGS)
        if fingerprint_seed is not None:
            launch_args.append(f"--fingerprint={fingerprint_seed}")
        apply_browser_audio_policy()
        launch = asyncio.create_task(
            launch_persistent_context_async(
                profile,
                headless=headless,
                license_key=license_key or None,
                locale="en-US",
                args=launch_args,
                humanize=humanize,
                **({"env": {**os.environ, **environment}} if environment else {}),
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
            if warmup and fresh_profile:
                await warmup_profile(context)
            yield context, profile
        finally:
            await context.close()


@asynccontextmanager
async def open_direct_page(
    *,
    headless: bool = False,
    license_key: str | None = None,
    fingerprint_seed: int | None = None,
    profile_dir: str | Path | None = None,
    warmup: bool = False,
    environment: Mapping[str, str] | None = None,
) -> AsyncIterator[Page]:
    async with _prepared_browser_context(
        headless=headless,
        license_key=license_key,
        fingerprint_seed=fingerprint_seed,
        profile_dir=profile_dir,
        warmup=warmup,
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


async def _read_devtools_endpoint(
    profile: str, timeout_seconds: float = _DEVTOOLS_PORT_TIMEOUT_SECONDS
) -> tuple[str, str]:
    path = Path(profile) / "DevToolsActivePort"
    deadline = time.monotonic() + timeout_seconds
    detail = "DevToolsActivePort file did not appear"
    while True:
        try:
            raw = await asyncio.to_thread(path.read_text)
        except Exception as error:
            detail = f"reading DevToolsActivePort failed: {error}"
        else:
            parsed = _parse_devtools_address(raw)
            if parsed is not None:
                return parsed
            detail = f"invalid DevToolsActivePort content: {raw.strip()!r}"
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Chrome DevTools endpoint is unavailable: {detail}")
        await asyncio.sleep(_DEVTOOLS_PORT_POLL_SECONDS)


@asynccontextmanager
async def open_browser_tools(
    *,
    headless: bool = False,
    license_key: str | None = None,
    fingerprint_seed: int | None = None,
    profile_dir: str | Path | None = None,
    warmup: bool = False,
    mcp_script: Path = Path("node_modules/@playwright/mcp/cli.js"),
) -> AsyncIterator[list[BaseTool]]:
    if not await asyncio.to_thread(mcp_script.is_file):
        raise FileNotFoundError("Playwright MCP is missing. Run npm ci from the project directory.")

    script_path = await asyncio.to_thread(mcp_script.resolve)
    output_path = await asyncio.to_thread(Path(".runtime/browser").resolve)

    async with _prepared_browser_context(
        headless=headless,
        license_key=license_key,
        fingerprint_seed=fingerprint_seed,
        profile_dir=profile_dir,
        warmup=warmup,
        humanize=False,
    ) as (_context, profile):
        port, endpoint = await _read_devtools_endpoint(profile)
        server = StdioServerParameters(
            command="node",
            args=[
                str(script_path),
                "--cdp-endpoint",
                f"ws://127.0.0.1:{port}{endpoint}",
                "--caps",
                "vision",
                "--output-dir",
                str(output_path),
            ],
        )
        async with stdio_client(server) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=60)
            ) as session:
                await session.initialize()
                yield await load_mcp_tools(session)
