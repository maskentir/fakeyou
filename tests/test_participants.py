from fakeyou.foundation.participants import (
    Participant,
    SpeakerSample,
    SpeechWindow,
    attribute_speaker,
)


def roster(*parts):
    return {p.id: p for p in parts}


def test_stable_single_speaker_observed():
    people = roster(Participant("a", "Budi", "e1", False))
    samples = [SpeakerSample(t, "e1", ("a",)) for t in range(0, 1001, 250)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "observed"
    assert result.participant_id == "a"
    assert result.display_name == "Budi"
    assert result.reason == "observed"
    assert result.coverage >= 0.95


def test_overlap_preserves_unknown():
    people = roster(
        Participant("a", "Budi", "e1", False),
        Participant("b", "Rani", "e1", False),
    )
    samples = [SpeakerSample(t, "e1", ("a", "b")) for t in range(0, 1001, 250)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "unknown"
    assert result.participant_id is None
    assert result.reason == "overlap"


def test_speaker_switch_before_final_stays_unknown():
    people = roster(
        Participant("a", "Budi", "e1", False),
        Participant("b", "Rani", "e1", False),
    )
    samples = [SpeakerSample(t, "e1", ("a",)) for t in (0, 250, 500)]
    samples += [SpeakerSample(t, "e1", ("b",)) for t in (750, 1000)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "unknown"
    assert result.participant_id is None
    assert result.reason == "speaker_switch"


def test_self_only_stays_unknown():
    people = roster(Participant("self", "Rani Twin", "e1", True))
    samples = [SpeakerSample(t, "e1", ("self",)) for t in range(0, 1001, 250)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "unknown"
    assert result.participant_id is None
    assert result.reason == "self"


def test_stale_gap_stays_unknown():
    people = roster(Participant("a", "Budi", "e1", False))
    samples = [SpeakerSample(0, "e1", ("a",)), SpeakerSample(1000, "e1", ("a",))]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "unknown"
    assert result.reason == "stale_gap"


def test_duplicate_names_are_not_merged():
    people = roster(
        Participant("a", "Sam", "e1", False),
        Participant("b", "Sam", "e1", False),
    )
    samples = [SpeakerSample(t, "e1", ("b",)) for t in range(0, 1001, 250)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "observed"
    assert result.participant_id == "b"


def test_missing_roster_id_stays_unknown():
    people = roster(Participant("a", "Budi", "e1", False))
    samples = [SpeakerSample(t, "e1", ("gone",)) for t in range(0, 1001, 250)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "unknown"
    assert result.participant_id is None
    assert result.reason == "left_roster"


def test_epoch_mismatch_stays_unknown():
    people = roster(Participant("a", "Budi", "e2", False))
    samples = [SpeakerSample(t, "e1", ("a",)) for t in range(0, 1001, 250)]
    result = attribute_speaker(SpeechWindow("t1", "e2", 0, 1000), samples, people)
    assert result.status == "unknown"
    assert result.reason == "no_samples"


def test_bad_clock_stays_unknown():
    people = roster(Participant("a", "Budi", "e1", False))
    samples = [SpeakerSample(t, "e1", ("a",)) for t in range(0, 1001, 250)]
    reversed_window = attribute_speaker(SpeechWindow("t1", "e1", 1000, 0), samples, people)
    assert reversed_window.status == "unknown"
    assert reversed_window.reason == "bad_clock"
    nan_window = attribute_speaker(SpeechWindow("t1", "e1", float("nan"), 1000), samples, people)
    assert nan_window.status == "unknown"
    assert nan_window.reason == "bad_clock"


def test_reversed_and_duplicate_samples_match_sorted():
    people = roster(Participant("a", "Budi", "e1", False))
    ordered = [SpeakerSample(t, "e1", ("a",)) for t in range(0, 1001, 250)]
    messy = list(reversed(ordered)) + [ordered[0], ordered[-1]]
    first = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), ordered, people)
    second = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), messy, people)
    assert first == second
    assert first.status == "observed"


def test_unobserved_edges_hold_attribution():
    people = roster(Participant("a", "Budi", "e1", False))
    samples = [SpeakerSample(t, "e1", ("a",)) for t in range(400, 601, 100)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "unknown"
    assert result.reason == "low_coverage"
    assert abs(result.coverage - 0.2) < 1e-9


def test_dense_cluster_cannot_fake_missing_interval():
    people = roster(Participant("a", "Budi", "e1", False))
    samples = [SpeakerSample(0, "e1", ("a",)) for _ in range(50)]
    samples += [SpeakerSample(1000, "e1", ("a",)) for _ in range(50)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "unknown"
    assert result.reason == "stale_gap"


def test_gap_boundary_five_hundred_passes_five_oh_one_fails():
    people = roster(Participant("a", "Budi", "e1", False))
    passing = [SpeakerSample(t, "e1", ("a",)) for t in (0, 500, 1000)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), passing, people)
    assert result.status == "observed"
    failing = [SpeakerSample(t, "e1", ("a",)) for t in (0, 501, 1000)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), failing, people)
    assert result.status == "unknown"
    assert result.reason == "stale_gap"


def test_large_gap_in_long_window_fails_despite_high_coverage():
    people = roster(Participant("a", "Budi", "e1", False))
    samples = [SpeakerSample(t, "e1", ("a",)) for t in range(0, 6001, 250)]
    samples += [SpeakerSample(t, "e1", ("a",)) for t in range(6600, 12351, 250)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 12350), samples, people)
    assert result.status == "unknown"
    assert result.reason == "stale_gap"


def test_coverage_exactly_ninety_five_passes():
    people = roster(Participant("a", "Budi", "e1", False))
    samples = [SpeakerSample(t, "e1", ("a",)) for t in (0, 250, 500, 750, 950)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "observed"
    assert result.coverage == 0.95
    samples = [SpeakerSample(t, "e1", ("a",)) for t in (0, 250, 500, 750, 949)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "unknown"
    assert result.reason == "low_coverage"


def test_conflicting_ids_at_same_moment_stay_unknown():
    people = roster(
        Participant("a", "Budi", "e1", False),
        Participant("b", "Rani", "e1", False),
    )
    samples = [SpeakerSample(t, "e1", ("a",)) for t in (0, 250, 750, 1000)]
    samples += [SpeakerSample(500, "e1", ("a",)), SpeakerSample(500, "e1", ("b",))]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "unknown"
    assert result.reason == "conflict"


def test_empty_samples_stay_unknown():
    people = roster(Participant("a", "Budi", "e1", False))
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), [], people)
    assert result.status == "unknown"
    assert result.reason == "no_samples"


def test_samples_outside_window_stay_unknown():
    people = roster(Participant("a", "Budi", "e1", False))
    samples = [SpeakerSample(t, "e1", ("a",)) for t in (1500, 1750, 2000)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "unknown"
    assert result.reason == "unobserved_window"


def test_nonfinite_sample_moments_are_ignored():
    people = roster(Participant("a", "Budi", "e1", False))
    samples = [SpeakerSample(t, "e1", ("a",)) for t in range(0, 1001, 250)]
    samples += [SpeakerSample(float("nan"), "e1", ("a",))]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "observed"
    assert result.participant_id == "a"


def test_empty_active_ids_stay_unknown():
    people = roster(Participant("a", "Budi", "e1", False))
    samples = [SpeakerSample(t, "e1", ("a",)) for t in (0, 500, 1000)]
    samples.append(SpeakerSample(250, "e1", ()))
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "unknown"
    assert result.reason == "silence"


def test_roster_epoch_mismatch_stays_unknown():
    people = roster(Participant("a", "Budi", "e2", False))
    samples = [SpeakerSample(t, "e1", ("a",)) for t in range(0, 1001, 250)]
    result = attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert result.status == "unknown"
    assert result.reason == "left_roster"


def test_resolver_does_not_mutate_inputs_and_types_are_frozen():
    people = roster(Participant("a", "Budi", "e1", False))
    samples = [SpeakerSample(t, "e1", ("a",)) for t in range(0, 1001, 250)]
    frozen_samples = list(samples)
    frozen_roster = dict(people)
    attribute_speaker(SpeechWindow("t1", "e1", 0, 1000), samples, people)
    assert samples == frozen_samples
    assert people == frozen_roster
    for frozen, field, value in (
        (people["a"], "id", "x"),
        (samples[0], "at_ms", -1.0),
        (SpeechWindow("t1", "e1", 0, 1000), "started_ms", -1.0),
    ):
        try:
            setattr(frozen, field, value)
        except AttributeError:
            pass
        else:
            raise AssertionError("resolver types must be frozen")


def test_zero_duration_and_negative_clocks_stay_unknown():
    people = roster(Participant("a", "Budi", "e1", False))
    samples = [SpeakerSample(t, "e1", ("a",)) for t in range(0, 1001, 250)]
    for window in (
        SpeechWindow("t1", "e1", 500, 500),
        SpeechWindow("t1", "e1", -1000, 0),
        SpeechWindow("t1", "e1", 0, float("inf")),
    ):
        result = attribute_speaker(window, samples, people)
        assert result.status == "unknown"
        assert result.reason == "bad_clock"
