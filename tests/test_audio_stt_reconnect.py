import asyncio

import pytest

from fakeyou.audio import ManagedMuseSTT, run_stt_reconnect, stt_reconnect_supported
from fakeyou.voice import create_muse_stt
from tests.fakes import FakeReconnectableSTT, LegacyRawReconnectSTT, LegacySTT


def test_pipecat_1_12_0_meta_stt_exposes_reconnect_coroutine() -> None:
    stt = create_muse_stt(api_key="test-meta-key")

    assert hasattr(stt, "_reconnect")
    assert asyncio.iscoroutinefunction(stt._reconnect)


def test_managed_muse_stt_exposes_policy_controlled_reconnect() -> None:
    stt = create_muse_stt(api_key="test-meta-key", service_class=ManagedMuseSTT)

    assert asyncio.iscoroutinefunction(stt.reconnect_when_idle)
    assert stt_reconnect_supported(stt) is True


def test_reconnect_guard_accepts_only_policy_controlled_reconnect() -> None:
    assert stt_reconnect_supported(FakeReconnectableSTT()) is True
    assert stt_reconnect_supported(LegacyRawReconnectSTT()) is False
    assert stt_reconnect_supported(LegacySTT()) is False
    assert stt_reconnect_supported(None) is False


async def test_frozen_reconnect_path_calls_fake_stt() -> None:
    stt = FakeReconnectableSTT()

    await run_stt_reconnect(stt)

    assert stt.calls == 1


async def test_frozen_reconnect_path_rejects_legacy_stt() -> None:
    with pytest.raises(AttributeError):
        await run_stt_reconnect(LegacySTT())
