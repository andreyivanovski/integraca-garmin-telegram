from __future__ import annotations

import base64
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from curl_cffi import requests

from app.config import Settings, get_settings

IMPERSONATE = "chrome131"
DI_GRANT = "https://connectapi.garmin.com/di-oauth2-service/oauth/grant/service_ticket"
DI_CLIENT_IDS = (
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2024Q4",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI",
    "GARMIN_CONNECT_MOBILE_IOS_DI",
)

# Mobile SSO evita CAPTCHA do portal web
MOBILE_CLIENT_ID = "GCM_ANDROID_DARK"
MOBILE_SERVICE_URL = "https://mobile.integration.garmin.com/gcm/android"
PORTAL_CLIENT_ID = "GarminConnect"
PORTAL_SERVICE_URL = "https://connect.garmin.com/app"

SERVICE_URL_CANDIDATES = (
    MOBILE_SERVICE_URL,
    PORTAL_SERVICE_URL,
    "https://sso.garmin.com/sso/embed",
)


class GarminAuthError(Exception):
    pass


def _basic_auth(client_id: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:".encode()).decode()


@dataclass
class TokenStore:
    access_token: str
    refresh_token: str | None
    di_client_id: str
    email: str | None = None
    device_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "diClientId": self.di_client_id,
            "diBasicAuth": _basic_auth(self.di_client_id),
            "username": self.email,
            "deviceId": str(self.device_id) if self.device_id is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenStore:
        device = data.get("deviceId")
        return cls(
            access_token=data["accessToken"],
            refresh_token=data.get("refreshToken"),
            di_client_id=data.get("diClientId") or DI_CLIENT_IDS[0],
            email=data.get("username"),
            device_id=int(device) if device not in (None, "") else None,
        )


_pending: dict[str, Any] = {}
_RATE_LIMIT_COOLDOWN_SEC = 20 * 60  # 20 min padrão após 429


def _rate_limit_path(settings: Settings) -> Path:
    return settings.data_dir / "garmin_login_cooldown.json"


def _load_rate_limited_until(settings: Settings) -> float:
    path = _rate_limit_path(settings)
    if not path.exists():
        return 0.0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data.get("until", 0))
    except Exception:
        return 0.0


def _save_rate_limited_until(settings: Settings, until: float) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _rate_limit_path(settings).write_text(
        json.dumps({"until": until}, indent=2),
        encoding="utf-8",
    )


def _clear_rate_limit(settings: Settings) -> None:
    path = _rate_limit_path(settings)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


class GarminAuth:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._tokens: TokenStore | None = None
        self.load()

    @property
    def tokens_path(self) -> Path:
        return self.settings.tokens_path

    def load(self) -> TokenStore | None:
        path = self.tokens_path
        if not path.exists():
            self._tokens = None
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        self._tokens = TokenStore.from_dict(data)
        return self._tokens

    def save(self, tokens: TokenStore) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        if tokens.device_id is None:
            tokens.device_id = self.settings.default_device_id
        self.tokens_path.write_text(json.dumps(tokens.to_dict(), indent=2), encoding="utf-8")
        self._tokens = tokens

    @property
    def is_authenticated(self) -> bool:
        return bool(self._tokens and self._tokens.access_token)

    def status(self) -> dict[str, Any]:
        t = self._tokens
        return {
            "authenticated": self.is_authenticated,
            "email": t.email if t else None,
            "device_id": t.device_id if t else self.settings.default_device_id,
            "has_refresh": bool(t and t.refresh_token),
        }

    def get_access_token(self) -> str:
        if not self._tokens:
            raise GarminAuthError("Não autenticado. Faça o setup MFA em /setup.")
        return self._tokens.access_token

    def get_device_id(self) -> int:
        if self._tokens and self._tokens.device_id:
            return self._tokens.device_id
        return self.settings.default_device_id

    def set_device_id(self, device_id: int) -> None:
        if not self._tokens:
            raise GarminAuthError("Não autenticado")
        self._tokens.device_id = device_id
        self.save(self._tokens)

    def login_cooldown_seconds(self) -> int:
        """Segundos restantes do bloqueio pós-429 (0 = liberado)."""
        until = _load_rate_limited_until(self.settings)
        left = int(until - time.time())
        return max(0, left)

    def _raise_if_rate_limited(self) -> None:
        left = self.login_cooldown_seconds()
        if left <= 0:
            return
        mins = max(1, (left + 59) // 60)
        raise GarminAuthError(
            f"Garmin ainda bloqueando login (429). Espera ~{mins} min e tenta de novo."
        )

    def _mark_rate_limited(self, response: Any | None = None) -> None:
        seconds = _RATE_LIMIT_COOLDOWN_SEC
        if response is not None:
            retry = response.headers.get("Retry-After") or response.headers.get("retry-after")
            if retry:
                try:
                    seconds = max(seconds, int(retry))
                except ValueError:
                    pass
        _save_rate_limited_until(self.settings, time.time() + seconds)

    def start_login(self, email: str, password: str) -> dict[str, Any]:
        """Login via mobile SSO (sem CAPTCHA do portal web)."""
        self._raise_if_rate_limited()

        sso = self.settings.garmin_sso
        sess = requests.Session(impersonate=IMPERSONATE)
        login_params = {
            "clientId": MOBILE_CLIENT_ID,
            "locale": self.settings.garmin_locale,
            "service": MOBILE_SERVICE_URL,
        }
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": sso,
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Mobile Safari/537.36"
            ),
        }

        # pequena pausa anti-WAF
        time.sleep(random.uniform(2, 5))

        r = sess.post(
            f"{sso}/mobile/api/login",
            params=login_params,
            headers=headers,
            json={
                "username": email,
                "password": password,
                "rememberMe": True,
                "captchaToken": "",
            },
            timeout=60,
        )
        if r.status_code == 429:
            self._mark_rate_limited(r)
            mins = max(1, (self.login_cooldown_seconds() + 59) // 60)
            raise GarminAuthError(
                f"Rate limit Garmin (429). A SSO bloqueou tentativas — "
                f"espera ~{mins} min antes de tentar de novo."
            )
        if r.status_code == 403 or "Just a moment" in r.text:
            raise GarminAuthError("Cloudflare bloqueou o login mobile. Tente novamente.")

        try:
            data = r.json()
        except Exception as exc:
            raise GarminAuthError(f"Login não-JSON ({r.status_code})") from exc

        status = data.get("responseStatus", {}).get("type")
        if status == "MFA_REQUIRED":
            method = data.get("customerMfaInfo", {}).get("mfaLastMethodUsed", "email")
            _pending.clear()
            _pending.update(
                {
                    "session": sess,
                    "login_params": login_params,
                    "headers": headers,
                    "email": email,
                    "mfa_method": method,
                    "service_url": MOBILE_SERVICE_URL,
                    "api_base": "mobile",
                }
            )
            return {"status": "mfa_required", "mfa_method": method}

        if status == "CAPTCHA_REQUIRED":
            raise GarminAuthError(
                "CAPTCHA_REQUIRED no mobile. Use ticket fresco do Network (serviceTicketId) "
                "antes do redirect, ou tente de novo em alguns minutos."
            )

        if status == "SUCCESSFUL":
            _clear_rate_limit(self.settings)
            ticket = data["serviceTicketId"]
            tokens = self._exchange_ticket(ticket, email=email, service_url=MOBILE_SERVICE_URL)
            self.save(tokens)
            return {"status": "ok", **self.status()}

        raise GarminAuthError(f"Login falhou: {status or data}")

    def complete_mfa(self, code: str) -> dict[str, Any]:
        sess = _pending.get("session")
        if sess is None:
            raise GarminAuthError("Sessão MFA expirada. Faça login novamente.")

        login_params = _pending["login_params"]
        headers = dict(_pending["headers"])
        email = _pending["email"]
        method = _pending.get("mfa_method", "email")
        service_url = _pending.get("service_url", MOBILE_SERVICE_URL)
        api_base = _pending.get("api_base", "mobile")
        sso = self.settings.garmin_sso

        r = sess.post(
            f"{sso}/{api_base}/api/mfa/verifyCode",
            params=login_params,
            headers=headers,
            json={
                "mfaMethod": method,
                "mfaVerificationCode": code.strip(),
                "rememberMyBrowser": True,
                "reconsentList": [],
                "mfaSetup": False,
            },
            timeout=60,
        )
        try:
            data = r.json()
        except Exception as exc:
            raise GarminAuthError(f"MFA não-JSON ({r.status_code}): {r.text[:200]}") from exc

        status = data.get("responseStatus", {}).get("type")
        if status != "SUCCESSFUL":
            raise GarminAuthError(f"MFA falhou: {status or data}")

        ticket = data["serviceTicketId"]
        tokens = self._exchange_ticket(ticket, email=email, service_url=service_url)
        self.save(tokens)
        _pending.clear()
        return {"status": "ok", **self.status()}

    def login_with_ticket(self, ticket: str, email: str | None = None) -> dict[str, Any]:
        ticket = ticket.strip()
        # Aceita URL cola completa
        if "ticket=" in ticket:
            ticket = ticket.split("ticket=", 1)[1].split("&", 1)[0].strip()

        last_err = ""
        for service_url in SERVICE_URL_CANDIDATES:
            try:
                tokens = self._exchange_ticket(ticket, email=email, service_url=service_url)
                self.save(tokens)
                return {"status": "ok", **self.status()}
            except GarminAuthError as exc:
                last_err = str(exc)
                continue
        raise GarminAuthError(
            f"{last_err} — Ticket da URL do browser já foi CONSUMIDO "
            "(one-time). Prefira Login automático + MFA nesta página "
            "(fluxo mobile, sem CAPTCHA)."
        )

    def _exchange_ticket(
        self,
        ticket: str,
        email: str | None = None,
        service_url: str | None = None,
    ) -> TokenStore:
        svc = service_url or MOBILE_SERVICE_URL
        errors: list[str] = []
        for client_id in DI_CLIENT_IDS:
            r = requests.post(
                f"{self.settings.garmin_diauth}/di-oauth2-service/oauth/token",
                impersonate=IMPERSONATE,
                headers={
                    "Authorization": _basic_auth(client_id),
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "GCM-Android-5.23",
                },
                data={
                    "client_id": client_id,
                    "service_ticket": ticket,
                    "grant_type": DI_GRANT,
                    "service_url": svc,
                },
                timeout=60,
            )
            if r.ok:
                tok = r.json()
                return TokenStore(
                    access_token=tok["access_token"],
                    refresh_token=tok.get("refresh_token"),
                    di_client_id=client_id,
                    email=email,
                    device_id=self.settings.default_device_id,
                )
            errors.append(f"{client_id}: {r.status_code} {r.text[:120]}")

        raise GarminAuthError(
            "Falha ao trocar service ticket por DI token. "
            + (errors[0] if errors else "")
        )

    def clear(self) -> None:
        self._tokens = None
        if self.tokens_path.exists():
            try:
                self.tokens_path.unlink()
            except OSError:
                pass

    def needs_login(self) -> bool:
        return not self.is_authenticated

    def refresh(self) -> TokenStore:
        if not self._tokens or not self._tokens.refresh_token:
            self.clear()
            raise GarminAuthError("Sessão expirada. Conecte de novo pelo Telegram (/login).")
        client_id = self._tokens.di_client_id
        r = requests.post(
            f"{self.settings.garmin_diauth}/di-oauth2-service/oauth/token",
            impersonate=IMPERSONATE,
            headers={
                "Authorization": _basic_auth(client_id),
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "GCM-Android-5.23",
            },
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": self._tokens.refresh_token,
            },
            timeout=60,
        )
        if not r.ok:
            self.clear()
            raise GarminAuthError(
                "Sessão Garmin expirou ou foi revogada. "
                "Conecte de novo pelo Telegram com /login."
            )
        tok = r.json()
        self._tokens.access_token = tok["access_token"]
        if tok.get("refresh_token"):
            self._tokens.refresh_token = tok["refresh_token"]
        self.save(self._tokens)
        return self._tokens


_auth: GarminAuth | None = None


def get_auth() -> GarminAuth:
    global _auth
    if _auth is None:
        _auth = GarminAuth()
    return _auth
