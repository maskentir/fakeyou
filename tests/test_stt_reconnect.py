import asyncio

import pytest

from fakeyou.audio import ManagedMuseSTT, run_stt_reconnect, stt_reconnect_supported
from fakeyou.voice import create_muse_stt


class _ReconnectableSTT:
    def __init__(self) -> None:
        self.calls = 0

    async def reconnect_when_idle(self) -> None:
        self.calls += 1


class _LegacyRawReconnectSTT:
    async def _reconnect(self) -> None:
        pass


class _LegacySTT:
    pass


def test_pipecat_1_12_0_meta_stt_exposes_reconnect_coroutine() -> None:
    stt = create_muse_stt(api_key="test-meta-key")

    assert hasattr(stt, "_reconnect")
    assert asyncio.iscoroutinefunction(stt._reconnect)


def test_managed_muse_stt_exposes_policy_controlled_reconnect() -> None:
    stt = create_muse_stt(api_key="test-meta-key", service_class=ManagedMuseSTT)

    assert asyncio.iscoroutinefunction(stt.reconnect_when_idle)
    assert stt_reconnect_supported(stt) is True


def test_reconnect_guard_accepts_only_policy_controlled_reconnect() -> None:
    assert stt_reconnect_supported(_ReconnectableSTT()) is True
    assert stt_reconnect_supported(_LegacyRawReconnectSTT()) is False
    assert stt_reconnect_supported(_LegacySTT()) is False
    assert stt_reconnect_supported(None) is False


async def test_frozen_reconnect_path_calls_fake_stt() -> None:
    stt = _ReconnectableSTT()

    await run_stt_reconnect(stt)

    assert stt.calls == 1


async def test_frozen_reconnect_path_rejects_legacy_stt() -> None:
    with pytest.raises(AttributeError):
        await run_stt_reconnect(_LegacySTT())
