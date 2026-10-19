from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.output import emit_json
from fakeyou.config.settings import Settings
from fakeyou.db.voiceprint_store import VoiceprintRecord, VoiceprintRepository
from fakeyou.identity.enrollment import (
    DEFAULT_LIVENESS_GATE,
    DEFAULT_VOICEPRINT_DEPS,
    EnrollmentAccepted,
    EnrollmentRejected,
    EnrollSubject,
    LivenessGate,
    VoiceprintDeps,
    delete_voiceprint,
    enroll_voiceprint,
    issue_challenge,
    list_voiceprints,
    verify_voiceprint_clip,
)
from fakeyou.observability.journal import Journal
from fakeyou.persistence.retention import purge_voiceprints_expired


def voiceprint_challenge() -> None:
    challenge = issue_challenge()
    emit_json({"challenge": challenge.challenge, "challenge_id": challenge.challenge_id})


def voiceprint_enroll(
    subject: EnrollSubject,
    clip: list[str],
    *,
    gate: LivenessGate = DEFAULT_LIVENESS_GATE,
    settings: Settings,
    deps: VoiceprintDeps = DEFAULT_VOICEPRINT_DEPS,
) -> None:
    outcome = enroll_voiceprint(subject, clip, gate=gate, settings=settings, deps=deps)
    if isinstance(outcome, EnrollmentRejected):
        emit_json(
            {"enrolled": False, "reason": outcome.reason, "challenge_id": outcome.challenge_id}
        )
        raise SystemExit(ExitCode.INPUT_REJECTED)
    emit_json(_accepted_payload(outcome))


def voiceprint_verify(
    name: str,
    clip: str,
    *,
    settings: Settings,
    deps: VoiceprintDeps = DEFAULT_VOICEPRINT_DEPS,
) -> None:
    verdict = verify_voiceprint_clip(name, clip, settings=settings, deps=deps)
    emit_json(
        {
            "person_id": verdict.person_id,
            "outcome": verdict.outcome,
            "score": verdict.score,
            "margin": verdict.margin,
        }
    )


def voiceprint_delete(
    name: str,
    *,
    settings: Settings,
    store: VoiceprintRepository | None = None,
    journal: Journal | None = None,
) -> None:
    delete_voiceprint(name, settings=settings, store=store, journal=journal)
    emit_json({"person_id": name, "removed": True})


def voiceprint_list(
    *,
    settings: Settings,
    store: VoiceprintRepository | None = None,
) -> None:
    records = list_voiceprints(settings=settings, store=store)
    emit_json([_listed_payload(record) for record in records])


def voiceprint_purge_expired(
    *,
    settings: Settings,
    store: VoiceprintRepository | None = None,
    journal: Journal | None = None,
) -> None:
    removed = purge_voiceprints_expired(settings=settings, store=store, journal=journal)
    emit_json({"removed": removed})


def _accepted_payload(outcome: EnrollmentAccepted) -> dict[str, object]:
    payload: dict[str, object] = {
        "person_id": outcome.person_id,
        "n_utterances": outcome.n_utterances,
        "dim": outcome.dim,
        "is_negative": outcome.is_negative,
    }
    if outcome.challenge_id is not None:
        payload["challenge_id"] = outcome.challenge_id
        payload["owner_name"] = outcome.owner_name
    return payload


def _listed_payload(record: VoiceprintRecord) -> dict[str, object]:
    return {
        "person_id": record.person_id,
        "dim": record.dim,
        "n_utterances": record.n_utterances,
        "is_negative": record.is_negative,
        "enrolled_at": record.enrolled_at.isoformat(),
        "last_used_at": record.last_used_at.isoformat(),
    }
