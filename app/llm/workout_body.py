from __future__ import annotations

import json
import re
from typing import Any

from openai import OpenAI

from app.config import get_settings
from app.garmin.workout_schema import WorkoutBody, normalize_workout_dict

WARMUP_RE = re.compile(
    r"(aquec\w*|warmup|warm[\s-]?up)",
    re.IGNORECASE,
)
COOLDOWN_RE = re.compile(
    r"(desaquec\w*|cooldown|cool[\s-]?down|voltar?\s+a?\s*calma|volta\s+a\s+calma)",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """Você converte descrições de treinos de corrida em JSON Garmin Connect.

Responda APENAS JSON válido (sem markdown).

Campos OBRIGATÓRIOS em todo stepType: stepTypeId, stepTypeKey, displayOrder.
Campos OBRIGATÓRIOS em todo endCondition: conditionTypeId, conditionTypeKey, displayOrder, displayable.
Em RepeatGroupDTO: type="RepeatGroupDTO", numberOfIterations (int), workoutSteps (array), skipLastRestStep=true.
Em ExecutableStepDTO: type="ExecutableStepDTO".

IDs:
- stepType: warmup=1, cooldown=2, interval=3, recovery=4, repeat=6 (displayOrder=mesmo id)
- endCondition: lap.button=1, time=2, distance=3, iterations=7
- targetType padrão: {"workoutTargetTypeId":1,"workoutTargetTypeKey":"no.target","displayOrder":1}

REGRA IMPORTANTE — aquecimento / desaquecimento:
- NÃO inclua stepTypeKey "warmup" a menos que o usuário peça explicitamente (ex: aquecimento, warmup).
- NÃO inclua stepTypeKey "cooldown" a menos que o usuário peça explicitamente (ex: desaquecimento, cooldown, volta à calma).
- Se o usuário só pedir "10x300m", o workoutSteps deve ter APENAS o RepeatGroupDTO (intervalos + recovery), sem warmup e sem cooldown.

Para "10x300m" / "10 vezes de 300 mt" (sem aquecimento/desaquecimento):
- só RepeatGroupDTO numberOfIterations=10 com filhos:
  - interval distance endConditionValue=300
  - recovery lap.button endConditionValue=1000

Exemplo (sem aquecimento/desaquecimento):
{
  "sportType": {"sportTypeId":1,"sportTypeKey":"running","displayOrder":1},
  "workoutName": "10x300m",
  "estimatedDistanceUnit": {"unitKey": null},
  "workoutSegments": [{
    "segmentOrder": 1,
    "sportType": {"sportTypeId":1,"sportTypeKey":"running","displayOrder":1},
    "workoutSteps": [
      {
        "type": "RepeatGroupDTO",
        "stepOrder": 1,
        "stepType": {"stepTypeId":6,"stepTypeKey":"repeat","displayOrder":6},
        "numberOfIterations": 10,
        "smartRepeat": false,
        "childStepId": 1,
        "skipLastRestStep": true,
        "endCondition": {"conditionTypeId":7,"conditionTypeKey":"iterations","displayOrder":7,"displayable":false},
        "workoutSteps": [
          {
            "type": "ExecutableStepDTO",
            "stepOrder": 2,
            "childStepId": 1,
            "stepType": {"stepTypeId":3,"stepTypeKey":"interval","displayOrder":3},
            "endCondition": {"conditionTypeId":3,"conditionTypeKey":"distance","displayOrder":3,"displayable":true},
            "endConditionValue": 300,
            "targetType": {"workoutTargetTypeId":1,"workoutTargetTypeKey":"no.target","displayOrder":1}
          },
          {
            "type": "ExecutableStepDTO",
            "stepOrder": 3,
            "childStepId": 1,
            "stepType": {"stepTypeId":4,"stepTypeKey":"recovery","displayOrder":4},
            "endCondition": {"conditionTypeId":1,"conditionTypeKey":"lap.button","displayOrder":1,"displayable":true},
            "endConditionValue": 1000,
            "targetType": {"workoutTargetTypeId":1,"workoutTargetTypeKey":"no.target","displayOrder":1}
          }
        ]
      }
    ]
  }],
  "estimatedDurationInSecs": 0,
  "estimatedDistanceInMeters": 0,
  "isWheelchair": false
}
"""


def _wants_warmup(text: str) -> bool:
    return bool(WARMUP_RE.search(text))


def _wants_cooldown(text: str) -> bool:
    return bool(COOLDOWN_RE.search(text))


def _lap_step(order: int, step_key: str, step_id: int) -> dict[str, Any]:
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {
            "stepTypeId": step_id,
            "stepTypeKey": step_key,
            "displayOrder": step_id,
        },
        "endCondition": {
            "conditionTypeId": 1,
            "conditionTypeKey": "lap.button",
            "displayOrder": 1,
            "displayable": True,
        },
        "endConditionValue": 1000,
        "targetType": {
            "workoutTargetTypeId": 1,
            "workoutTargetTypeKey": "no.target",
            "displayOrder": 1,
        },
    }


def _strip_unrequested_warm_cool(raw: dict[str, Any], text: str) -> dict[str, Any]:
    """Remove warmup/cooldown gerados pela LLM se o usuário não pediu."""
    want_w = _wants_warmup(text)
    want_c = _wants_cooldown(text)
    if want_w and want_c:
        return raw

    for seg in raw.get("workoutSegments") or []:
        steps = seg.get("workoutSteps") or []
        kept = []
        for step in steps:
            key = (step.get("stepType") or {}).get("stepTypeKey")
            if key == "warmup" and not want_w:
                continue
            if key == "cooldown" and not want_c:
                continue
            kept.append(step)
        # renumerar stepOrder
        for i, step in enumerate(kept, start=1):
            step["stepOrder"] = i
        seg["workoutSteps"] = kept
    return raw


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    if not text.startswith("{"):
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            text = m.group(0)
    return json.loads(text)


def _fallback_interval_workout(text: str) -> WorkoutBody | None:
    """Heurística para padrões tipo 10x300m / 10 vezes de 300 mt."""
    t = text.lower().replace(",", ".")
    m = re.search(
        r"(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(m|mt|metros|km)?",
        t,
    )
    if not m:
        m = re.search(
            r"(\d+)\s*(?:vezes|reps?)\s*(?:de\s*)?(\d+(?:\.\d+)?)\s*(m|mt|metros|km)?",
            t,
        )
    if not m:
        return None

    reps = int(m.group(1))
    dist = float(m.group(2))
    unit = (m.group(3) or "m").lower()
    meters = dist * 1000 if unit.startswith("km") else dist
    name = f"{reps}x{int(meters)}m"

    steps: list[dict[str, Any]] = []
    order = 1

    if _wants_warmup(text):
        steps.append(_lap_step(order, "warmup", 1))
        order += 1

    steps.append(
        {
            "type": "RepeatGroupDTO",
            "stepOrder": order,
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
            "numberOfIterations": reps,
            "smartRepeat": False,
            "childStepId": 1,
            "skipLastRestStep": True,
            "endCondition": {
                "conditionTypeId": 7,
                "conditionTypeKey": "iterations",
                "displayOrder": 7,
                "displayable": False,
            },
            "workoutSteps": [
                {
                    "type": "ExecutableStepDTO",
                    "stepOrder": order + 1,
                    "childStepId": 1,
                    "stepType": {
                        "stepTypeId": 3,
                        "stepTypeKey": "interval",
                        "displayOrder": 3,
                    },
                    "endCondition": {
                        "conditionTypeId": 3,
                        "conditionTypeKey": "distance",
                        "displayOrder": 3,
                        "displayable": True,
                    },
                    "endConditionValue": meters,
                    "targetType": {
                        "workoutTargetTypeId": 1,
                        "workoutTargetTypeKey": "no.target",
                        "displayOrder": 1,
                    },
                },
                {
                    "type": "ExecutableStepDTO",
                    "stepOrder": order + 2,
                    "childStepId": 1,
                    "stepType": {
                        "stepTypeId": 4,
                        "stepTypeKey": "recovery",
                        "displayOrder": 4,
                    },
                    "endCondition": {
                        "conditionTypeId": 1,
                        "conditionTypeKey": "lap.button",
                        "displayOrder": 1,
                        "displayable": True,
                    },
                    "endConditionValue": 1000,
                    "targetType": {
                        "workoutTargetTypeId": 1,
                        "workoutTargetTypeKey": "no.target",
                        "displayOrder": 1,
                    },
                },
            ],
        }
    )
    order += 1

    if _wants_cooldown(text):
        steps.append(_lap_step(order, "cooldown", 2))

    raw = {
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
        "workoutName": name,
        "estimatedDistanceUnit": {"unitKey": None},
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
                "workoutSteps": steps,
            }
        ],
        "estimatedDurationInSecs": 0,
        "estimatedDistanceInMeters": 0,
        "isWheelchair": False,
    }
    return WorkoutBody.model_validate(normalize_workout_dict(raw))


def text_to_workout_body(text: str) -> WorkoutBody:
    settings = get_settings()
    if not settings.groq_api_key:
        fb = _fallback_interval_workout(text)
        if fb:
            return fb
        raise RuntimeError("GROQ_API_KEY não configurada.")

    client = OpenAI(api_key=settings.groq_api_key, base_url=settings.groq_base_url)
    resp = client.chat.completions.create(
        model=settings.groq_model,
        temperature=0.1,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text.strip()},
        ],
    )
    content = resp.choices[0].message.content or ""
    try:
        raw = _strip_unrequested_warm_cool(_extract_json(content), text)
        raw = normalize_workout_dict(raw)
        body = WorkoutBody.model_validate(raw)
        for seg in body.workoutSegments:
            for step in seg.workoutSteps:
                if getattr(step, "type", None) == "RepeatGroupDTO" and not step.workoutSteps:
                    raise ValueError("RepeatGroup sem filhos")
        return body
    except Exception:
        fb = _fallback_interval_workout(text)
        if fb:
            return fb
        raise
