from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings


@dataclass
class ChatCredentials:
    email: str
    password: str
    source: str = "chat"  # "chat" | "env"


class CredentialStore:
    """Armazena email/senha por chat_id do Telegram (override opcional)."""

    def __init__(self, path: Path | None = None) -> None:
        settings = get_settings()
        self.path = path or (settings.data_dir / "telegram_credentials.json")

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get(self, chat_id: int) -> ChatCredentials | None:
        row = self._read().get(str(chat_id))
        if not row or not row.get("email") or not row.get("password"):
            return None
        return ChatCredentials(email=row["email"], password=row["password"], source="chat")

    def save(self, chat_id: int, email: str, password: str) -> None:
        data = self._read()
        data[str(chat_id)] = {"email": email, "password": password}
        self._write(data)

    def delete(self, chat_id: int) -> None:
        data = self._read()
        if str(chat_id) in data:
            del data[str(chat_id)]
            self._write(data)


def resolve_garmin_credentials(chat_id: int) -> ChatCredentials | None:
    """
    1) Credenciais salvas neste chat (override via /creds)
    2) Se o chat está em TELEGRAM_ALLOWED_CHAT_IDS (ou lista vazia = aberto):
       usa GARMIN_EMAIL / GARMIN_PASSWORD do .env
    """
    stored = get_credential_store().get(chat_id)
    if stored:
        return stored

    settings = get_settings()
    allowed = settings.allowed_chat_ids
    if allowed and chat_id not in allowed:
        return None

    email = (settings.garmin_email or "").strip()
    password = settings.garmin_password or ""
    if email and password:
        return ChatCredentials(email=email, password=password, source="env")
    return None


_store: CredentialStore | None = None


def get_credential_store() -> CredentialStore:
    global _store
    if _store is None:
        _store = CredentialStore()
    return _store
