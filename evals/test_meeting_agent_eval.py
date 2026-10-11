from deepeval.evaluate import assert_test
from deepeval.metrics import ToolCorrectnessMetric
from deepeval.metrics.base_metric import BaseMetric
from deepeval.test_case import LLMTestCase, ToolCall, ToolCallParams
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.conversation.agent import create_meeting_agent
from tests.fakes import OfflineJudge, ToolCallingFakeModel


def test_meeting_agent_tool_trajectory_is_correct() -> None:
    @tool(description="Raise the digital twin's hand in the meeting.")
    def raise_hand(reason: str) -> str:
        return f"raised hand: {reason}"

    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "raise_hand",
                        "args": {"reason": "I have a question"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="I raised my hand."),
        ]
    )
    graph = create_meeting_agent(
        model,
        tools=[raise_hand],
        checkpointer=InMemorySaver(),
    )
    result = graph.invoke(
        {"messages": [{"role": "user", "content": "Raise your hand because you have a question"}]},
        {"configurable": {"thread_id": "eval-thread"}},
    )
    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    tool_calls = [
        message
        for message in result["messages"]
        if isinstance(message, AIMessage) and message.tool_calls
    ]
    case = LLMTestCase(
        input="Raise your hand because you have a question",
        actual_output=result["messages"][-1].content,
        tools_called=[
            ToolCall(
                name=call["name"],
                input_parameters=call["args"],
                output=tool_messages[index].content,
            )
            for index, call in enumerate(tool_calls[0].tool_calls)
        ],
        expected_tools=[
            ToolCall(name="raise_hand", input_parameters={"reason": "I have a question"})
        ],
    )

    metric: BaseMetric = ToolCorrectnessMetric(
        threshold=1.0,
        strict_mode=True,
        model=OfflineJudge(),
        async_mode=False,
        include_reason=False,
        evaluation_params=[ToolCallParams.INPUT_PARAMETERS],
    )
    assert_test(test_case=case, metrics=[metric], run_async=False)
