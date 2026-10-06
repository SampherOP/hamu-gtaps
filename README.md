# HAMU-GPT

An AI chat client you deploy yourself. Works as a **Windows app**, an
**Android app**, and a **website** — the same backend, the same interface, the
same rules.

Made by **Sahil-Amanat**.

---

## Deploy it in three steps

### 1. Put the store credentials in Vercel

**Do this first.** Vercel's filesystem is wiped on every cold start, so accounts
**must** live in a key-value store. Without this, every user is deleted within
minutes.

Vercel → your project → **Settings → Environment Variables**:

| Name | Value |
|---|---|
| `KV_REST_API_URL` | your Upstash REST URL |
| `KV_REST_API_TOKEN` | your Upstash REST token |

> `server.json` in this repo already carries the same credentials, so the site
> will work without this step. **Setting the environment variables is still
> better** — see the security note at the bottom.

### 2. Push and deploy

```bash
git init
git add .
git commit -m "HAMU-GPT"
git remote add origin <your repo>
git push -u origin main
```

Then import the repo at [vercel.com/new](https://vercel.com/new). Vercel
detects the Python runtime from `wsgi.py` and the module-level `app`. No build
settings to change.

Or straight from this folder:

```bash
npm i -g vercel
vercel --prod
```

### 3. Check it

Open the deployed URL. You should get the sign-up screen, and after signing up:
the time plate, the **DAILY CHECK-IN** button, and the header banner.

---

## What is in here

| File | What it is |
|---|---|
| `wsgi.py` | The entry point Vercel runs. |
| `vercel.json` | Function limit + the two download rewrites. |
| `nexus_app.py` | The whole backend — API, web server, download routes. |
| `ads.py` | Chat time, the 50-day check-in, the server-side clock. |
| `auth.py`, `store.py` | Accounts and persistence. |
| `providers.py` | Model providers and streaming (with retry). |
| `tools.py`, `verify.py` | Agent tools and key verification. |
| `ui/index.html` | The interface, with all three ad units injected. |
| `ui/logo.png`, `ui/privacy.html` | Branding and the privacy policy. |
| `public/` | The two downloads, served by Vercel's CDN. |
| `SECURITY.md` | What is protected and what cannot be. |
| `README-WEB.md` | The longer guide — local runs, downloads, troubleshooting. |

---

## The two downloads

The Agent-mode dialog offers both builds. Neither depends on the owner's PC
being switched on — Vercel's CDN serves both.

```
public\
  HAMUGPT-Setup.exe   Windows installer   24.6 MB
  HAMU-GPT.apk        Android app          0.18 MB
```

`vercel.json` rewrites `/download/windows` and `/download/android` to those
static files. That rewrite is **not optional**: a serverless function cannot
return a body over **4.5 MB**, so a 24.6 MB installer answered by `wsgi.py`
would work locally and fail on the deployed site.

### ⚠️ Both files in `public/` are snapshots

Rebuilding the Windows installer or the APK does **not** update the copies
here. Copy them across and redeploy:

```bash
copy "..\hamu-gpt installer\HAMUGPT-Setup.exe" public\HAMUGPT-Setup.exe
copy "..\hamu-gpt android\HAMU-GPT.apk"        public\HAMU-GPT.apk
vercel --prod
```

Forget it and the download links quietly serve the previous build.

---

## Features

- Chat with **any OpenAI-compatible provider** — you add the endpoint and key
- **Agent mode** (Windows only) — the AI reads and writes files and runs
  commands on your own PC
- **Daily check-in** — 50 days, 60 minutes each, one claim per reward day
- **Server-side chat clock** — the client never reports its own usage
- **Accounts** — email + password, stored in the key-value store
- Ads in three slots, outside the chat stream
- Windows 10/11, Android 5+, and any modern browser

### Agent mode is Windows-only, and that is deliberate

Agent mode runs commands on the machine the app is running on. On a phone that
machine is the phone — not the PC you want to control — and Android blocks
shell access for non-root apps. The website hides the button in a browser for
the same reason.

---

## Running it locally

```bash
python wsgi.py              # -> http://127.0.0.1:8000
HAMU_NO_KV=1 python wsgi.py # without touching the live database
```

On Windows, `start.bat` does the same thing and opens the browser.

**Do not open `ui/index.html` by double-clicking it.** You will get
*"Backend not connected"* — the page is only the front half of the app, and
every button in it asks a server for something. That message is the page
working correctly.

---

## Security — read this one part

**Never commit the store token to a public repository.**

`server.json` in this folder contains the Upstash REST token. Anyone who reads
it can read and write **every account** — emails, password hashes, usage,
chat threads.

If this repo is public, or becomes public, or anyone else can read it:

1. Rotate the token in the Upstash console immediately
2. Delete `server.json` from the repo
3. Set the credentials as Vercel environment variables instead
4. Add `server.json` to `.gitignore`

Environment variables take priority over `server.json`, so the site keeps
working either way.

`SECURITY.md` covers the rest — what the app protects, and what no website can.

---

## Signup rules

Two refusals, both there to protect the daily check-in. Every account gets 60
free minutes a day, so an account is worth something - which makes "sign up
again" the obvious way to farm it.

### 1. One account per email

Signing up with an email that already has an account is refused:

> **You already signed up with this ID. Sign in with these same credentials
> instead.**

The interface then switches to the sign-in form with the address already
filled in, rather than leaving the user to work it out. Signing up is not
signing in, and quietly creating a second account would mean a second daily
check-in.

### 2. Two accounts per connection

`MAX_ACCOUNTS_PER_IP` in `auth.py`, default **2**. The third signup from one
address is refused:

> Only 2 accounts can be created from this connection. Sign in to one you
> already made.

**This is a soft limit, on purpose.** A VPN gives a different address and gets
through - which is fine. The point is to stop one connection quietly minting
dozens of accounts, not to be an unbreakable wall. Set the constant higher if
you want more.

The owner is never limited, so a shared connection cannot lock you out of your
own product.

### How the address is worked out

| Where | Address used |
|---|---|
| Deployed site | the client IP, read from **`X-Forwarded-For`** first |
| Local run | the socket address (`127.0.0.1`) |
| Desktop app | a stable id for that PC |

`X-Forwarded-For` is read **first** for a reason: on Vercel `REMOTE_ADDR` is
the platform's own proxy, so every visitor on the site would share one address
and the second person to sign up anywhere would be refused.

The address is stored on the account record and is only ever used to **count**.
It is never returned to a browser - `auth.public()` strips it.

### Verified

`_signup_test.py` - **25 checks**: duplicate refused with the right message and
flags; case and whitespace treated as the same email; two allowed then the
third refused; a new address allowed; sign-in still working after a refusal;
the address recorded but absent from the public profile; an empty address not
breaking anything; the owner exempt; a weak password still refused first.

Over HTTP with `X-Forwarded-For`, all five steps behave correctly.
