import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.memory import InMemorySaver
from PIL import Image

import fakeyou.orchestration.assembly as assembly_module
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import PerceptionSettings
from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import LangGraphResponder
from fakeyou.conversation.meeting_messages import request_messages
from fakeyou.conversation.session import SessionConfig
from fakeyou.foundation.greetings import create_greeting_middleware
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.memory.memory import create_memory_prefetch_middleware, current_speech
from fakeyou.memory.persona import PersonaProfile
from fakeyou.memory.visual_hydrate import (
    VISUAL_CLARIFY_AMBIGUOUS,
    VISUAL_CLARIFY_MISSING,
    VISUAL_HYDRATE_MAX_IMAGES,
    create_visual_hydrate_middleware,
    create_visual_resolver,
    visual_clarification,
)
from fakeyou.orchestration.assembly import AgentWiring, TurnBehavior
from fakeyou.visual.visual_caption import VisualCaptioner, frame_ref
from fakeyou.visual.visual_store import VisualFrame, VisualStore
from tests._conversation_fakes import ToolCallingFakeModel
from tests._memory_fakes import make_graphiti

AT = "2026-10-09T03:10:00+00:00"
SPOKEN_AT = "2026-10-09T03:12:00+00:00"
CAPTIONS = [
    "Slide one shows revenue growth.",
    "Slide two shows cost breakdown.",
    "Slide three shows headcount change.",
    "Slide four shows timeline shift.",
    "Slide five shows risk notes.",
]
COLORS = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (200, 200, 30), (200, 30, 200)]
SHAS = ["11" * 32, "22" * 32, "33" * 32, "44" * 32, "55" * 32]


def shot_file(directory: Path, name: str, color: tuple[int, int, int]) -> Path:
    path = directory / name
    Image.new("RGB", (64, 48), color).save(path, format="JPEG", quality=60)
    return path


def stored_frames(directory: Path, count: int) -> tuple[VisualStore, list[VisualFrame]]:
    store = VisualStore(directory / "visual")
    frames: list[VisualFrame] = []
    for index in range(count):
        path = shot_file(directory, f"shot-{index}.jpg", COLORS[index])
        frame = VisualFrame(
            frame_id=f"170000000000{index}-{SHAS[index][:8]}",
            captured_page_ms=1700000000000.0 + index * 2000.0,
            captured_at=AT,
            sha256=SHAS[index],
            path=str(path),
            thumb_path=str(directory / f"shot-{index}.thumb.jpg"),
            dhash=int.from_bytes(hashlib.sha256(f"frame-{index}".encode()).digest()[:8], "big"),
        )
        store.append(frame)
        frames.append(frame)
    assert len(store.frames) == count
    return store, frames


def frame_bytes64(frame: VisualFrame) -> str:
    return base64.b64encode(Path(frame.path).read_bytes()).decode("ascii")


def captioner_for(texts: list[str]) -> tuple[VisualCaptioner, ToolCallingFakeModel]:
    model = ToolCallingFakeModel(responses=[AIMessage(content=text) for text in texts], seen=[])
    return VisualCaptioner(model, settings=PerceptionSettings(enabled=True)), model


def participant(text: str, message_id: str, refs: tuple[str, ...] = ()) -> HumanMessage:
    event = TranscriptEvent(
        id=message_id,
        timestamp=SPOKEN_AT,
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id="unknown",
        speaker_evidence_id=None,
        visual_refs=refs,
    )
    (message,) = request_messages([event])
    return message


async def quarterly_caption(ref: str) -> str:
    return f"[visual {ref}: quarterly numbers.]"


def speech_event(message_id: str, text: str, refs: tuple[str, ...]) -> TranscriptEvent:
    return TranscriptEvent(
        id=message_id,
        timestamp=SPOKEN_AT,
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id="unknown",
        speaker_evidence_id=None,
        visual_refs=refs,
    )


class RecordingSaver(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.dumps: list[str] = []

    @override
    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self.dumps.append(json.dumps(checkpoint, default=str))
        return super().put(config, checkpoint, metadata, new_versions)

    @override
    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self.dumps.append(json.dumps(checkpoint, default=str))
        return await super().aput(config, checkpoint, metadata, new_versions)


def model_calls(model: ToolCallingFakeModel) -> list[list[Any]]:
    assert model.seen[::2] == model.seen[1::2]
    return model.seen[::2]


def image_blocks(messages: list[Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, HumanMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, dict) and block.get("type") == "image":
                    found.append(block)
    return found


def state_images(messages: list[Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, dict) and block.get("type") in ("image", "image_url"):
                    found.append(block)
    return found


def stack(
    store: VisualStore,
    captioner: VisualCaptioner,
    replies: list[AIMessage],
    *,
    saver: Any = None,
    visual: Any = None,
) -> tuple[Any, ToolCallingFakeModel]:
    model = ToolCallingFakeModel(responses=cast(list[Any], replies), seen=[])
    agent = create_meeting_agent(
        model,
        checkpointer=saver or InMemorySaver(),
        persona="PERSONA",
        middleware=[
            create_memory_prefetch_middleware(
                make_graphiti(), group_id="team_alpha", visual_caption=visual
            ),
            create_visual_hydrate_middleware(store, captioner),
        ],
    )
    return agent, model


async def turn(agent: Any, message: HumanMessage, thread: str = "visual-thread") -> Any:
    return await agent.ainvoke(
        {"messages": [message]}, config={"configurable": {"thread_id": thread}}
    )


async def test_state_has_refs_only(tmp_path: Path) -> None:
    store, frames = stored_frames(tmp_path, 3)
    captioner, visual = captioner_for(CAPTIONS[:2])
    first, second, third = frames
    assert await captioner.caption_frame(first) is not None
    assert await captioner.caption_frame(second) is not None
    assert len(model_calls(visual)) == 2
    blobs = [frame_bytes64(frame) for frame in frames]
    assert len(set(blobs)) == 3
    echo = AIMessage(
        content=[
            {"type": "text", "text": "Revenue grew."},
            {"type": "image", "base64": blobs[0], "mime_type": "image/jpeg"},
        ]
    )
    saver = RecordingSaver()
    agent, model = stack(
        store,
        captioner,
        [echo, AIMessage(content="Costs stayed flat.")],
        saver=saver,
        visual=quarterly_caption,
    )
    config = {"configurable": {"thread_id": "refs-only"}}
    await agent.ainvoke(
        {"messages": [participant("What shows revenue?", "s1", (first.frame_id, second.frame_id))]},
        config=config,
    )
    await agent.ainvoke(
        {"messages": [participant("What shows costs?", "s2", (third.frame_id,))]},
        config=config,
    )
    state = await agent.aget_state(config)
    messages = state.values["messages"]
    assert all(
        isinstance(message.content, str)
        for message in messages
        if isinstance(message, HumanMessage)
    )
    assert state_images(messages) == []
    [earlier] = [message for message in messages if message.id == "s1"]
    assert earlier.additional_kwargs["meeting_event"]["visual_refs"] == []
    assert CAPTIONS[0] in earlier.text
    assert CAPTIONS[1] in earlier.text
    assert f"frame:{SHAS[0][:8]}@{AT}" in earlier.text
    [later] = [message for message in messages if message.id == "s2"]
    assert later.additional_kwargs["meeting_event"]["visual_refs"] == [third.frame_id]
    assert saver.dumps != []
    blob = "\n".join(saver.dumps)
    for payload in blobs:
        assert payload not in blob
    assert "quarterly numbers" not in blob
    assert third.frame_id in blob
    seen = [block for call in model_calls(model) for block in image_blocks(call)]
    assert {block["base64"] for block in seen} == set(blobs)
    assert len(model_calls(visual)) == 2


async def test_hydrate_adds_max_two_frames(tmp_path: Path) -> None:
    store, frames = stored_frames(tmp_path, 5)
    captioner, visual = captioner_for([])
    blobs = [frame_bytes64(frame) for frame in frames]
    assert len(set(blobs)) == 5
    agent, model = stack(
        store,
        captioner,
        [AIMessage(content="Here is the summary.")],
        visual=quarterly_caption,
    )
    refs = tuple(frame.frame_id for frame in frames)
    await turn(agent, participant("Summarize all slides.", "m1", refs))
    [call] = model_calls(model)
    hydrated_list = [
        message
        for message in call
        if isinstance(message, HumanMessage) and isinstance(message.content, list)
    ]
    assert len(hydrated_list) == 1
    hydrated = hydrated_list[0]
    blocks = hydrated.content
    assert isinstance(blocks, list)
    images = [block for block in blocks if isinstance(block, dict) and block.get("type") == "image"]
    texts = [block for block in blocks if isinstance(block, dict) and block.get("type") == "text"]
    assert len(images) == VISUAL_HYDRATE_MAX_IMAGES == 2
    assert [block["base64"] for block in images] == blobs[3:]
    assert all(block["mime_type"] == "image/jpeg" for block in images)
    assert len(texts) == 1
    assert blocks.index(images[0]) < blocks.index(texts[0])
    assert blocks.index(images[1]) < blocks.index(texts[0])
    for index in range(5):
        assert f"[visual frame:{SHAS[index][:8]}@{AT}]" in texts[0]["text"]
    assert "Summarize all slides." in texts[0]["text"]
    assert visual.seen == []
    evidence = hydrated.additional_kwargs["visual_evidence"]
    assert evidence["version"] == "v1"
    assert evidence["images"] == 2
    assert evidence["visual_ids"] == [frames[3].frame_id, frames[4].frame_id]
    assert hydrated.id == "m1"
    visual_marks = [
        message
        for message in call
        if isinstance(message, HumanMessage)
        and (message.additional_kwargs.get("meeting_event") or {}).get("kind") == "visual"
    ]
    assert len(visual_marks) == 1


async def test_greeting_survives_multimodal() -> None:
    model = ToolCallingFakeModel(responses=[AIMessage(content="from model")], seen=[])
    agent = create_meeting_agent(
        model,
        middleware=[create_greeting_middleware("en")],
        checkpointer=InMemorySaver(),
    )
    message = HumanMessage(
        content=[
            {"type": "image", "base64": "aGVsbG8=", "mime_type": "image/jpeg"},
            {"type": "text", "text": "Hi"},
        ],
        id="greet-mm-1",
        additional_kwargs={
            "meeting_event": {
                "timestamp": SPOKEN_AT,
                "source": "meeting_audio",
                "participant_id": "unknown",
                "speaker_evidence_id": None,
            }
        },
    )
    result = await agent.ainvoke(
        {"messages": [message]}, config={"configurable": {"thread_id": "greeting-mm"}}
    )
    assert result["messages"][-1].content == "Hello."
    assert model.seen == []


async def test_prefetch_appends_latest_caption() -> None:
    seen_refs: list[str] = []

    async def resolver(ref: str) -> str:
        seen_refs.append(ref)
        return f"[visual {ref}: A quarterly chart.]"

    model = ToolCallingFakeModel(
        responses=[AIMessage(content="Noted."), AIMessage(content="Again.")], seen=[]
    )
    agent = create_meeting_agent(
        model,
        checkpointer=InMemorySaver(),
        persona="PERSONA",
        middleware=[
            create_memory_prefetch_middleware(
                make_graphiti(), group_id="team_alpha", visual_caption=resolver
            )
        ],
    )
    await turn(
        agent, participant("What shows revenue?", "c1", ("frame-a", "frame-b")), "caption-turn"
    )
    [call] = model_calls(model)
    index = next(i for i, message in enumerate(call) if message.id == "c1")
    visual = call[index + 1]
    assert isinstance(visual, HumanMessage)
    assert visual.additional_kwargs["meeting_event"]["kind"] == "visual"
    assert visual.content == "[visual frame-b: A quarterly chart.]"
    assert seen_refs == ["frame-b"]
    captioned = list(call)
    captioned.insert(
        index + 1,
        HumanMessage(
            content="[visual frame-b: A quarterly chart.]",
            id="c1-visual",
            additional_kwargs={"meeting_event": {"kind": "visual"}},
        ),
    )
    end, speech = current_speech(captioned)
    assert [message.id for message in speech] == ["c1"]
    assert end == index + 1
    await turn(agent, participant("Anything else?", "c2"), "caption-empty")
    untouched = model_calls(model)[1]
    assert seen_refs == ["frame-b"]
    assert all(
        (message.additional_kwargs.get("meeting_event") or {}).get("kind") != "visual"
        for message in untouched
        if isinstance(message, HumanMessage)
    )

    async def failing(_ref: str) -> str:
        raise RuntimeError("visual unavailable")

    broken = ToolCallingFakeModel(responses=[AIMessage(content="Unknown.")], seen=[])
    guarded = create_meeting_agent(
        broken,
        checkpointer=InMemorySaver(),
        persona="PERSONA",
        middleware=[
            create_memory_prefetch_middleware(
                make_graphiti(), group_id="team_alpha", visual_caption=failing
            )
        ],
    )
    result = await turn(
        guarded, participant("What shows revenue?", "c3", ("frame-a",)), "caption-guarded"
    )
    assert result["messages"][-1].content == "Unknown."
    [guarded_call] = model_calls(broken)
    assert all(
        (message.additional_kwargs.get("meeting_event") or {}).get("kind") != "visual"
        for message in guarded_call
        if isinstance(message, HumanMessage)
    )


async def test_failing_visual_caption_is_recorded_as_fault_and_journal() -> None:
    from fakeyou.observability.diagnostics import Diagnostics
    from fakeyou.observability.faults import FaultReporter
    from tests._persistence_fakes import RecordingJournal

    async def failing(_ref: str) -> str:
        raise RuntimeError("visual unavailable")

    journal = RecordingJournal()
    reporter = FaultReporter(journal=journal)
    model = ToolCallingFakeModel(responses=[AIMessage(content="Unknown.")], seen=[])
    guarded = create_meeting_agent(
        model,
        checkpointer=InMemorySaver(),
        persona="PERSONA",
        middleware=[
            create_memory_prefetch_middleware(
                make_graphiti(),
                group_id="team_alpha",
                diag=Diagnostics(faults=reporter, journal=journal),
                visual_caption=failing,
            )
        ],
    )
    result = await turn(
        guarded, participant("What shows revenue?", "c3", ("frame-a",)), "caption-traced"
    )

    assert result["messages"][-1].content == "Unknown."
    [event] = journal.events("visual_caption_failed")
    assert event["source"] == "memory"
    assert event["stage"] == "caption"
    assert event["error_type"] == "RuntimeError"
    assert reporter.total() >= 1


async def test_streaming_unchanged(tmp_path: Path) -> None:
    store, frames = stored_frames(tmp_path, 2)
    captioner, visual = captioner_for(["Frame caption."])
    assert await captioner.caption_frame(frames[0]) is not None
    assert await captioner.caption_frame(frames[1]) is not None
    assert len(model_calls(visual)) == 2
    blobs = [frame_bytes64(frame) for frame in frames]
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(content="Revenue grew strongly. Costs stayed flat."),
            AIMessage(content="Costs stayed flat. Nothing else."),
        ],
        seen=[],
    )
    agent = create_meeting_agent(
        model,
        checkpointer=InMemorySaver(),
        persona="PERSONA",
        middleware=[
            create_memory_prefetch_middleware(
                make_graphiti(),
                group_id="team_alpha",
                visual_caption=create_visual_resolver(store, captioner),
            ),
            create_visual_hydrate_middleware(store, captioner),
        ],
    )
    responder = LangGraphResponder(agent, "visual-stream", language="en", max_sentences=4)
    first = speech_event("st1", "What shows revenue?", tuple(frame.frame_id for frame in frames))
    sentences = [
        sentence
        async for sentence in responder.stream_sentences(
            [first], lambda: None, request_id="stream-visual-1"
        )
    ]
    assert sentences == ["Revenue grew strongly.", "Costs stayed flat."]
    assert len(model_calls(visual)) == 2
    assert {block["base64"] for entry in model.seen for block in image_blocks(entry)} == set(blobs)
    streamed = len(model.seen)
    second = speech_event("st2", "What shows costs?", (frames[0].frame_id,))
    followup = [
        sentence
        async for sentence in responder.stream_sentences(
            [second], lambda: None, request_id="stream-visual-2"
        )
    ]
    assert followup == ["Costs stayed flat.", "Nothing else."]
    assert len(model_calls(visual)) == 2
    restreamed = model.seen[streamed:]
    assert restreamed != []
    assert {block["base64"] for entry in restreamed for block in image_blocks(entry)} == {blobs[0]}


async def test_resolver_skips_network_on_cache_miss(tmp_path: Path) -> None:
    store, frames = stored_frames(tmp_path, 1)
    captioner, visual = captioner_for(["Seed caption."])
    resolve = create_visual_resolver(store, captioner)
    [frame] = frames

    line = await resolve(frame.frame_id)

    assert line == f"[visual {frame_ref(frame)}]"
    assert visual.seen == []


async def test_resolver_serves_cached_caption_without_new_call(tmp_path: Path) -> None:
    store, frames = stored_frames(tmp_path, 1)
    captioner, visual = captioner_for(["Seed caption."])
    [frame] = frames
    assert await captioner.caption_frame(frame) is not None
    assert len(model_calls(visual)) == 1
    resolve = create_visual_resolver(store, captioner)

    line = await resolve(frame.frame_id)

    assert line == f"[visual {frame_ref(frame)}: Seed caption.]"
    assert len(model_calls(visual)) == 1


async def test_trim_skips_network_on_cache_miss(tmp_path: Path) -> None:
    store, frames = stored_frames(tmp_path, 2)
    captioner, visual = captioner_for(["Seed caption."])
    middleware = create_visual_hydrate_middleware(store, captioner)
    first, second = frames

    update = await middleware.abefore_model(
        {
            "messages": [
                participant("What shows revenue?", "t1", (first.frame_id,)),
                participant("What shows costs?", "t2", (second.frame_id,)),
            ]
        },
        cast(Any, None),
    )

    assert update is not None
    [trimmed] = update["messages"]
    assert trimmed.id == "t1"
    assert f"[visual {frame_ref(first)}]" in trimmed.text
    assert "Seed caption." not in trimmed.text
    assert visual.seen == []


def test_visual_clarification_asks_single_neutral_question() -> None:
    assert visual_clarification(1) is None
    for count in (0, 2, 5):
        reply = visual_clarification(count)
        assert reply is not None
        assert reply.count("?") == 1
        assert "frame:" not in reply
    assert visual_clarification(0) == VISUAL_CLARIFY_MISSING
    assert visual_clarification(2) == VISUAL_CLARIFY_AMBIGUOUS
    assert VISUAL_CLARIFY_MISSING != VISUAL_CLARIFY_AMBIGUOUS


def test_after_model_drops_echoed_images(tmp_path: Path) -> None:
    store, frames = stored_frames(tmp_path, 1)
    captioner, _ = captioner_for([])
    middleware = create_visual_hydrate_middleware(store, captioner)
    [frame] = frames
    update = middleware.after_model(
        {
            "messages": [
                AIMessage(
                    content=[
                        {"type": "text", "text": "Revenue grew."},
                        {
                            "type": "image",
                            "base64": frame_bytes64(frame),
                            "mime_type": "image/jpeg",
                        },
                    ],
                    id="echo-1",
                )
            ]
        },
        cast(Any, None),
    )
    assert update is not None
    [cleaned] = update["messages"]
    assert cleaned.id == "echo-1"
    assert cleaned.content == [{"type": "text", "text": "Revenue grew."}]
    assert (
        middleware.after_model({"messages": [AIMessage(content="Plain.")]}, cast(Any, None)) is None
    )


async def test_assembly_registers_hydrate_after_prefetch(
    settings: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: dict[str, Any] = {}

    def fake_agent(
        _model: Any, *, tools: Any = (), _checkpointer: Any = None, **kwargs: Any
    ) -> Any:
        del tools
        created["middleware"] = [type(item).__name__ for item in kwargs["middleware"]]
        return SimpleNamespace(name="agent")

    monkeypatch.setattr(assembly_module, "create_meeting_agent", fake_agent)
    recall = settings.memory.recall.model_copy(update={"strategy": "prefetch"})
    perception = settings.perception.model_copy(update={"enabled": True})
    wired = settings.model_copy(
        update={
            "memory": settings.memory.model_copy(update={"recall": recall}),
            "perception": perception,
        }
    )
    profile = PersonaProfile(
        owner_id=settings.identity.owner_id,
        twin_id=settings.identity.twin_id,
        display_name="Visual Twin",
        role="Project coordinator",
        allowed_tools=cast(Any, ["recall_meeting_context"]),
    )
    config = SessionConfig(
        owner_id=profile.owner_id,
        twin_id=profile.twin_id,
        meeting_id=SessionId("visual-wiring"),
        link="https://meet.google.com/abc-defg-hij",
        twin_name="Visual Twin",
    )
    store = VisualStore(tmp_path / "visual")
    captioner = VisualCaptioner(ToolCallingFakeModel(responses=[], seen=[]))
    async with assembly_module.open_conversation(
        wired,
        config,
        agent=AgentWiring(
            profile=profile,
            graphiti=make_graphiti(),
            visual_store=store,
            visual_captioner=captioner,
        ),
        behavior=TurnBehavior(on_transition=lambda *_args: None),
    ):
        pass
    names = created["middleware"]
    assert "VisualHydrateMiddleware" in names
    assert names.index("VisualHydrateMiddleware") > names.index("_MemoryPrefetchMiddleware")
    async with assembly_module.open_conversation(
        wired,
        config,
        agent=AgentWiring(profile=profile, graphiti=make_graphiti()),
        behavior=TurnBehavior(on_transition=lambda *_args: None),
    ):
        pass
    assert "VisualHydrateMiddleware" not in created["middleware"]
