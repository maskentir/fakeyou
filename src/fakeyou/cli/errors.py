class CliError(ValueError):
    pass


FAILURE_EXIT_CODES = {
    "infra_failure": 3,
    "browser_failure": 1,
    "audio_failure": 1,
    "conversation_failure": 1,
}
