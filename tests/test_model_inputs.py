from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from fakeyou.observability.model_inputs import digest, journal_model_inputs
from tests._persistence_fakes import RecordingJournal


def fake_model() -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter([AIMessage(content="ok"), AIMessage(content="ok")]))


async def test_every_model_call_records_a_digest_of_its_input_and_turn() -> None:
    journal = RecordingJournal()
    model = journal_model_inputs(fake_model(), journal, "reply")
    messages = [SystemMessage(content="You are the twin."), HumanMessage(content="Ayu: hi twin")]

    await model.ainvoke(
        messages, config={"metadata": {"transcript_id": "t1", "request_id": "t1:0"}}
    )

    [row] = journal.events("model_input_recorded")
    assert row["slot"] == "reply"
    assert row["transcript_id"] == "t1"
    assert row["request_id"] == "t1:0"
    assert row["system_sha256"] == digest("You are the twin.")
    assert row["messages"] == 2
    assert "Ayu: hi twin" not in str(row)


async def test_the_same_prompt_with_a_new_window_keeps_its_system_digest() -> None:
    journal = RecordingJournal()
    model = journal_model_inputs(fake_model(), journal, "judge")
    system = SystemMessage(content="Judge the latest line.")

    await model.ainvoke([system, HumanMessage(content="first")])
    await model.ainvoke([system, HumanMessage(content="second")])

    first, second = journal.events("model_input_recorded")
    assert first["system_sha256"] == second["system_sha256"]
    assert first["window_sha256"] != second["window_sha256"]
    assert "transcript_id" not in first


def test_no_journal_leaves_the_model_untouched() -> None:
    model = fake_model()

    assert journal_model_inputs(model, None, "reply").callbacks is None
