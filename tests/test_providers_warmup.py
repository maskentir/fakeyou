import subprocess
import sys
from textwrap import dedent

CHECK = dedent(
    """
    import sys
    from pydantic import SecretStr
    from fakeyou.config.settings import OpenAIReply, Settings
    from fakeyou.orchestration.providers import create_chat_model

    beta = "openai.resources.beta.beta"
    before = beta in sys.modules
    settings = Settings(
        _env_file=None,
        reply=OpenAIReply(provider="openai", api_key=SecretStr("k"), max_retries=0),
    )
    create_chat_model(settings)
    print(f"before={before} after={beta in sys.modules}")
    """
)


def test_creating_the_chat_model_loads_the_openai_streaming_client_up_front() -> None:
    result = subprocess.run(
        [sys.executable, "-c", CHECK],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )

    assert result.stdout.strip().splitlines()[-1] == "before=False after=True"
