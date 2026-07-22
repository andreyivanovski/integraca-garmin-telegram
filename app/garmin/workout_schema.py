from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator


STEP_TYPE_META: dict[str, tuple[int, int]] = {
    "warmup": (1, 1),
    "cooldown": (2, 2),
    "interval": (3, 3),
    "recovery": (4, 4),
    "rest": (5, 5),
    "repeat": (6, 6),
    "other": (8, 8),
}

END_CONDITION_META: dict[str, tuple[int, int, bool]] = {
    "lap.button": (1, 1, True),
    "time": (2, 2, True),
    "distance": (3, 3, True),
    "hr.less.than": (4, 4, True),
    "hr.greater.than": (5, 5, True),
    "calories": (6, 6, True),
    "iterations": (7, 7, False),
}


def _fill_step_type(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    out = dict(data)
    key = out.get("stepTypeKey") or "interval"
    sid, order = STEP_TYPE_META.get(key, (3, 3))
    out.setdefault("stepTypeId", sid)
    out.setdefault("displayOrder", order)
    out.setdefault("stepTypeKey", key)
    return out


def _fill_end_condition(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    out = dict(data)
    key = out.get("conditionTypeKey") or "lap.button"
    cid, order, displayable = END_CONDITION_META.get(key, (1, 1, True))
    out.setdefault("conditionTypeId", cid)
    out.setdefault("displayOrder", order)
    out.setdefault("displayable", displayable)
    out.setdefault("conditionTypeKey", key)
    return out


class SportType(BaseModel):
    sportTypeId: int = 1
    sportTypeKey: str = "running"
    displayOrder: int = 1


class StepType(BaseModel):
    stepTypeId: int = 3
    stepTypeKey: str = "interval"
    displayOrder: int = 3

    @model_validator(mode="before")
    @classmethod
    def fill(cls, data: Any) -> Any:
        return _fill_step_type(data)


class EndCondition(BaseModel):
    conditionTypeId: int = 1
    conditionTypeKey: str = "lap.button"
    displayOrder: int = 1
    displayable: bool = True

    @model_validator(mode="before")
    @classmethod
    def fill(cls, data: Any) -> Any:
        return _fill_end_condition(data)


class TargetType(BaseModel):
    workoutTargetTypeId: int = 1
    workoutTargetTypeKey: str = "no.target"
    displayOrder: int = 1


class ExecutableStep(BaseModel):
    type: Literal["ExecutableStepDTO"] = "ExecutableStepDTO"
    stepId: int | None = None
    stepOrder: int = 1
    stepType: StepType = Field(default_factory=StepType)
    endCondition: EndCondition = Field(default_factory=EndCondition)
    endConditionValue: float | None = 1000
    preferredEndConditionUnit: Any | None = None
    stepAudioNote: Any | None = None
    targetType: TargetType = Field(default_factory=TargetType)
    targetValueOne: float | None = None
    targetValueTwo: float | None = None
    targetValueUnit: Any | None = None
    childStepId: int | None = None
    category: Any | None = None
    exerciseName: Any | None = None

    @model_validator(mode="before")
    @classmethod
    def defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        out.setdefault("type", "ExecutableStepDTO")
        if "stepType" in out:
            out["stepType"] = _fill_step_type(out["stepType"])
        if "endCondition" in out:
            out["endCondition"] = _fill_end_condition(out["endCondition"])
        return out


class RepeatGroup(BaseModel):
    type: Literal["RepeatGroupDTO"] = "RepeatGroupDTO"
    stepId: int | None = None
    stepOrder: int = 1
    stepType: StepType = Field(
        default_factory=lambda: StepType(stepTypeId=6, stepTypeKey="repeat", displayOrder=6)
    )
    numberOfIterations: int = 1
    smartRepeat: bool = False
    childStepId: int | None = 1
    workoutSteps: list[ExecutableStep] = Field(default_factory=list)
    endCondition: EndCondition = Field(
        default_factory=lambda: EndCondition(
            conditionTypeId=7,
            conditionTypeKey="iterations",
            displayOrder=7,
            displayable=False,
        )
    )
    skipLastRestStep: bool = True

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        out.setdefault("type", "RepeatGroupDTO")
        # aliases comuns da LLM
        if "numberOfIterations" not in out:
            for alt in ("iterations", "repeatCount", "reps", "times"):
                if alt in out:
                    out["numberOfIterations"] = out[alt]
                    break
        if "workoutSteps" not in out:
            for alt in ("steps", "childSteps", "children"):
                if alt in out:
                    out["workoutSteps"] = out[alt]
                    break
        out.setdefault(
            "stepType",
            {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
        )
        out["stepType"] = _fill_step_type(out["stepType"])
        out.setdefault(
            "endCondition",
            {
                "conditionTypeId": 7,
                "conditionTypeKey": "iterations",
                "displayOrder": 7,
                "displayable": False,
            },
        )
        out["endCondition"] = _fill_end_condition(out["endCondition"])
        return out


WorkoutStep = Annotated[Union[ExecutableStep, RepeatGroup], Field(discriminator="type")]


class WorkoutSegment(BaseModel):
    segmentOrder: int = 1
    sportType: SportType = Field(default_factory=SportType)
    workoutSteps: list[WorkoutStep] = Field(default_factory=list)


class WorkoutBody(BaseModel):
    sportType: SportType = Field(default_factory=SportType)
    subSportType: Any | None = None
    workoutName: str = "Treino"
    estimatedDistanceUnit: dict[str, Any] = Field(default_factory=lambda: {"unitKey": None})
    workoutSegments: list[WorkoutSegment]
    avgTrainingSpeed: float | None = None
    estimatedDurationInSecs: int = 0
    estimatedDistanceInMeters: float = 0
    estimateType: Any | None = None
    isWheelchair: bool = False

    @field_validator("workoutName", mode="before")
    @classmethod
    def name_fallback(cls, v: Any) -> Any:
        return v or "Treino"

    def summary(self) -> str:
        lines = [f"Treino: {self.workoutName}", f"Esporte: {self.sportType.sportTypeKey}"]
        for seg in self.workoutSegments:
            for step in seg.workoutSteps:
                if getattr(step, "type", None) == "RepeatGroupDTO" or isinstance(step, RepeatGroup):
                    child_bits = []
                    for c in step.workoutSteps:
                        bit = (
                            f"{c.stepType.stepTypeKey}/"
                            f"{c.endCondition.conditionTypeKey}={c.endConditionValue}"
                        )
                        tt = getattr(c.targetType, "workoutTargetTypeKey", None) or "no.target"
                        if tt == "pace.zone" and c.targetValueOne and c.targetValueTwo:
                            # m/s → s/km aproximado para leitura
                            def _fmt(mps: float) -> str:
                                spk = 1000.0 / mps
                                return f"{int(spk // 60)}:{int(spk % 60):02d}/km"

                            bit += f" pace {_fmt(c.targetValueOne)}-{_fmt(c.targetValueTwo)}"
                        child_bits.append(bit)
                    lines.append(f"- Repeat x{step.numberOfIterations}: {', '.join(child_bits)}")
                else:
                    lines.append(
                        f"- {step.stepType.stepTypeKey}: "
                        f"{step.endCondition.conditionTypeKey}={step.endConditionValue}"
                    )
        return "\n".join(lines)


def _fix_sport(st: Any) -> dict[str, Any]:
    if not isinstance(st, dict):
        return {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}
    out = dict(st)
    key = str(out.get("sportTypeKey") or "running").lower().strip()
    # LLM às vezes trunca: runnin, runni, run
    if key.startswith("run"):
        key = "running"
        out["sportTypeId"] = 1
    out["sportTypeKey"] = key
    out.setdefault("sportTypeId", 1 if key == "running" else out.get("sportTypeId") or 1)
    out.setdefault("displayOrder", out.get("sportTypeId") or 1)
    # chave incompleta / inválida → força running (uso atual do bot)
    if key not in {"running", "cycling", "swimming", "strength_training", "cardio", "yoga"}:
        out = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}
    return out


def normalize_workout_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Preenche campos que a LLM costuma omitir e corrige aliases."""
    data = dict(raw)
    data["sportType"] = _fix_sport(data.get("sportType"))
    data.setdefault("estimatedDistanceUnit", {"unitKey": None})
    data.setdefault("estimatedDurationInSecs", 0)
    data.setdefault("estimatedDistanceInMeters", 0)
    data.setdefault("isWheelchair", False)
    data.setdefault("subSportType", None)
    data.setdefault("workoutName", "Treino")

    segments = data.get("workoutSegments") or []
    if not segments and "workoutSteps" in data:
        segments = [{"segmentOrder": 1, "workoutSteps": data.pop("workoutSteps")}]

    fixed_segments = []
    for i, seg in enumerate(segments):
        if seg is None:
            continue
        s = dict(seg)
        s["segmentOrder"] = int(s.get("segmentOrder") or (i + 1))
        s["sportType"] = _fix_sport(s.get("sportType") or data.get("sportType"))
        steps = []
        for j, step in enumerate(s.get("workoutSteps") or []):
            if step is None:
                continue
            st = dict(step)
            st.setdefault("stepOrder", j + 1)
            step_type = st.get("type") or st.get("stepType", {}).get("stepTypeKey")
            if (
                step_type == "repeat"
                or st.get("type") == "RepeatGroupDTO"
                or "numberOfIterations" in st
                or "iterations" in st
            ):
                st["type"] = "RepeatGroupDTO"
                if "workoutSteps" not in st:
                    for alt in ("steps", "childSteps", "children"):
                        if alt in st:
                            st["workoutSteps"] = st[alt]
                            break
                children = []
                for k, ch in enumerate(st.get("workoutSteps") or []):
                    if ch is None:
                        continue
                    child = dict(ch)
                    child.setdefault("type", "ExecutableStepDTO")
                    child.setdefault("stepOrder", k + 1)
                    child.setdefault("childStepId", 1)
                    children.append(child)
                st["workoutSteps"] = children
                if not children:
                    continue  # repeat vazio → descarta (evita 400)
            else:
                st.setdefault("type", "ExecutableStepDTO")
            steps.append(st)
        if not steps:
            continue  # segmento sem steps → 400 da Garmin
        s["workoutSteps"] = steps
        fixed_segments.append(s)

    if not fixed_segments:
        raise ValueError("Workout sem segmentos/steps válidos")

    # Garante segmentOrder único e sequencial
    for i, s in enumerate(fixed_segments):
        s["segmentOrder"] = i + 1
    data["workoutSegments"] = fixed_segments
    return data
