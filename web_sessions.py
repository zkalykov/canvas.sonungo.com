"""
Signed-in web dashboard sessions, so they can be seen and logged out from Telegram.

- When a login is approved and the dashboard picks up the credentials,
  create_session() stores a session (web_sessions/{id}) and returns its id and a
  secret. The dashboard keeps both in its encrypted cookie.
- The dashboard asks check_session() whether the session is still active (at most
  about once a minute per server). That also records when it was last used.
- /sessions in the bot lists the active sessions with Log out buttons.
- A session ends when the user logs it out in Telegram, logs out on the website,
  doesn't use it for an hour (IDLE_SECONDS), changes their Canvas token, or
  deletes their data.
- cleanup_login_records() (run by the scheduled /check_reminder job) deletes old
  one-time codes, login requests and sessions.
"""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from google.cloud.firestore import FieldFilter
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from db import get_db

IDLE_SECONDS = 60 * 60
# How long ended sessions and old login records are kept before cleanup.
KEEP_SESSIONS_DAYS = 30
KEEP_REQUESTS_DAYS = 1
# last_seen is written at most this often, to keep Firestore writes low.
TOUCH_SECONDS = 60
END_PREFIX = "SESSION_END_"
END_ALL = "SESSION_END_ALL"
ACCESS_LABEL = {"view": "View only", "full": "Full access"}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_session(user_id: str, access: str, device: str = "", location: str = "", ip: str = "", request_id: str = "") -> dict:
    session_id = uuid.uuid4().hex
    secret = secrets.token_urlsafe(32)
    now = _now()
    get_db().collection("web_sessions").document(session_id).set({
        "user": str(user_id),
        "status": "active",
        "secret_hash": _hash(secret),
        "access": access if access in ("view", "full") else "view",
        "device": device,
        "location": location,
        "ip": ip,
        "request_id": request_id,
        "created_at": now,
        "last_seen": now,
    })
    return {"session_id": session_id, "session_secret": secret}


def _load_owned(session_id: str, secret: str):
    """The session document, if the secret matches."""
    if not session_id or not secret:
        raise HTTPException(status_code=400, detail="Missing session_id or session_secret")
    ref = get_db().collection("web_sessions").document(session_id)
    doc = ref.get()
    if not doc.exists:
        return ref, None
    data = doc.to_dict()
    if not secrets.compare_digest(data.get("secret_hash", ""), _hash(secret)):
        raise HTTPException(status_code=403, detail="Forbidden")
    return ref, data


def _is_idle(data: dict, now: datetime) -> bool:
    last_seen = data.get("last_seen") or data.get("created_at")
    return bool(last_seen and now - last_seen > timedelta(seconds=IDLE_SECONDS))


def check_session(session_id: str, secret: str) -> dict:
    """{"status": "active"} or {"status": "ended"}; marks the session as used."""
    ref, data = _load_owned(session_id, secret)
    if data is None or data.get("status") != "active":
        return {"status": "ended"}
    now = _now()
    if _is_idle(data, now):
        ref.update({"status": "ended", "ended_reason": "idle", "ended_at": now})
        return {"status": "ended"}
    last_seen = data.get("last_seen")
    if not last_seen or now - last_seen > timedelta(seconds=TOUCH_SECONDS):
        ref.update({"last_seen": now})
    return {"status": "active"}


def end_session(session_id: str, secret: str) -> dict:
    """The user logged out on the website."""
    ref, data = _load_owned(session_id, secret)
    if data is not None and data.get("status") == "active":
        ref.update({"status": "ended", "ended_reason": "website", "ended_at": _now()})
    return {"status": "ended"}


def end_all_sessions(user_id: str, reason: str) -> int:
    """Logs out every active website session of a user. Returns how many."""
    now = _now()
    ended = 0
    for doc in (
        get_db()
        .collection("web_sessions")
        .where(filter=FieldFilter("user", "==", str(user_id)))
        .where(filter=FieldFilter("status", "==", "active"))
        .stream()
    ):
        doc.reference.update({"status": "ended", "ended_reason": reason, "ended_at": now})
        ended += 1
    return ended


def delete_login_data(user_id: str) -> int:
    """'Delete my data': removes the user's sessions, login requests and codes (sessions end at once)."""
    db = get_db()
    deleted = 0
    for collection in ("web_sessions", "login_requests", "auth_codes"):
        for doc in db.collection(collection).where(filter=FieldFilter("user", "==", str(user_id))).stream():
            doc.reference.delete()
            deleted += 1
    return deleted


def cleanup_login_records(limit: int = 500) -> dict:
    """Deletes expired one-time codes and login requests, and sessions unused for a month."""
    db = get_db()
    now = _now()
    plan = (
        ("auth_codes", "expires_at", now - timedelta(days=KEEP_REQUESTS_DAYS)),
        ("login_requests", "expires_at", now - timedelta(days=KEEP_REQUESTS_DAYS)),
        ("web_sessions", "last_seen", now - timedelta(days=KEEP_SESSIONS_DAYS)),
    )
    counts = {}
    for collection, field, cutoff in plan:
        n = 0
        for doc in db.collection(collection).where(filter=FieldFilter(field, "<", cutoff)).limit(limit).stream():
            doc.reference.delete()
            n += 1
        counts[collection] = n
    return counts


# ---------------------------------------------------------------------------
# Telegram: /sessions
# ---------------------------------------------------------------------------

def _active_sessions(user_id: str) -> list:
    """Active sessions of a user, newest first. Idle ones are ended on the way."""
    now = _now()
    docs = (
        get_db()
        .collection("web_sessions")
        .where(filter=FieldFilter("user", "==", str(user_id)))
        .where(filter=FieldFilter("status", "==", "active"))
        .stream()
    )
    active = []
    for doc in docs:
        data = doc.to_dict()
        if _is_idle(data, now):
            doc.reference.update({"status": "ended", "ended_reason": "idle", "ended_at": now})
            continue
        active.append((doc.id, data))
    active.sort(key=lambda item: item[1].get("created_at") or now, reverse=True)
    return active


def _ago(moment: datetime | None, now: datetime) -> str:
    if not moment:
        return "unknown"
    minutes = int((now - moment).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"


def _local_time(moment: datetime | None, time_zone: str | None) -> str:
    if not moment:
        return "unknown"
    try:
        moment = moment.astimezone(ZoneInfo(time_zone or "America/Chicago"))
    except Exception:
        pass
    return moment.strftime("%b %d, %I:%M %p").replace(" 0", " ")


def _sessions_view(user_id: str) -> tuple[str, InlineKeyboardMarkup | None]:
    sessions = _active_sessions(user_id)
    if not sessions:
        return "No active sessions on Canvas Dashboard.\n\nSend /portal to log in.", None

    user_doc = get_db().collection("users").document(str(user_id)).get()
    time_zone = user_doc.to_dict().get("time_zone") if user_doc.exists else None
    now = _now()
    lines = [f"Active sessions on Canvas Dashboard ({len(sessions)}):"]
    buttons = []
    for number, (session_id, data) in enumerate(sessions, start=1):
        device = data.get("device") or "A web browser"
        details = [ACCESS_LABEL.get(data.get("access"), "View only")]
        if data.get("location"):
            details.append(data["location"])
        if data.get("ip"):
            details.append(data["ip"])
        lines.append(
            f"\n{number}. {device}\n"
            f"   {' · '.join(details)}\n"
            f"   Logged in {_local_time(data.get('created_at'), time_zone)} · active {_ago(data.get('last_seen'), now)}"
        )
        buttons.append([InlineKeyboardButton(f"Log out {number}: {device}"[:60], callback_data=f"{END_PREFIX}{session_id}")])
    if len(sessions) > 1:
        buttons.append([InlineKeyboardButton("Log out all", callback_data=END_ALL)])
    lines.append("\nSessions also log out after 1 hour without use.")
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def sessions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, keyboard = _sessions_view(str(update.effective_user.id))
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def handle_session_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot callback for the Log out buttons under /sessions."""
    query = update.callback_query
    user_id = str(query.from_user.id)
    data = query.data or ""
    now = _now()
    ended = 0

    if data == END_ALL:
        ended = end_all_sessions(user_id, "telegram")
    elif data.startswith(END_PREFIX):
        ref = get_db().collection("web_sessions").document(data[len(END_PREFIX):])
        doc = ref.get()
        if doc.exists:
            session = doc.to_dict()
            if str(session.get("user")) != user_id:
                await query.answer("This session isn't yours.", show_alert=True)
                return
            if session.get("status") == "active":
                ref.update({"status": "ended", "ended_reason": "telegram", "ended_at": now})
                ended = 1
    else:
        await query.answer()
        return

    await query.answer("Logged out" if ended else "Already logged out")
    text, keyboard = _sessions_view(user_id)
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except Exception:
        pass  # message unchanged
