"""
Login approval for the web dashboard.

Flow:
1. The bot's /portal command sends a one-time link (auth_codes, 30 seconds).
2. The dashboard opens the link and calls create_login_request(): the code is used up,
   a login request is stored (2 minutes), and the bot asks the user to Approve or Deny.
3. The dashboard polls check_login_request() with the secret poll token it received.
   Only after the user approves are the Canvas credentials returned, exactly once,
   together with the access level the user chose: "view" (read only) or "full",
   and a web session (web_sessions.py) that can be logged out from Telegram (/sessions).
"""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from canvas_initialize import decrypt_token
from db import get_db
from web_sessions import create_session

APPROVAL_SECONDS = 120
VIEW_PREFIX = "LOGIN_VIEW_"
FULL_PREFIX = "LOGIN_FULL_"
DENY_PREFIX = "LOGIN_DENY_"
LEGACY_APPROVE_PREFIX = "LOGIN_APPROVE_"  # buttons sent before access levels existed: treated as view only
ACCESS_LABEL = {"view": "view only", "full": "full access"}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local_time(moment: datetime, time_zone: str | None) -> str:
    try:
        moment = moment.astimezone(ZoneInfo(time_zone or "America/Chicago"))
    except Exception:
        pass
    return moment.strftime("%b %d, %I:%M %p").replace(" 0", " ")


async def create_login_request(bot, code: str, device: str, location: str = "", ip: str = "") -> dict:
    """Uses up a one-time code and asks the user on Telegram to approve the login."""
    db = get_db()
    code_ref = db.collection("auth_codes").document(code)
    code_doc = code_ref.get()
    if not code_doc.exists:
        raise HTTPException(status_code=404, detail="Invalid auth code")

    code_data = code_doc.to_dict()
    if code_data.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Auth code already used")
    code_expires = code_data.get("expires_at")
    if code_expires and code_expires < _now():
        raise HTTPException(status_code=400, detail="Auth code expired")
    code_ref.update({"status": "used"})

    user_id = str(code_data.get("user"))
    request_id = uuid.uuid4().hex
    poll_token = secrets.token_urlsafe(32)
    created = _now()
    expires = created + timedelta(seconds=APPROVAL_SECONDS)
    device = (device or "a web browser").strip()[:80]
    location = (location or "").strip()[:100]
    ip = (ip or "").strip()[:45]
    user_doc = db.collection("users").document(user_id).get()
    time_zone = user_doc.to_dict().get("time_zone") if user_doc.exists else None

    # Store the request before messaging, so a very quick Approve tap always finds it.
    request_ref = db.collection("login_requests").document(request_id)
    request_ref.set({
        "user": user_id,
        "status": "pending",
        "poll_token_hash": _hash(poll_token),
        "device": device,
        "location": location,
        "ip": ip,
        "created_at": created,
        "expires_at": expires,
    })

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("View only (recommended)", callback_data=f"{VIEW_PREFIX}{request_id}")],
        [InlineKeyboardButton("Full access", callback_data=f"{FULL_PREFIX}{request_id}")],
        [InlineKeyboardButton("Deny", callback_data=f"{DENY_PREFIX}{request_id}")],
    ])
    details = [f"Device: {device}", f"Location: {location or 'unknown'}"]
    if ip:
        details.append(f"IP address: {ip}")
    details.append(f"Time: {_local_time(created, time_zone)}")
    try:
        message = await bot.send_message(
            chat_id=user_id,
            text=(
                "Log in to Canvas Dashboard?\n\n"
                + "\n".join(details)
                + "\n\nView only: see everything, but nothing can be submitted or changed.\n"
                "Full access: also submit work, post and send messages.\n\n"
                "Only approve if you just opened your login link. This request expires in 2 minutes."
            ),
            reply_markup=keyboard,
        )
        request_ref.update({"message_id": message.message_id})
    except Exception as e:
        request_ref.update({"status": "error"})
        print(f"Error sending login approval: {e}")
        raise HTTPException(status_code=502, detail="Could not send the approval message on Telegram")

    return {"request_id": request_id, "poll_token": poll_token, "expires_at": expires.isoformat()}


def check_login_request(request_id: str, poll_token: str) -> dict:
    """Returns the request status; on approval also the Canvas credentials (only once)."""
    if not request_id or not poll_token:
        raise HTTPException(status_code=400, detail="Missing request_id or poll_token")

    db = get_db()
    request_ref = db.collection("login_requests").document(request_id)
    request_doc = request_ref.get()
    if not request_doc.exists:
        raise HTTPException(status_code=404, detail="Unknown login request")

    req = request_doc.to_dict()
    if not secrets.compare_digest(req.get("poll_token_hash", ""), _hash(poll_token)):
        raise HTTPException(status_code=403, detail="Forbidden")

    status = req.get("status")
    if status == "pending" and req.get("expires_at") and req["expires_at"] < _now():
        request_ref.update({"status": "expired"})
        return {"status": "expired"}
    if status != "approved":
        # pending, denied, expired, completed or error
        return {"status": status}

    user_doc = db.collection("users").document(req["user"]).get()
    if not user_doc.exists:
        raise HTTPException(status_code=404, detail="User not found")
    user_data = user_doc.to_dict()
    encrypted_token = user_data.get("canvas_token")
    canvas_url = user_data.get("canvas_url")
    if not encrypted_token or not canvas_url:
        raise HTTPException(status_code=400, detail="Incomplete user data")
    try:
        canvas_token = decrypt_token(encrypted_token)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to decrypt token")

    # Hand the credentials over exactly once.
    request_ref.update({"status": "completed", "completed_at": _now()})
    access = req.get("access") if req.get("access") in ("view", "full") else "view"
    session = create_session(
        req["user"], access, req.get("device", ""), req.get("location", ""), req.get("ip", ""), request_id
    )
    return {"status": "approved", "access": access, "canvas_url": canvas_url, "canvas_token": canvas_token, **session}


async def handle_login_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot callback for the View only / Full access / Deny buttons."""
    query = update.callback_query
    data = query.data or ""
    access = None
    for prefix, level in ((VIEW_PREFIX, "view"), (FULL_PREFIX, "full"), (LEGACY_APPROVE_PREFIX, "view"), (DENY_PREFIX, None)):
        if data.startswith(prefix):
            request_id, access = data[len(prefix):], level
            break
    else:
        await query.answer()
        return
    approve = access is not None

    db = get_db()
    request_ref = db.collection("login_requests").document(request_id)
    request_doc = request_ref.get()
    if not request_doc.exists:
        await query.answer("This login request no longer exists.")
        await query.edit_message_reply_markup(reply_markup=None)
        return

    req = request_doc.to_dict()
    if str(req.get("user")) != str(query.from_user.id):
        await query.answer("This login request isn't yours.", show_alert=True)
        return

    if req.get("status") != "pending":
        await query.answer("This login request was already handled.")
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if req.get("expires_at") and req["expires_at"] < _now():
        request_ref.update({"status": "expired"})
        await query.answer("This login request expired.")
        await query.edit_message_text("This login request expired. Send /portal to get a new link.")
        return

    device = req.get("device") or "a web browser"
    where = f" in {req['location']}" if req.get("location") else ""
    update_fields = {"status": "approved" if approve else "denied", "decided_at": _now()}
    if approve:
        update_fields["access"] = access
    request_ref.update(update_fields)
    if approve:
        label = ACCESS_LABEL[access]
        await query.answer(f"Approved: {label}")
        await query.edit_message_text(
            f"Login approved with {label} on {device}{where}. You can go back to the website.\n\n"
            "See or log out your sessions any time with /sessions."
        )
    else:
        await query.answer("Denied")
        await query.edit_message_text("Login denied. Nobody was signed in.")
