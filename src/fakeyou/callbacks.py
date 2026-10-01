from fakeyou.faults import Fault
from fakeyou.journal import ProviderJournal


def record_consumer_fault(
    reporter: object, detail: str, error: BaseException, *, journal: ProviderJournal | None = None
) -> None:
    fault = Fault(source="stt", kind="callback", detail=f"{detail}: {type(error).__name__}")
    record = getattr(reporter, "record", None)
    if record is not None:
        try:
            record(fault)
        except Exception:
            pass
    journal = journal or getattr(reporter, "journal", None) or ProviderJournal("")
    journal.record("callback_fault", detail=detail, error=type(error).__name__)
