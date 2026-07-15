from __future__ import annotations

import asyncio
import html
import logging
from datetime import date, timedelta

import re

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.auth.garmin_auth import GarminAuthError, get_auth
from app.config import get_settings
from app.services.workout_flow import draft_workout, execute_workout
from app.telegram.credentials import get_credential_store, resolve_garmin_credentials
from app.telegram.date_parse import parse_date_pt

logger = logging.getLogger(__name__)

(
    WAITING_EMAIL,
    WAITING_PASSWORD,
    WAITING_MFA,
    WAITING_WORKOUT,
    WAITING_DATE,
    WAITING_CONFIRM,
) = range(6)

CB_DATE_TODAY = "date:today"
CB_DATE_TOMORROW = "date:tomorrow"
CB_CONFIRM_YES = "confirm:yes"
CB_CONFIRM_NO = "confirm:no"
CB_LOGIN_START = "login:start"
CB_LOGIN_FORCE = "login:force"

BTN_NEW = "Novo treino"
BTN_STATUS = "Status"
BTN_RECONNECT = "Reconectar"

_GREETING_RE = re.compile(
    r"^\s*(oi+|ol[aá]|ola|hey|hi|hello|bom\s*dia|boa\s*tarde|boa\s*noite|"
    r"e\s*a[ií]|salve|fala|menu|in[ií]cio|comecar|começar)\s*[!?.]*\s*$",
    re.IGNORECASE,
)


def _is_greeting(text: str) -> bool:
    return bool(_GREETING_RE.match((text or "").strip()))


def _is_menu_new(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {
        BTN_NEW.lower(),
        "novo",
        "treino",
        "novo treino",
        "marcar treino",
        "criar treino",
    }


def _is_menu_status(text: str) -> bool:
    return (text or "").strip().lower() in {BTN_STATUS.lower(), "status"}


def _is_menu_reconnect(text: str) -> bool:
    return (text or "").strip().lower() in {
        BTN_RECONNECT.lower(),
        "reconectar",
        "login",
        "conectar",
    }


def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_NEW)],
            [KeyboardButton(BTN_STATUS), KeyboardButton(BTN_RECONNECT)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _allowed(update: Update) -> bool:
    settings = get_settings()
    allowed = settings.allowed_chat_ids
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not allowed:
        return True
    return chat_id in allowed


def _chat_id(update: Update) -> int | None:
    return update.effective_chat.id if update.effective_chat else None


def _esc(text: str) -> str:
    return html.escape(text or "")


def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        x in msg
        for x in (
            "não autenticado",
            "nao autenticado",
            "sessão",
            "sessao",
            "expir",
            "revog",
            "401",
            "unauthorized",
            "conecte de novo",
            "auth",
        )
    )


def _friendly_summary(summary: str) -> str:
    name = "Treino"
    bits: list[str] = []
    for line in summary.splitlines():
        line = line.strip()
        if line.startswith("Treino:"):
            name = line.split(":", 1)[1].strip() or name
        elif line.startswith("- Repeat"):
            try:
                left, right = line.split(":", 1)
                reps = left.replace("- Repeat x", "").strip()
                if "distance=" in right:
                    dist = right.split("distance=", 1)[1].split(",", 1)[0]
                    bits.append(f"{reps}×{int(float(dist))}m")
                else:
                    bits.append(f"{reps} repetições")
            except Exception:
                bits.append("intervalos")
        elif line.startswith("- warmup"):
            bits.append("aquecimento")
        elif line.startswith("- cooldown"):
            bits.append("desaquecimento")
    detail = ", ".join(bits) if bits else "corrida"
    return f"<b>{_esc(name)}</b>\n{_esc(detail)}"


def _date_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Hoje", callback_data=CB_DATE_TODAY),
                InlineKeyboardButton("Amanhã", callback_data=CB_DATE_TOMORROW),
            ]
        ]
    )


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Confirmar", callback_data=CB_CONFIRM_YES)],
            [InlineKeyboardButton("Cancelar", callback_data=CB_CONFIRM_NO)],
        ]
    )


def _reconnect_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Reconectar", callback_data=CB_LOGIN_START)]]
    )


async def _reply(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
    with_menu: bool = False,
) -> None:
    msg = update.effective_message
    if msg is None:
        return
    markup = reply_markup
    if markup is None and with_menu:
        markup = _main_keyboard()
    await msg.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=markup,
    )


async def _welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mensagem amigável + teclado; entra aguardando o treino."""
    if get_auth().needs_login():
        return await _login_with_stored(
            update,
            context,
            "Antes de marcar treino, preciso conectar no Garmin.",
        )
    await _reply(
        update,
        "Oi! Pronto quando você estiver.\n\n"
        "Manda o treino (ex: <b>10x300m</b>) ou toca em <b>Novo treino</b>.",
        with_menu=True,
    )
    return WAITING_WORKOUT


async def _try_delete(update: Update) -> None:
    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass


async def _after_auth_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Depois do login/MFA: retoma treino pendente ou pede o treino."""
    if context.user_data.get("workout_body") and context.user_data.get("date"):
        await _reply(update, "Conectado de novo.", with_menu=True)
        return await _ask_confirm(update, context, context.user_data["date"])

    pending = context.user_data.pop("pending_workout_text", None)
    await _reply(update, "Conectado.", with_menu=True)
    if pending and update.message:
        update.message.text = pending
        return await receive_workout(update, context)

    await _reply(update, "Pode mandar o treino (ex: <b>10x300m</b>).", with_menu=True)
    return WAITING_WORKOUT


def _force_login_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Forçar novo login", callback_data=CB_LOGIN_FORCE)]]
    )


def _is_rate_limit_error(exc: Exception) -> bool:
    return "429" in str(exc)


async def _login_with_stored(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    reason: str = "",
    *,
    force: bool = False,
) -> int:
    """Usa .env (chat liberado) ou credenciais salvas; pede MFA se necessário."""
    cid = _chat_id(update)
    if cid is None:
        return ConversationHandler.END

    auth = get_auth()

    # Já tem sessão → não bate na SSO de novo (evita 429)
    if not force and not auth.needs_login():
        prefix = f"{reason}\n\n" if reason else ""
        await _reply(
            update,
            f"{prefix}"
            "Já está conectado na Garmin — pode mandar o treino.\n"
            "Só force login se a sessão estiver estranha.",
            reply_markup=_force_login_keyboard(),
            with_menu=True,
        )
        return WAITING_WORKOUT

    cooldown = auth.login_cooldown_seconds()
    if cooldown > 0:
        mins = max(1, (cooldown + 59) // 60)
        hint = ""
        if not auth.needs_login():
            hint = "\n\nA sessão atual ainda serve — manda o treino normalmente."
        await _reply(
            update,
            f"Garmin bloqueou login por tentativas demais (429).\n"
            f"Espera ~<b>{mins} min</b> e tenta de novo.{hint}",
            with_menu=True,
        )
        return WAITING_WORKOUT if not auth.needs_login() else ConversationHandler.END

    creds = resolve_garmin_credentials(cid)
    if not creds:
        prefix = f"{reason}\n\n" if reason else ""
        await _reply(
            update,
            f"{prefix}"
            "Não achei login Garmin.\n"
            "Coloque <b>GARMIN_EMAIL</b> e <b>GARMIN_PASSWORD</b> no <code>.env</code> "
            "deste app, ou manda o <b>e-mail</b> aqui pra salvar neste chat.",
        )
        return WAITING_EMAIL

    prefix = f"{reason}\n\n" if reason else ""
    origem = "do .env" if creds.source == "env" else "salvo neste chat"
    await _reply(update, f"{prefix}Reconectando ({origem})…")
    try:
        result = await asyncio.to_thread(auth.start_login, creds.email, creds.password)
    except GarminAuthError as exc:
        if _is_rate_limit_error(exc):
            await _reply(
                update,
                f"{_esc(str(exc))}\n\n"
                "Não fica tentando agora — piora o bloqueio.\n"
                "Se Status mostrar conectado, manda o treino direto.",
                with_menu=True,
            )
            return WAITING_WORKOUT if not get_auth().needs_login() else ConversationHandler.END
        if creds.source == "env":
            await _reply(
                update,
                f"Não consegui entrar com o login do <code>.env</code>.\n"
                f"{_esc(str(exc))}\n\n"
                "Confere email/senha no .env, ou manda o <b>e-mail</b> pra sobrescrever neste chat.",
            )
        else:
            await _reply(
                update,
                f"Não consegui entrar com a conta salva.\n"
                f"{_esc(str(exc))}\n\n"
                "Manda o <b>e-mail</b> de novo pra atualizar o login.",
            )
        return WAITING_EMAIL
    except Exception as exc:
        logger.exception("stored login failed")
        await _reply(update, f"Falhou a reconexão. Tenta /login.\n<i>{_esc(str(exc)[:120])}</i>")
        return ConversationHandler.END

    if result.get("status") == "mfa_required":
        await _reply(update, "Te mandei um código. Cola o MFA aqui.")
        return WAITING_MFA

    return await _after_auth_ok(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _allowed(update):
        await _reply(update, "Esse chat não está liberado.")
        return ConversationHandler.END

    pending = context.user_data.get("pending_workout_text")
    body = context.user_data.get("workout_body")
    date_val = context.user_data.get("date")
    context.user_data.clear()
    if pending:
        context.user_data["pending_workout_text"] = pending
    if body:
        context.user_data["workout_body"] = body
    if date_val:
        context.user_data["date"] = date_val

    return await _welcome(update, context)


async def entry_any_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entrada sem /start: oi, botões do menu, ou já o texto do treino."""
    if not _allowed(update):
        await _reply(update, "Esse chat não está liberado.")
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if _is_menu_status(text):
        await status_cmd(update, context)
        return WAITING_WORKOUT if not get_auth().needs_login() else ConversationHandler.END
    if _is_menu_reconnect(text):
        return await _login_with_stored(update, context)
    if _is_greeting(text) or _is_menu_new(text):
        context.user_data.clear()
        return await _welcome(update, context)

    # Qualquer outra mensagem: assume treino
    context.user_data.clear()
    return await receive_workout(update, context)


async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _allowed(update):
        await _reply(update, "Esse chat não está liberado.")
        return ConversationHandler.END
    # /login sem force só reconecta se precisar
    return await _login_with_stored(update, context, force=False)


async def creds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Override opcional: salva email+senha neste chat (senão usa o .env)."""
    if not _allowed(update):
        await _reply(update, "Esse chat não está liberado.")
        return ConversationHandler.END
    await _reply(
        update,
        "Por padrão uso o login do <code>.env</code>.\n"
        "Se quiser um override só neste chat, manda o <b>e-mail</b>.",
    )
    return WAITING_EMAIL


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        await _reply(update, "Esse chat não está liberado.")
        return
    cid = _chat_id(update)
    if cid is not None:
        get_credential_store().delete(cid)
    get_auth().clear()
    await _reply(
        update,
        "Apaguei a sessão Garmin e o override deste chat.\n"
        "O login do <code>.env</code> continua; use Reconectar quando quiser.",
        with_menu=True,
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        await _reply(update, "Esse chat não está liberado.")
        return
    cid = _chat_id(update)
    creds = resolve_garmin_credentials(cid) if cid else None
    st = get_auth().status()
    if st["authenticated"]:
        extra = ""
        if creds and creds.source == "env":
            extra = " Login via .env."
        elif creds and creds.source == "chat":
            extra = " Login override neste chat."
        await _reply(
            update,
            f"Tudo certo — Garmin conectado.{extra}",
            with_menu=True,
        )
    else:
        msg = "Garmin desconectado."
        if creds and creds.source == "env":
            msg += " Posso reconectar com o .env (só peço MFA)."
        elif creds:
            msg += " Posso reconectar pedindo só o MFA."
        else:
            msg += " Falta GARMIN_EMAIL/PASSWORD no .env."
        await _reply(update, msg, reply_markup=_reconnect_keyboard())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await _reply(
        update,
        "Cancelei. Quando quiser, manda <b>oi</b> ou toca em Novo treino.",
        with_menu=True,
    )
    return ConversationHandler.END


async def login_start_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    force = bool(query and query.data == CB_LOGIN_FORCE)
    if query:
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    return await _login_with_stored(update, context, force=force)


async def receive_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _allowed(update):
        return ConversationHandler.END
    email = (update.message.text or "").strip()
    if "@" not in email or "." not in email:
        await _reply(update, "Esse e-mail não parece válido. Manda de novo?")
        return WAITING_EMAIL
    context.user_data["login_email"] = email
    await _reply(
        update,
        "Agora a <b>senha</b>.\n"
        "Vou guardar neste chat pra próxima vez pedir só o MFA.",
    )
    return WAITING_PASSWORD


async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _allowed(update):
        return ConversationHandler.END
    password = (update.message.text or "").strip()
    await _try_delete(update)
    if not password:
        await _reply(update, "Senha vazia. Manda de novo?")
        return WAITING_PASSWORD

    email = context.user_data.get("login_email")
    cid = _chat_id(update)
    if not email or cid is None:
        await _reply(update, "Vamos recomeçar. Qual o <b>e-mail</b>?")
        return WAITING_EMAIL

    get_credential_store().save(cid, email, password)

    auth = get_auth()
    cooldown = auth.login_cooldown_seconds()
    if cooldown > 0:
        mins = max(1, (cooldown + 59) // 60)
        await _reply(
            update,
            f"Login salvo, mas a Garmin ainda está em cooldown (~{mins} min).\n"
            "Depois toca em <b>Reconectar</b> — sem ficar tentando agora.",
            with_menu=True,
        )
        return WAITING_WORKOUT if not auth.needs_login() else ConversationHandler.END

    await _reply(update, "Login salvo. Entrando na Garmin…")

    try:
        result = await asyncio.to_thread(auth.start_login, email, password)
    except GarminAuthError as exc:
        if _is_rate_limit_error(exc):
            await _reply(
                update,
                f"{_esc(str(exc))}\n\n"
                "Login ficou salvo. Espera o cooldown e toca Reconectar.",
                with_menu=True,
            )
            return WAITING_WORKOUT if not get_auth().needs_login() else ConversationHandler.END
        await _reply(
            update,
            f"Não consegui entrar: {_esc(str(exc))}\n\nManda o e-mail de novo.",
        )
        return WAITING_EMAIL
    except Exception as exc:
        logger.exception("login failed")
        await _reply(update, f"Falhou o login. /login de novo.\n<i>{_esc(str(exc)[:120])}</i>")
        return ConversationHandler.END

    if result.get("status") == "mfa_required":
        await _reply(update, "Te mandei um código. Cola o MFA aqui.")
        return WAITING_MFA

    return await _after_auth_ok(update, context)


async def receive_mfa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _allowed(update):
        return ConversationHandler.END
    code = (update.message.text or "").strip()
    await _try_delete(update)
    if not code:
        await _reply(update, "Manda o código MFA.")
        return WAITING_MFA

    auth = get_auth()
    try:
        await asyncio.to_thread(auth.complete_mfa, code)
    except GarminAuthError as exc:
        await _reply(
            update,
            f"Código não rolou: {_esc(str(exc))}\nTenta outro, ou /login.",
        )
        return WAITING_MFA
    except Exception as exc:
        logger.exception("mfa failed")
        await _reply(update, f"Falhou a confirmação. /login.\n<i>{_esc(str(exc)[:120])}</i>")
        return ConversationHandler.END

    return await _after_auth_ok(update, context)


async def receive_workout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _allowed(update):
        await _reply(update, "Esse chat não está liberado.")
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        await _reply(update, "Manda o treino, tipo: <b>10x150m</b>", with_menu=True)
        return WAITING_WORKOUT

    # Botões / saudações dentro do fluxo — não tratam como treino
    if _is_menu_status(text):
        await status_cmd(update, context)
        return WAITING_WORKOUT
    if _is_menu_reconnect(text):
        return await _login_with_stored(update, context)
    if _is_greeting(text) or _is_menu_new(text):
        return await _welcome(update, context)

    if get_auth().needs_login():
        context.user_data["pending_workout_text"] = text
        return await _login_with_stored(
            update,
            context,
            "Sessão Garmin expirou. Vou usar o login salvo deste chat.",
        )

    await _reply(update, "Beleza, montando o treino…")
    try:
        draft = await asyncio.to_thread(draft_workout, text)
    except Exception as exc:
        logger.exception("draft failed")
        await _reply(
            update,
            "Não consegui entender esse treino. Tenta de outro jeito?\n"
            f"<i>{_esc(str(exc)[:120])}</i>",
            with_menu=True,
        )
        return WAITING_WORKOUT

    context.user_data["workout_body"] = draft.workout_body
    context.user_data["summary"] = draft.summary
    await _reply(
        update,
        f"Ficou assim:\n{_friendly_summary(draft.summary)}\n\n"
        "Qual dia você quer?",
        reply_markup=_date_keyboard(),
    )
    return WAITING_DATE


async def _ask_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, date_str: str) -> int:
    context.user_data["date"] = date_str
    summary = context.user_data.get("summary", "")
    try:
        nice = date.fromisoformat(date_str).strftime("%d/%m/%Y")
    except ValueError:
        nice = date_str

    await _reply(
        update,
        f"{_friendly_summary(summary)}\n\n"
        f"Dia: <b>{_esc(nice)}</b>\n\n"
        "Posso criar, agendar e mandar pro relógio?",
        reply_markup=_confirm_keyboard(),
    )
    return WAITING_CONFIRM


async def receive_date_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _allowed(update):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if _is_greeting(text) or _is_menu_new(text):
        context.user_data.clear()
        return await _welcome(update, context)

    if "workout_body" not in context.user_data:
        await _reply(
            update,
            "Perdi o contexto. Manda <b>oi</b> ou toca em Novo treino.",
            with_menu=True,
        )
        return WAITING_WORKOUT

    try:
        d = parse_date_pt(text)
    except ValueError:
        await _reply(
            update,
            "Não peguei essa data. Usa os botões ou algo tipo <b>amanhã</b>.",
            reply_markup=_date_keyboard(),
        )
        return WAITING_DATE

    return await _ask_confirm(update, context, d.isoformat())


async def receive_date_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None:
        return WAITING_DATE
    await query.answer()

    if not _allowed(update) or "workout_body" not in context.user_data:
        await query.message.reply_text(
            "Perdi o contexto. Manda oi ou toca em Novo treino.",
            reply_markup=_main_keyboard(),
        )
        return WAITING_WORKOUT

    today = date.today()
    if query.data == CB_DATE_TODAY:
        d = today
    elif query.data == CB_DATE_TOMORROW:
        d = today + timedelta(days=1)
    else:
        return WAITING_DATE

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    return await _ask_confirm(update, context, d.isoformat())


async def receive_confirm_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None:
        return WAITING_CONFIRM
    await query.answer()

    if not _allowed(update):
        return ConversationHandler.END

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if query.data == CB_CONFIRM_NO:
        context.user_data.clear()
        await query.message.reply_text(
            "Ok, cancelei. Manda oi ou toca em Novo treino.",
            reply_markup=_main_keyboard(),
        )
        return WAITING_WORKOUT

    if query.data != CB_CONFIRM_YES:
        return WAITING_CONFIRM

    body = context.user_data.get("workout_body")
    date_str = context.user_data.get("date")
    if not body or not date_str:
        await query.message.reply_text(
            "Perdi o contexto. Manda oi ou toca em Novo treino.",
            reply_markup=_main_keyboard(),
        )
        context.user_data.clear()
        return WAITING_WORKOUT

    if get_auth().needs_login():
        return await _login_with_stored(
            update,
            context,
            "Sessão Garmin caiu.",
        )

    await query.message.reply_text("Beleza, enviando pro Garmin…")

    try:
        result = await asyncio.to_thread(execute_workout, body, date_str)
    except Exception as exc:
        logger.exception("execute failed")
        if _is_auth_error(exc):
            get_auth().clear()
            return await _login_with_stored(
                update,
                context,
                "Sessão Garmin expirou.",
            )
        await query.message.reply_text("Não rolou enviar agora. Tenta de novo em instantes.")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        nice = date.fromisoformat(result.date).strftime("%d/%m/%Y")
    except ValueError:
        nice = result.date

    context.user_data.clear()
    await query.message.reply_text(
        f"Pronto! <b>{_esc(result.workout_name)}</b> agendado pra <b>{_esc(nice)}</b>.\n\n"
        "Só sincronizar o relógio que ele aparece lá.",
        parse_mode=ParseMode.HTML,
        reply_markup=_main_keyboard(),
    )
    return WAITING_WORKOUT


def build_application() -> Application | None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN vazio — bot desligado.")
        return None

    app = Application.builder().token(settings.telegram_bot_token).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("treino", start),
            CommandHandler("login", login_cmd),
            CommandHandler("creds", creds_cmd),
            CallbackQueryHandler(login_start_button, pattern=r"^login:(start|force)$"),
            # Qualquer texto inicia: oi, botões, ou já o treino
            MessageHandler(filters.TEXT & ~filters.COMMAND, entry_any_text),
        ],
        states={
            WAITING_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_email),
            ],
            WAITING_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password),
            ],
            WAITING_MFA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mfa),
            ],
            WAITING_WORKOUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_workout),
            ],
            WAITING_DATE: [
                CallbackQueryHandler(receive_date_button, pattern=r"^date:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_date_text),
            ],
            WAITING_CONFIRM: [
                CallbackQueryHandler(receive_confirm_button, pattern=r"^confirm:"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            # Se travar no meio do fluxo, "oi" / Novo treino reinicia
            MessageHandler(
                filters.Regex(
                    re.compile(
                        r"^\s*(oi+|ol[aá]|ola|novo\s*treino|menu|in[ií]cio)\s*$",
                        re.IGNORECASE,
                    )
                ),
                start,
            ),
        ],
        # False: senão qualquer texto reinicia e quebra escolha de data/confirmação
        allow_reentry=False,
        per_chat=True,
        per_user=True,
    )
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(conv)
    return app


async def run_bot_polling(application: Application) -> None:
    """Long-poll com timeout alto = menos requests/CPU quando ocioso."""
    from telegram.error import Conflict, NetworkError, TimedOut

    async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if isinstance(err, Conflict):
            logger.error(
                "Conflict: outra instância está fazendo getUpdates com este token. "
                "Deixe só 1 container/réplica rodando. Aguardando 90s…"
            )
            await asyncio.sleep(90)
            return
        if isinstance(err, (TimedOut, NetworkError)):
            logger.warning("Telegram rede: %s", err)
            return
        logger.exception("Erro no bot Telegram: %s", err)

    application.add_error_handler(_on_error)
    await application.initialize()
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.start()
    await application.updater.start_polling(
        drop_pending_updates=True,
        poll_interval=1.0,
        timeout=50,
        bootstrap_retries=-1,
    )
    logger.info("Telegram bot polling started (timeout=50s)")
