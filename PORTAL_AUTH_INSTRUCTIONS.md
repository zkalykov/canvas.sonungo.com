# Portal Authentication — Integration Instructions

## Overview

When a user types `/portal` in the Telegram bot, the bot generates a **one-time auth code** (UUID) that expires in **1 minute**. The code is stored in Firestore (`auth_codes` collection) and sent to the user as a link:

```
{PORTAL_DOMAIN}/auth/{authcode}
```

The portal (`portal.sonungo.com`) must exchange this code for the user's Canvas credentials by calling back to the canvas API server.

---

## Flow

```
1. User types /portal in Telegram
2. Bot creates auth code in Firestore (status: "pending", expires in 1 min)
3. Bot sends link: portal.sonungo.com/auth/<authcode>
4. User clicks link → portal.sonungo.com opens
5. Portal extracts <authcode> from the URL
6. Portal sends POST to canvas.sonungo.com/api/portal/auth with the code
7. API verifies the code, marks it as "used", decrypts the Canvas token
8. API returns canvas_url, canvas_token, and telegram_id to the portal
9. Portal stores these securely (e.g., in-memory/session) and authorizes the user
```

---

## API Endpoint

### `POST /api/portal/auth`

**URL:** `https://canvas.sonungo.com/api/portal/auth`

**Headers:**
```
Content-Type: application/json
```

**Request Body:**
```json
{
  "code": "<the-authcode-from-url>"
}
```

**Success Response (200):**
```json
{
  "status": "success",
  "canvas_url": "nau.instructure.com",
  "canvas_token": "<decrypted-plain-text-canvas-token>",
  "telegram_id": "123456789"
}
```

**Error Responses:**

| Status | Detail |
|--------|--------|
| 400 | `Missing auth code` — no `code` in body |
| 400 | `Auth code already used` — code was already exchanged |
| 400 | `Auth code expired` — more than 1 minute has passed |
| 404 | `Invalid auth code` — code doesn't exist |
| 404 | `User not found` — user was deleted |
| 500 | `Failed to decrypt token` — KMS decryption error |

---

## Example: Portal Frontend (JavaScript)

```javascript
// 1. Extract authcode from URL path: /auth/<authcode>
const pathParts = window.location.pathname.split("/");
const authcode = pathParts[pathParts.length - 1];

// 2. Exchange code for credentials
async function authenticate(code) {
  const response = await fetch("https://canvas.sonungo.com/api/portal/auth", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });

  if (!response.ok) {
    const error = await response.json();
    console.error("Auth failed:", error.detail);
    // Show error to user (expired, already used, etc.)
    return null;
  }

  const data = await response.json();
  // data.canvas_url    → e.g. "nau.instructure.com"
  // data.canvas_token  → decrypted Canvas API token
  // data.telegram_id   → user's Telegram ID

  return data;
}

// 3. Use credentials
authenticate(authcode).then((data) => {
  if (data) {
    // Store in sessionStorage (cleared when tab closes — more secure than localStorage)
    sessionStorage.setItem("canvas_url", data.canvas_url);
    sessionStorage.setItem("canvas_token", data.canvas_token);
    sessionStorage.setItem("telegram_id", data.telegram_id);

    // Now make Canvas API calls:
    // fetch(`https://${data.canvas_url}/api/v1/courses`, {
    //   headers: { Authorization: `Bearer ${data.canvas_token}` }
    // })
  }
});
```

---

## Security Notes

1. **One-time use** — Each code can only be exchanged once. After the first successful call, the code is marked `"used"` and cannot be reused.
2. **1-minute expiry** — If the user doesn't click the link within 1 minute, the code expires.
3. **CORS restricted** — Only the domain set in `PORTAL_DOMAIN` env var is allowed to call the API.
4. **Token decryption** — The Canvas token is encrypted at rest in Firestore using Google Cloud KMS. It is decrypted server-side only during this exchange and sent over HTTPS.
5. **Recommendation** — Store credentials in `sessionStorage` (not `localStorage`) so they are cleared when the browser tab closes.

---

## Environment Variable

Set `PORTAL_DOMAIN` in your `.env` file:

```env
# Local development
PORTAL_DOMAIN=http://localhost:3000

# Production
PORTAL_DOMAIN=https://portal.sonungo.com
```

This variable is used in:
- `telegram_bot.py` — to generate the link sent to the user
- `main.py` — for CORS allowed origins
