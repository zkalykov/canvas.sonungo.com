# Canvas Telegram Bot

A high-performance Telegram bot that helps students track their Canvas assignments. It uses **FastAPI** for the backend and **Google Cloud KMS** for enterprise-grade security.

## Features
*   **Secure:** User tokens are encrypted via Google Cloud Key Management Service (KMS). Even the database admins cannot see the raw tokens.
*   **Privacy-Focused:** Auto-deletes sensitive messages (tokens) and media from the chat.
*   **Smart Reminders:** Sends notifications 24h, 12h, 6h, 1h, and minutes before deadlines.
*   **Lightweight:** Built on FastAPI and Python 3.11.

## Prerequisites
*   Google Cloud Platform Account
*   Python 3.10+
*   Telegram Bot Token (from @BotFather)
*   `gcloud` CLI installed

---

## Setup Guide

### 1. Google Cloud Infrastructure
This project relies on Google Cloud services. You must set these up first.

**A. Authentication**
Login to your Google Cloud account locally so the app can access the APIs.
```bash
gcloud auth login
gcloud auth application-default login
```

**B. Key Management Service (KMS)**
We use a specific Key Ring and Key to encrypt data. Run these commands to create them:

```bash
# 1. Create the Key Ring (The container)
gcloud kms keyrings create canvas-reminder-ring --location global

# 2. Create the Key (The actual encryption key)
gcloud kms keys create canvas-token-key \
    --location global \
    --keyring canvas-reminder-ring \
    --purpose encryption
```
*Note: Ensure your Service Account has the `Cloud KMS CryptoKey Encrypter/Decrypter` role on this key.*

**C. Firestore**
Enable **Firestore** (Native Mode) in your Google Cloud Project. This serves as the database.

### 2. Environment Variables
Create a `.env` file in the root directory:

```bash
PROJECT_ID=your-google-cloud-project-id
TELEGRAM_BOT_API=your-telegram-token
DATABASE_NAME=your-database-name (optional, defaults to (default))
```
*Note: We do NOT store an encryption key here anymore. That is handled by KMS.*

### 3. Installation & Running Locally

Install the dependencies:
```bash
pip install -r requirements.txt
```

Run the application:
```bash
uvicorn main:app --reload
```
The server will start at `http://127.0.0.1:8000`.

---

## Deployment (Docker & Cloud Run)

This project is optimized for **Google Cloud Run**.

**1. Build the Container**
```bash
gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/canvas-bot
```

**2. Deploy Service**
```bash
gcloud run deploy canvas-bot \
    --image gcr.io/YOUR_PROJECT_ID/canvas-bot \
    --platform managed \
    --allow-unauthenticated \
    --region us-central1
```

**3. Configure Cloud Scheduler**
To trigger reminders, create a Cloud Scheduler job targeting your deployed URL:
*   **URL:** `https://your-cloud-run-url.com/check_reminder`
*   **Method:** POST
*   **Frequency:** `* * * * *` (Every minute)

---

## How Security Works
We use a **Blind Envelope** encryption strategy:
1.  When a user submits a token, the bot immediately sends it to Google KMS.
2.  KMS returns a ciphertext (scrambled string) which is saved to the database.
3.  The raw token is removed from memory immediately.
4.  Decryption *only* happens individually during the reminder check process and requires the specific Service Account permissions.

This ensures that even if the database is leaked, the tokens remain useless without access to the Google Cloud KMS hardware key.
