from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, field_validator

from app.config import get_settings
from app.garmin.workout_schema import WorkoutBody, normalize_workout_dict

logger = logging.getLogger(__name__)

WARMUP_RE = re.compile(r"(?:(?<!des)aquec\w*|warmup|warm[\s-]?up)", re.IGNORECASE)
COOLDOWN_RE = re.compile(
    r"(?:desaquec\w*|cooldown|cool[\s-]?down|voltar?\s+a?\s*calma|volta\s+a\s+calma)",
    re.IGNORECASE,
)

# 10x400 / 10 X 400 / 10 vezes de 400 m
INTERVAL_RE = re.compile(
    r"(?P<reps>\d+)\s*[x×]\s*(?P<dist>\d+(?:[.,]\d+)?)\s*(?P<unit>m|mt|metros|km)?",
    re.IGNORECASE,
)
INTERVAL_RE_ALT = re.compile(
    r"(?P<reps>\d+)\s*(?:vezes|reps?)\s*(?:de\s*)?(?P<dist>\d+(?:[.,]\d+)?)\s*(?P<unit>m|mt|metros|km)?",
    re.IGNORECASE,
)

# ritmo/pace 01:06 a 01:20 | 1:06-1:20 | 5:00/km | 5:00 a 5:30 /km
PACE_RANGE_RE = re.compile(
    r"(?:ritmo|pace|passo|em)?\s*"
    r"(?P<a>\d{1,2}:\d{2})"
    r"\s*(?:a|á|à|ate|até|-|–|—|ate|/)\s*"
    r"(?P<b>\d{1,2}:\d{2})"
    r"\s*(?P<perkm>/?\s*km|min/?km|por\s*km)?",
    re.IGNORECASE,
)
PACE_SINGLE_RE = re.compile(
    r"(?:ritmo|pace|passo)\s*(?:de\s*)?(?P<a>\d{1,2}:\d{2})"
    r"\s*(?P<perkm>/?\s*km|min/?km|por\s*km)?",
    re.IGNORECASE,
)

# aquecimento|desaquecimento|recuperação + tempo/distância/relógio
# Exemplos: "aquecimento 10 min", "2,5km Aquecimento", "pausa 90s", "intervalo parado de 1:30"
_PHASE_QTY = (
    r"(?:de\s*|com\s*|em\s*|por\s*)?"
    r"(?:"
    r"(?P<clock>\d{1,2}:\d{2})"
    r"|"
    r"(?P<val>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>s|seg|segs|segundos|min|mins|minuto\w*|h|hora\w*|m|mt|metros|km)?"
    r")"
)

WARMUP_PHASE_RE = re.compile(
    rf"(?:(?<!des)aquec\w*|warmup|warm[\s-]?up)\s*{_PHASE_QTY}",
    re.IGNORECASE,
)
WARMUP_QTY_BEFORE_RE = re.compile(
    rf"(?P<val>\d+(?:[.,]\d+)?)\s*(?P<unit>km|m|mt|metros)\s+"
    rf"(?:(?<!des)aquec\w*|warmup|warm[\s-]?up)",
    re.IGNORECASE,
)
COOLDOWN_PHASE_RE = re.compile(
    rf"(?:desaquec\w*|cooldown|cool[\s-]?down|volta(?:r)?\s+a\s*calma)\s*{_PHASE_QTY}",
    re.IGNORECASE,
)
COOLDOWN_QTY_BEFORE_RE = re.compile(
    rf"(?P<val>\d+(?:[.,]\d+)?)\s*(?P<unit>km|m|mt|metros)\s+"
    rf"(?:desaquec\w*|cooldown|cool[\s-]?down|volta(?:r)?\s+a\s*calma)",
    re.IGNORECASE,
)

# Recuperação ENTRE as reps (prioridade alta)
INTER_RECOVERY_RE = re.compile(
    rf"(?:"
    rf"intervalo\s+parado|"
    rf"recupera(?:ção|cao|\w*)\s+entre|"
    rf"pausa\s+entre|"
    rf"entre\s+(?:elas|eles|tiros|séries|series|reps?)"
    rf")\s*{_PHASE_QTY}",
    re.IGNORECASE,
)

# Descanso isolado (ex.: após aquecimento, ANTES dos tiros) — NÃO é recovery do repeat
PRE_REST_RE = re.compile(
    rf"(?:descanso\s+parado|descans\w+|repouso)\s*{_PHASE_QTY}",
    re.IGNORECASE,
)

# Fallback genérico de recovery só se não houver inter-recovery
RECOVERY_FALLBACK_RE = re.compile(
    rf"(?:recupera(?:ção|cao|\w*)|recovery|pausa)\s*{_PHASE_QTY}",
    re.IGNORECASE,
)

# 400> 1:28/32  ou  1:28/32  (minutos do segundo relógio implícitos)
LAP_SLASH_PACE_RE = re.compile(
    r"(?:(?P<dist>\d+)\s*m?\s*>\s*)?(?P<a>\d{1,2}:\d{2})\s*/\s*(?P<b>\d{2})\b",
    re.IGNORECASE,
)

NORMALIZE_SYSTEM = """Você é um extrator. Converte texto de treino de corrida em JSON estrito.

Responda APENAS JSON (sem markdown) neste schema:
{
  "workout_name": string,
  "reps": int|null,
  "interval_meters": number|null,
  "interval_seconds": number|null,
  "pace_mode": "none"|"lap_time"|"per_km",
  "pace_fast_clock": "M:SS"|null,
  "pace_slow_clock": "M:SS"|null,
  "recovery_mode": "lap"|"time"|"distance",
  "recovery_seconds": number|null,
  "recovery_meters": number|null,
  "pre_repeat_rest_mode": "none"|"time"|"distance",
  "pre_repeat_rest_seconds": number|null,
  "pre_repeat_rest_meters": number|null,
  "warmup": bool,
  "warmup_mode": "lap"|"time"|"distance",
  "warmup_seconds": number|null,
  "warmup_meters": number|null,
  "cooldown": bool,
  "cooldown_mode": "lap"|"time"|"distance",
  "cooldown_seconds": number|null,
  "cooldown_meters": number|null
}

REGRAS (obrigatórias):
1) "10x400" / "10x 400" → reps=10, interval_meters=400
2) Distância em METROS (2,5km → 2500). Tempo em SEGUNDOS.
3) "2,5km Aquecimento" / "Aquecimento 2,5km" → warmup distance 2500
4) "Descanso parado de 3min" DEPOIS do aquecimento e ANTES dos tiros → pre_repeat_rest_* (NÃO recovery)
5) "intervalo parado de 1:30" / recuperação entre tiros → recovery_* do Repeat
6) "400> 1:28/32" → pace_mode=lap_time, clocks 1:28 e 1:32
7) Ritmo "5:00/km" → pace_mode=per_km
8) warmup/cooldown = true só se o texto pedir
9) sport sempre corrida
"""


class WorkoutIntent(BaseModel):
    workout_name: str = "Treino"
    reps: int | None = None
    interval_meters: float | None = None
    interval_seconds: float | None = None
    pace_mode: str = "none"  # none | lap_time | per_km
    pace_fast_clock: str | None = None
    pace_slow_clock: str | None = None
    recovery_mode: str = "lap"  # lap | time | distance
    recovery_seconds: float | None = None
    recovery_meters: float | None = None
    warmup: bool = False
    warmup_mode: str = "lap"
    warmup_seconds: float | None = None
    warmup_meters: float | None = None
    cooldown: bool = False
    cooldown_mode: str = "lap"
    cooldown_seconds: float | None = None
    cooldown_meters: float | None = None
    # Descanso isolado entre aquecimento e o bloco de tiros
    pre_repeat_rest_mode: str = "none"  # none | time | distance
    pre_repeat_rest_seconds: float | None = None
    pre_repeat_rest_meters: float | None = None

    @field_validator("pace_mode", mode="before")
    @classmethod
    def _pace_mode(cls, v: Any) -> str:
        v = (str(v) if v is not None else "none").lower().strip()
        if v in {"lap", "lap_time", "lap-time", "tempo_volta"}:
            return "lap_time"
        if v in {"km", "per_km", "per-km", "min_km", "pace_km"}:
            return "per_km"
        if v in {"none", "", "null", "no"}:
            return "none"
        return v if v in {"none", "lap_time", "per_km"} else "none"

    @field_validator(
        "recovery_mode",
        "warmup_mode",
        "cooldown_mode",
        "pre_repeat_rest_mode",
        mode="before",
    )
    @classmethod
    def _end_mode(cls, v: Any) -> str:
        v = (str(v) if v is not None else "lap").lower().strip()
        if v in {"none", "", "null", "no"}:
            return "none"
        if v in {"time", "tempo", "seconds", "segundos"}:
            return "time"
        if v in {"distance", "distancia", "metros", "m"}:
            return "distance"
        if v == "none":
            return "none"
        return "lap"


@dataclass
class PhaseEnd:
    mode: str = "lap"  # lap | time | distance
    seconds: float | None = None
    meters: float | None = None


@dataclass
class PaceTarget:
    """Velocidades em m/s (Garmin pace.zone: One=mais rápido, Two=mais lento)."""

    fast_mps: float
    slow_mps: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "targetType": {
                "workoutTargetTypeId": 6,
                "workoutTargetTypeKey": "pace.zone",
                "displayOrder": 6,
            },
            "targetValueOne": self.fast_mps,
            "targetValueTwo": self.slow_mps,
            "targetValueUnit": None,
        }


def _wants_warmup(text: str) -> bool:
    return bool(WARMUP_RE.search(text))


def _wants_cooldown(text: str) -> bool:
    return bool(COOLDOWN_RE.search(text))


def _qty_match_to_phase(m: re.Match[str] | None) -> PhaseEnd | None:
    if not m:
        return None
    g = m.groupdict()
    clock = g.get("clock")
    if clock:
        try:
            return PhaseEnd(mode="time", seconds=_clock_to_seconds(clock))
        except ValueError:
            return None
    raw_val = g.get("val")
    if raw_val is None:
        return None
    val = float(str(raw_val).replace(",", "."))
    unit = (g.get("unit") or "").lower().strip()
    if unit.startswith("min") or unit.startswith("hora") or unit == "h":
        mult = 3600 if unit.startswith("hora") or unit == "h" else 60
        return PhaseEnd(mode="time", seconds=val * mult)
    if unit.startswith("km"):
        return PhaseEnd(mode="distance", meters=val * 1000)
    if unit in {"m", "mt", "metros"}:
        return PhaseEnd(mode="distance", meters=val)
    if unit.startswith("s") or unit.startswith("seg") or unit == "":
        if not unit and val >= 100:
            return PhaseEnd(mode="distance", meters=val)
        return PhaseEnd(mode="time", seconds=val)
    return PhaseEnd(mode="time", seconds=val)


def _parse_warmup_phase(text: str) -> PhaseEnd | None:
    return _qty_match_to_phase(WARMUP_QTY_BEFORE_RE.search(text)) or _qty_match_to_phase(
        WARMUP_PHASE_RE.search(text)
    )


def _parse_cooldown_phase(text: str) -> PhaseEnd | None:
    return _qty_match_to_phase(COOLDOWN_QTY_BEFORE_RE.search(text)) or _qty_match_to_phase(
        COOLDOWN_PHASE_RE.search(text)
    )


def _parse_inter_recovery(text: str) -> PhaseEnd | None:
    return _qty_match_to_phase(INTER_RECOVERY_RE.search(text))


def _parse_pre_repeat_rest(text: str) -> PhaseEnd | None:
    return _qty_match_to_phase(PRE_REST_RE.search(text))


def _parse_recovery_fallback(text: str) -> PhaseEnd | None:
    return _qty_match_to_phase(RECOVERY_FALLBACK_RE.search(text))


def _parse_slash_pace(text: str) -> tuple[str, str] | None:
    """400> 1:28/32 → ('1:28', '1:32')."""
    m = LAP_SLASH_PACE_RE.search(text)
    if not m:
        return None
    a = m.group("a")
    b_sec = m.group("b")
    mins = a.split(":")[0]
    return a, f"{mins}:{b_sec}"


def _apply_phase_to_intent(intent: WorkoutIntent, text: str) -> WorkoutIntent:
    """Sobrescreve warmup/cooldown/recovery/pre-rest a partir do texto (sempre)."""
    t = text.replace("\r", "\n")

    wu = _parse_warmup_phase(t)
    if wu:
        intent.warmup = True
        intent.warmup_mode = wu.mode
        intent.warmup_seconds = wu.seconds
        intent.warmup_meters = wu.meters
    elif _wants_warmup(t):
        intent.warmup = True

    cd = _parse_cooldown_phase(t)
    if cd:
        intent.cooldown = True
        intent.cooldown_mode = cd.mode
        intent.cooldown_seconds = cd.seconds
        intent.cooldown_meters = cd.meters
    elif _wants_cooldown(t):
        intent.cooldown = True

    # 1) recovery entre reps (intervalo parado…)
    inter = _parse_inter_recovery(t)
    # 2) descanso isolado (descanso parado…) — NÃO misturar com recovery
    pre = _parse_pre_repeat_rest(t)

    if inter:
        intent.recovery_mode = inter.mode
        intent.recovery_seconds = inter.seconds
        intent.recovery_meters = inter.meters
    elif pre is None:
        # só usa fallback se não houver "descanso parado" competindo
        fb = _parse_recovery_fallback(t)
        if fb:
            intent.recovery_mode = fb.mode
            intent.recovery_seconds = fb.seconds
            intent.recovery_meters = fb.meters

    if pre:
        # Se também existe inter-recovery, pre é descanso pós-aquecimento.
        # Se NÃO existe inter e o único descanso era o pre, e recovery ainda lap:
        # preferir pre como pre_repeat_rest e NÃO como recovery do repeat.
        intent.pre_repeat_rest_mode = pre.mode
        intent.pre_repeat_rest_seconds = pre.seconds
        intent.pre_repeat_rest_meters = pre.meters
        if not inter and intent.recovery_mode != "lap":
            # recovery veio do mesmo "descanso"? limpa recovery do repeat
            if (
                intent.recovery_seconds == pre.seconds
                and intent.recovery_meters == pre.meters
            ):
                intent.recovery_mode = "lap"
                intent.recovery_seconds = None
                intent.recovery_meters = None

    slash = _parse_slash_pace(t)
    if slash and intent.pace_mode == "none":
        intent.pace_mode = "lap_time"
        intent.pace_fast_clock, intent.pace_slow_clock = slash

    return intent


def _clock_to_seconds(clock: str) -> float:
    clock = clock.strip()
    parts = clock.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"Relógio inválido: {clock}")


def _seconds_to_mps_for_distance(seconds: float, meters: float) -> float:
    if seconds <= 0 or meters <= 0:
        raise ValueError("tempo/distância inválidos para pace")
    return meters / seconds


def _per_km_clock_to_mps(clock: str) -> float:
    secs = _clock_to_seconds(clock)
    if secs <= 0:
        raise ValueError("pace/km inválido")
    return 1000.0 / secs


def _no_target() -> dict[str, Any]:
    return {
        "targetType": {
            "workoutTargetTypeId": 1,
            "workoutTargetTypeKey": "no.target",
            "displayOrder": 1,
        }
    }


def _resolve_pace(intent: WorkoutIntent) -> PaceTarget | None:
    if intent.pace_mode == "none" or not intent.pace_fast_clock:
        return None
    fast_c = intent.pace_fast_clock
    slow_c = intent.pace_slow_clock or intent.pace_fast_clock
    try:
        if intent.pace_mode == "lap_time":
            meters = float(intent.interval_meters or 0)
            if meters <= 0:
                return None
            a = _seconds_to_mps_for_distance(_clock_to_seconds(fast_c), meters)
            b = _seconds_to_mps_for_distance(_clock_to_seconds(slow_c), meters)
        else:  # per_km
            a = _per_km_clock_to_mps(fast_c)
            b = _per_km_clock_to_mps(slow_c)
    except ValueError:
        return None
    # One = mais rápido (maior m/s), Two = mais lento
    return PaceTarget(fast_mps=max(a, b), slow_mps=min(a, b))


def _parse_intent_regex(text: str) -> WorkoutIntent | None:
    t = text.strip()
    m = INTERVAL_RE.search(t) or INTERVAL_RE_ALT.search(t)
    if not m:
        return None

    reps = int(m.group("reps"))
    dist = float(m.group("dist").replace(",", "."))
    unit = (m.group("unit") or "m").lower()
    meters = dist * 1000 if unit.startswith("km") else dist

    pace_mode = "none"
    fast_c = slow_c = None
    pr = PACE_RANGE_RE.search(t)
    ps = None if pr else PACE_SINGLE_RE.search(t)
    if pr:
        fast_c, slow_c = pr.group("a"), pr.group("b")
        perkm = bool(pr.group("perkm"))
        # Com distância de intervalo e clocks ~1–3 min → tempo de volta
        pace_mode = "per_km" if perkm else "lap_time"
    elif ps:
        fast_c = slow_c = ps.group("a")
        perkm = bool(ps.group("perkm"))
        pace_mode = "per_km" if perkm else "lap_time"

    recovery_mode = "lap"
    recovery_seconds = None
    recovery_meters = None
    inter = _parse_inter_recovery(t)
    pre = _parse_pre_repeat_rest(t)
    if inter:
        recovery_mode = inter.mode
        recovery_seconds = inter.seconds
        recovery_meters = inter.meters
    elif not pre:
        fb = _parse_recovery_fallback(t)
        if fb:
            recovery_mode = fb.mode
            recovery_seconds = fb.seconds
            recovery_meters = fb.meters

    wu = _parse_warmup_phase(t)
    cd = _parse_cooldown_phase(t)

    slash = _parse_slash_pace(t)
    if slash and pace_mode == "none":
        pace_mode = "lap_time"
        fast_c, slow_c = slash

    name = f"{reps}x{int(meters)}m"
    if fast_c and slow_c and fast_c != slow_c:
        name += f" @{fast_c}-{slow_c}"
    elif fast_c:
        name += f" @{fast_c}"

    return WorkoutIntent(
        workout_name=name,
        reps=reps,
        interval_meters=meters,
        pace_mode=pace_mode,
        pace_fast_clock=fast_c,
        pace_slow_clock=slow_c,
        recovery_mode=recovery_mode,
        recovery_seconds=recovery_seconds,
        recovery_meters=recovery_meters,
        warmup=bool(wu) or _wants_warmup(t),
        warmup_mode=wu.mode if wu else "lap",
        warmup_seconds=wu.seconds if wu else None,
        warmup_meters=wu.meters if wu else None,
        cooldown=bool(cd) or _wants_cooldown(t),
        cooldown_mode=cd.mode if cd else "lap",
        cooldown_seconds=cd.seconds if cd else None,
        cooldown_meters=cd.meters if cd else None,
        pre_repeat_rest_mode=pre.mode if pre else "none",
        pre_repeat_rest_seconds=pre.seconds if pre else None,
        pre_repeat_rest_meters=pre.meters if pre else None,
    )


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    if not text.startswith("{"):
        found = re.search(r"\{[\s\S]*\}", text)
        if found:
            text = found.group(0)
    return json.loads(text)


def _parse_intent_llm(text: str) -> WorkoutIntent | None:
    settings = get_settings()
    if not settings.groq_api_key:
        return None
    client = OpenAI(api_key=settings.groq_api_key, base_url=settings.groq_base_url)
    resp = client.chat.completions.create(
        model=settings.groq_model,
        temperature=0,
        messages=[
            {"role": "system", "content": NORMALIZE_SYSTEM},
            {
                "role": "user",
                "content": (
                    "Normalize este treino. "
                    "Se houver metros no intervalo e ritmo tipo 01:06–01:20, use pace_mode=lap_time.\n\n"
                    f"{text.strip()}"
                ),
            },
        ],
    )
    content = resp.choices[0].message.content or ""
    raw = _extract_json(content)
    # reforça warmup/cooldown pelo texto
    raw["warmup"] = bool(raw.get("warmup")) or _wants_warmup(text)
    raw["cooldown"] = bool(raw.get("cooldown")) or _wants_cooldown(text)
    return WorkoutIntent.model_validate(raw)


def normalize_intent(text: str) -> WorkoutIntent:
    """Agente 1: texto → intent estruturado (regex primeiro, LLM se precisar)."""
    intent = _parse_intent_regex(text)
    if intent and intent.reps and (intent.interval_meters or intent.interval_seconds):
        # Se regex achou intervalo mas sem pace e o texto tem ritmo, tenta LLM só pro pace
        if intent.pace_mode == "none" and re.search(r"\d{1,2}:\d{2}", text):
            llm = _parse_intent_llm(text)
            if llm and llm.pace_mode != "none":
                intent.pace_mode = llm.pace_mode
                intent.pace_fast_clock = llm.pace_fast_clock
                intent.pace_slow_clock = llm.pace_slow_clock or llm.pace_fast_clock
                if llm.workout_name and llm.workout_name != "Treino":
                    intent.workout_name = llm.workout_name
        return intent

    llm = _parse_intent_llm(text)
    if llm:
        return llm
    if intent:
        return intent
    raise ValueError(
        "Não entendi o treino. Ex.: 10x400 ritmo 01:06 a 01:20"
    )


def _target_dict(pace: PaceTarget | None) -> dict[str, Any]:
    return pace.as_dict() if pace else _no_target()


def _executable(
    *,
    order: int,
    step_key: str,
    step_id: int,
    end_key: str,
    end_id: int,
    end_value: float,
    displayable: bool = True,
    child_step_id: int | None = None,
    pace: PaceTarget | None = None,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {
            "stepTypeId": step_id,
            "stepTypeKey": step_key,
            "displayOrder": step_id,
        },
        "endCondition": {
            "conditionTypeId": end_id,
            "conditionTypeKey": end_key,
            "displayOrder": end_id,
            "displayable": displayable,
        },
        "endConditionValue": end_value,
        **_target_dict(pace),
        "preferredEndConditionUnit": None,
        "stepAudioNote": None,
        "category": None,
        "exerciseName": None,
    }
    if child_step_id is not None:
        step["childStepId"] = child_step_id
    return step


def _phase_end_from_intent(
    mode: str,
    seconds: float | None,
    meters: float | None,
) -> tuple[str, int, float]:
    """Retorna (end_key, end_id, end_value)."""
    if mode == "time" and seconds and seconds > 0:
        return "time", 2, float(seconds)
    if mode == "distance" and meters and meters > 0:
        return "distance", 3, float(meters)
    return "lap.button", 1, 1000.0


def build_workout_from_intent(intent: WorkoutIntent) -> WorkoutBody:
    """Agente 2: intent → JSON Garmin determinístico (sem LLM)."""
    if not intent.reps:
        raise ValueError("Treino precisa de repetições (ex: 10x400)")
    if not intent.interval_meters and not intent.interval_seconds:
        raise ValueError("Treino precisa de distância ou tempo no intervalo")

    pace = _resolve_pace(intent)
    steps: list[dict[str, Any]] = []
    order = 1

    if intent.warmup:
        ek, eid, ev = _phase_end_from_intent(
            intent.warmup_mode, intent.warmup_seconds, intent.warmup_meters
        )
        steps.append(
            _executable(
                order=order,
                step_key="warmup",
                step_id=1,
                end_key=ek,
                end_id=eid,
                end_value=ev,
            )
        )
        order += 1

    # Descanso parado entre aquecimento e o bloco de tiros
    if intent.pre_repeat_rest_mode in {"time", "distance"}:
        ek, eid, ev = _phase_end_from_intent(
            intent.pre_repeat_rest_mode,
            intent.pre_repeat_rest_seconds,
            intent.pre_repeat_rest_meters,
        )
        steps.append(
            _executable(
                order=order,
                step_key="rest",
                step_id=5,
                end_key=ek,
                end_id=eid,
                end_value=ev,
            )
        )
        order += 1

    if intent.interval_meters:
        interval_step = _executable(
            order=order + 1,
            step_key="interval",
            step_id=3,
            end_key="distance",
            end_id=3,
            end_value=float(intent.interval_meters),
            child_step_id=1,
            pace=pace,
        )
    else:
        interval_step = _executable(
            order=order + 1,
            step_key="interval",
            step_id=3,
            end_key="time",
            end_id=2,
            end_value=float(intent.interval_seconds or 0),
            child_step_id=1,
            pace=pace,
        )

    rek, reid, rev = _phase_end_from_intent(
        intent.recovery_mode, intent.recovery_seconds, intent.recovery_meters
    )
    recovery_step = _executable(
        order=order + 2,
        step_key="recovery",
        step_id=4,
        end_key=rek,
        end_id=reid,
        end_value=rev,
        child_step_id=1,
    )

    steps.append(
        {
            "type": "RepeatGroupDTO",
            "stepOrder": order,
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
            "numberOfIterations": int(intent.reps),
            "smartRepeat": False,
            "childStepId": 1,
            "skipLastRestStep": True,
            "endCondition": {
                "conditionTypeId": 7,
                "conditionTypeKey": "iterations",
                "displayOrder": 7,
                "displayable": False,
            },
            "workoutSteps": [interval_step, recovery_step],
        }
    )
    order += 1

    if intent.cooldown:
        ek, eid, ev = _phase_end_from_intent(
            intent.cooldown_mode, intent.cooldown_seconds, intent.cooldown_meters
        )
        steps.append(
            _executable(
                order=order,
                step_key="cooldown",
                step_id=2,
                end_key=ek,
                end_id=eid,
                end_value=ev,
            )
        )

    sport = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}
    raw = {
        "sportType": sport,
        "subSportType": None,
        "workoutName": (intent.workout_name or "Treino")[:100],
        "estimatedDistanceUnit": {"unitKey": None},
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": sport,
                "workoutSteps": steps,
            }
        ],
        "estimatedDurationInSecs": 0,
        "estimatedDistanceInMeters": 0,
        "estimateType": None,
        "isWheelchair": False,
    }
    if pace:
        raw["avgTrainingSpeed"] = (pace.fast_mps + pace.slow_mps) / 2

    return WorkoutBody.model_validate(normalize_workout_dict(raw))


def text_to_workout_body(text: str) -> WorkoutBody:
    """
    Pipeline:
      1) normalizar intent (regex → LLM)
      2) reforçar aquecimento/recuperação/desaquecimento pelo texto
      3) montar JSON Garmin determinístico
    """
    text = text.strip()
    if not text:
        raise ValueError("Texto de treino vazio")

    intent = normalize_intent(text)
    intent = _apply_phase_to_intent(intent, text)

    logger.info(
        "workout intent: reps=%s meters=%s pace=%s-%s recovery=%s/%s "
        "warmup=%s/%s pre_rest=%s/%s cooldown=%s/%s",
        intent.reps,
        intent.interval_meters,
        intent.pace_fast_clock,
        intent.pace_slow_clock,
        intent.recovery_mode,
        intent.recovery_seconds or intent.recovery_meters,
        intent.warmup_mode if intent.warmup else None,
        intent.warmup_seconds or intent.warmup_meters,
        intent.pre_repeat_rest_mode,
        intent.pre_repeat_rest_seconds or intent.pre_repeat_rest_meters,
        intent.cooldown_mode if intent.cooldown else None,
        intent.cooldown_seconds or intent.cooldown_meters,
    )
    return build_workout_from_intent(intent)
