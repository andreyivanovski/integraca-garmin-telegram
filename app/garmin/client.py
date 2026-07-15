from __future__ import annotations

import time
from typing import Any

import httpx

from app.auth.garmin_auth import GarminAuth, GarminAuthError, get_auth
from app.config import get_settings


class GarminClientError(Exception):
    pass


class GarminClient:
    def __init__(self, auth: GarminAuth | None = None) -> None:
        self.auth = auth or get_auth()
        self.settings = get_settings()
        self._http = httpx.Client(timeout=60.0)

    def close(self) -> None:
        self._http.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.auth.get_access_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "GCM-Android-5.23",
            "NK": "NT",
            "DI-Backend": "connectapi.garmin.com",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.settings.garmin_connectapi}/{path.lstrip('/')}"
        last_exc: Exception | None = None
        r: httpx.Response | None = None

        for attempt in range(1, 4):
            try:
                r = self._http.request(method, url, headers=self._headers(), **kwargs)
                if r.status_code == 401:
                    self.auth.refresh()
                    r = self._http.request(method, url, headers=self._headers(), **kwargs)
                last_exc = None
                break
            except GarminAuthError:
                raise
            except (httpx.ConnectError, httpx.TimeoutException, OSError) as exc:
                last_exc = exc
                if attempt < 3:
                    time.sleep(1.5 * attempt)
                    continue
                raise GarminClientError(
                    f"Falha de rede/DNS ao falar com Garmin ({exc}). "
                    "Tente de novo; se persistir: docker compose down && docker compose up -d"
                ) from exc
            except Exception as exc:
                raise GarminClientError(str(exc)) from exc
        else:
            raise GarminClientError(str(last_exc) if last_exc else "request failed")

        assert r is not None
        if r.status_code >= 400:
            raise GarminClientError(f"{method} {path} → {r.status_code}: {r.text[:500]}")
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def list_devices(self) -> Any:
        return self._request("GET", "/device-service/deviceregistration/devices")

    def create_workout(self, body: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", "/workout-service/workout", json=body)
        if not isinstance(result, dict) or "workoutId" not in result:
            raise GarminClientError(f"Resposta inesperada ao criar treino: {result}")
        return result

    def schedule_workout(self, workout_id: int, date_str: str) -> Any:
        return self._request(
            "POST",
            f"/workout-service/schedule/{workout_id}",
            json={"date": date_str},
        )

    def send_workout_to_device(
        self,
        workout_id: int,
        device_id: int | None = None,
        workout_name: str = "Treino",
    ) -> Any:
        device_id = device_id or self.auth.get_device_id()
        payload = [
            {
                "deviceId": device_id,
                "messageUrl": f"workout-service/workout/FIT/{workout_id}",
                "messageType": "workouts",
                "messageName": workout_name,
                "groupName": None,
                "priority": 1,
                "fileType": "FIT",
                "metaDataId": workout_id,
            }
        ]
        return self._request("POST", "/device-service/devicemessage/messages", json=payload)


_client: GarminClient | None = None


def get_client() -> GarminClient:
    global _client
    if _client is None:
        _client = GarminClient()
    return _client
