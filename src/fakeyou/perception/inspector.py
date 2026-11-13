from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

VisualVerdict = Literal["supported", "not_visible", "unclear"]
InspectionText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class VisualInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    verdict: VisualVerdict
    answer: InspectionText | None = None
    observed_facts: tuple[InspectionText, ...] = ()

    @model_validator(mode="after")
    def answer_matches_verdict(self) -> Self:
        if (self.verdict == "supported") != (self.answer is not None):
            raise ValueError("supported requires an answer; abstentions require no answer")
        return self
