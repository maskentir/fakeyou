import hashlib
from dataclasses import dataclass

from fakeyou.audio.playback import PlaybackEvent
from fakeyou.observability.journal import Journal
from fakeyou.persistence.record import MeetingRecord


@dataclass(slots=True)
class SpokenLog:
    journal: Journal
    record: MeetingRecord
    speaking: str | None = None

    def observe(self, event: PlaybackEvent) -> None:
        if event.cue:
            self.journal.record(
                event.kind.replace("playback_", "thinking_cue_"),
                source="playback",
                detail=event.detail,
                cue_text=event.text,
                transcript_id=event.utterance.transcript_id,
                sentence_index=event.utterance.sentence_index,
            )
            return
        utterance = event.utterance
        binding = utterance.publication
        publication = (
            {
                "publication_id": binding.publication_id,
                "publication_meeting_id": binding.meeting_id,
                "turn_id": binding.turn_id,
                "request_id": binding.request_id,
                "gather_attempt_id": binding.gather_attempt_id,
                "publication_scope": binding.scope,
                **(
                    {}
                    if binding.state is None
                    else {
                        "state_fact_id": binding.state.fact_id,
                        "state_revision": binding.state.revision,
                        "state_known": binding.state.known,
                    }
                ),
            }
            if binding is not None
            else {}
        )
        heard = {} if event.heard is None else {"heard_words": len(event.heard.split())}
        self.journal.record(
            event.kind,
            source="playback",
            ends_with_question=event.text.rstrip().endswith("?"),
            contains_question="?" in event.text,
            detail=event.detail,
            transcript_id=utterance.transcript_id,
            sentence_index=utterance.sentence_index,
            sentence_sha256=hashlib.sha256(" ".join(event.text.split()).encode()).hexdigest(),
            **publication,
            **heard,
        )
        if event.kind == "playback_started":
            self.speaking = event.text
        elif event.kind == "playback_finished":
            self.speaking = None
            self.record.add_twin_speech(event.text, causation_id=utterance.transcript_id)
        elif event.kind == "playback_dropped" and event.text == self.speaking:
            self.speaking = None
            self._record_interrupted(event)

    def _record_interrupted(self, event: PlaybackEvent) -> None:
        causation_id = event.utterance.transcript_id
        heard = event.heard
        if heard is None or heard == " ".join(event.text.split()):
            self.record.add_twin_speech(event.text, interrupted=True, causation_id=causation_id)
            return
        self.record.add_twin_speech(
            heard, interrupted=True, planned=event.text, causation_id=causation_id
        )
