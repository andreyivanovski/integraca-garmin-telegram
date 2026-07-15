from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.auth.garmin_auth import get_auth
from app.garmin.client import GarminClient, get_client
from app.garmin.workout_schema import WorkoutBody
from app.llm.workout_body import text_to_workout_body


@dataclass
class DraftResult:
    workout_body: dict[str, Any]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {"workout_body": self.workout_body, "summary": self.summary}


@dataclass
class ExecuteResult:
    workout_id: int
    workout_name: str
    date: str
    device_id: int
    schedule: Any
    device_message: Any

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def draft_workout(text: str) -> DraftResult:
    body = text_to_workout_body(text)
    return DraftResult(workout_body=body.model_dump(mode="json"), summary=body.summary())


def execute_workout(
    workout_body: dict[str, Any],
    date: str,
    device_id: int | None = None,
    client: GarminClient | None = None,
) -> ExecuteResult:
    auth = get_auth()
    if not auth.is_authenticated:
        raise RuntimeError("Garmin não autenticado. Faça o setup em /setup.")

    body = WorkoutBody.model_validate(workout_body)
    gc = client or get_client()
    device_id = device_id or auth.get_device_id()

    created = gc.create_workout(body.model_dump(mode="json"))
    workout_id = int(created["workoutId"])
    workout_name = created.get("workoutName") or body.workoutName

    schedule = gc.schedule_workout(workout_id, date)
    message = gc.send_workout_to_device(workout_id, device_id=device_id, workout_name=workout_name)

    return ExecuteResult(
        workout_id=workout_id,
        workout_name=workout_name,
        date=date,
        device_id=device_id,
        schedule=schedule,
        device_message=message,
    )
