# HAMU-GPT — WEB build (for Vercel)

This folder is the **website** version of HAMU-GPT. It runs the *same* backend
and the *same* interface as the desktop app — same daily check-in, same three
ad units, same server-side clock. Only the host changes.

---

## What is in here

| File | What it is |
|---|---|
| `start.bat` | **Double-click this** to run the site on this PC. |
| `wsgi.py` | The entry point Vercel runs. Wraps the app as a WSGI `app`. |
| `vercel.json` | Vercel config (60-second function limit). |
| `nexus_app.py` | The whole backend — the same file the desktop app uses. |
| `ads.py` | Chat time, the 50-day check-in, and the server-side clock. |
| `auth.py` / `store.py` | Accounts and persistence. |
| `providers.py` / `tools.py` / `verify.py` | Model providers, agent tools, key checks. |
| `ui/index.html` | The interface, **with all three ad units already injected**. |
| `ui/logo.png`, `ui/logo-small.png` | The logo. The page asks for these by name — if they are missing you get an empty circle. |
| `ui/privacy.html` | The privacy policy. **Adsterra requires the site to have one**, and the footer links to it at `/privacy`. |
| `server.json` | The Upstash credentials — for **local runs only**. Not committed. |
| `DEPLOY.md` | The longer deployment notes. |

---

## Run it locally first

### Double-click `start.bat`

That is the whole thing. It finds Python, starts the server, and opens your
browser at the right address.

### *** Never open `ui\index.html` by double-clicking it ***

You will get a page that says **"Backend not connected"**. That is not a bug —
it is the page working correctly. `index.html` is only the **front half** of the
app; every button in it asks a server for something. Opening it from the disk
means there is no server, so every request fails.

`start.bat` is the other half. Run that instead.

### Or by hand

```bash
cd HAMU-GPT-WEB
python wsgi.py            # -> http://127.0.0.1:8000
```

To make it behave like the real deploy (Vercel sets `VERCEL=1` itself):

```bash
HAMU_HOSTED=1 python wsgi.py
```

To run without touching the live database:

```bash
HAMU_HOSTED=1 HAMU_NO_KV=1 python wsgi.py
```

Then open <http://127.0.0.1:8000>, sign up with an email + password, and you
should see the time plate, the **DAILY CHECK-IN** button and the header banner.

### What was actually verified this way

Sign-up over HTTP returns a session token; `ad_status` reports
`enabled: true`; the check-in claims **3600 seconds** and lands on **Day 1**; a
second claim the same day is refused with `already`; the balance reads
**60 minutes**. Agent mode is refused with
*"only available in the desktop app"*. All of that is the same code path Vercel
runs.

---

## Deploy to Vercel

### 1. Set the store credentials as environment variables

**Do this first.** On Vercel the filesystem is **ephemeral** — every cold start
gets a fresh `/tmp`. If accounts only live in files, **every user is deleted
within minutes**. The key-value store is not optional on the web build.

In the Vercel dashboard → your project → **Settings → Environment Variables**,
add:

| Name | Value |
|---|---|
| `KV_REST_API_URL` | `https://wanted-lemming-196000.upstash.io` |
| `KV_REST_API_TOKEN` | *(your Upstash token)* |

The Upstash integration sets exactly these names, so if you connect Upstash
through Vercel's integration they appear automatically.

> The same token is in `server.json` in this folder. That file is in
> `.gitignore` on purpose — **do not commit it**. Anyone who reads it can read
> and write every account. Use the environment variables instead.

### 2. Deploy

```bash
npm i -g vercel
cd HAMU-GPT-WEB
vercel            # preview
vercel --prod     # live
```

Or push the folder to GitHub and import it in Vercel — it detects the Python
runtime from `wsgi.py` and the module-level `app`.

### 3. Check it worked

Visit the deployed URL. You should get:

- The sign-up screen
- After signing up: the time plate, **DAILY CHECK-IN**, and the header banner
- `usage` chip and provider list working

If accounts vanish after a few minutes, the environment variables in step 1
are missing or misspelled.

---

## What works, and what cannot

| Feature | On the web |
|---|---|
| Chat with any OpenAI-compatible provider | ✅ |
| Accounts, saved chats, settings | ✅ (needs Upstash) |
| **Daily check-in** — 50 days, 60 min/day | ✅ |
| **Server-side chat clock** (tamper-resistant) | ✅ |
| Header banner, Social Bar, Popunder | ✅ |
| **Agent mode** (reading/writing your PC) | ❌ **never** — by design |

Agent mode is the one thing that is desktop-only, and it has to be. A website
that could run shell commands on a visitor's machine would be a catastrophe,
and `store.hosted()` blocks it on every code path. The tool catalog is trimmed
server-side; the buttons are not merely hidden.

---

## Why the check-in is allowed on a website

This used to be desktop-only. The old design paid the user in chat time for
**watching an ad** — incentivised traffic, which Adsterra prohibits outright
(publisher terms Section 6(iv)), so a website running it would be banned.

That design is gone. The replacement is a **daily check-in**: a login bonus,
where **no ad has to be watched to claim it**. A login bonus is not
incentivised traffic, so the web build can run it — and the same server-side
clock keeps it honest there too.

The ads themselves are ordinary display units. Nothing on the page is tied to
a reward, and nothing forces a click.

---

## The tamper resistance, in one paragraph

The interface can always be edited by whoever is using it — DevTools,
Tampermonkey, a patched copy of the page. That is true of every website and
cannot be fixed. So the design does not rely on the interface: the chat-time
balance, the length of each turn, and the check-in day all live on the server.
Change the display all you like — the server still refuses the turn when the
real balance is gone.

---

## Keeping it in step with the desktop app

The two builds share the same files, so a change belongs in **both**:

1. Edit the source in `HAMU-GPT\` (the desktop project)
2. Copy the changed `.py` files and `ui/index.html` into this folder
3. Re-inject the ads into this folder's `ui/index.html` — the desktop build
   script can do it:
   `python -c "import sys; sys.path.insert(0,r'...\hamu-gpt installer'); import build; build.inject_ads(r'...\HAMU-GPT-WEB')"`
4. Redeploy

The desktop installer lives in `hamu-gpt installer\` and already handles the
injection on its side.

---

## A trap worth knowing

The server only serves files that sit **inside `ui/`** — `nexus_app._serve_static`
refuses anything else, and refuses `..` outright. So every asset the page asks
for has to live in that folder.

When this folder was first assembled, only `index.html` was copied across. The
page loaded, the app worked — and the logo was an **empty circle**, because
`logo.png` was a 404. `privacy.html` was missing too, which is worse than
cosmetic: Adsterra's terms require the site to publish a privacy policy.

If you ever add an image or a page to the interface, put it in `ui/` and copy
it here as well, or it will 404 on the deployed site and nowhere else.

To check: load the site, open DevTools → Network, and look for anything red.

---

## "Continue with Google" — where it works, and where it cannot

This is the one feature that genuinely differs between the three places the app
runs, and the difference is a browser security boundary, not a bug.

The one-tap list is built by reading Chrome's own profile files:

```
%LOCALAPPDATA%\Google\Chrome\User Data\<profile>\Preferences
```

specifically the `account_info` block, which is plain JSON. Nothing is sent
anywhere. But it can only read **the machine the backend is running on**.

| Where it runs | Google button | Why |
|---|---|---|
| **Desktop app** | ✅ works | backend and Chrome are on your PC |
| **`start.bat` (local web)** | ✅ works | same thing — your PC, your Chrome |
| **Deployed on Vercel** | ❌ hidden | a server has no Chrome to read |

### It does NOT see accounts from other PCs

Worth being blunt about: if you sign into Chrome on your laptop and also on a
friend's machine, the app on **each machine shows only that machine's Chrome
accounts**. There is no shared list and no central account directory — it reads
local files, so it is local by nature.

On Vercel it shows nothing at all, which is why the button hides itself there
rather than offering a list that would be empty.

### If you want real Google sign-in on the deployed site

That needs **Google OAuth**: a Google Cloud project, an OAuth client ID, a
client secret, and the deployed URL registered as an authorised redirect URI.
That is a different mechanism — Google redirects the user back to your site
with a verified token, instead of you reading anything off their disk. It is
buildable, but it needs those credentials from your Google Cloud console first.

Until then, the deployed site uses email + password, which works everywhere.

---

## Troubleshooting

### "Continue with Google" is missing

You are almost certainly still running an **old server process**. Changing
`start.bat` does nothing until the server is restarted — a browser refresh is
not enough.

1. Close the minimised window called **"HAMU-GPT WEB server"**
2. Run `start.bat` again

The button hides itself when `store.hosted()` is true, and an earlier version of
`start.bat` set `HAMU_HOSTED=1`. The current one deliberately does not.

### `FileNotFoundError: ... account.json.tmp`

Fixed. `account.json` is deliberately **local-only** (it records who is signed
in on this machine, so it must never go into the shared store), which means on a
hosted build it is a real file write into the temp folder. If that folder was
missing the write used to raise. `store._write` now creates the folder first.

### Sign-up works but the account vanishes later

On Vercel that means the store credentials are missing. See step 1 of the
deploy section — `KV_REST_API_URL` and `KV_REST_API_TOKEN`. A serverless
filesystem is wiped on every cold start, so file storage cannot hold accounts.

Check what the app thinks it is using:

```bash
python -c "import store; print(store.kv_kind())"
```

It should print **key-value store (Upstash Redis)**. If it prints
**files (no key-value store configured)**, the credentials are not being found.

### Where the store credentials are read from

In order — the first one that answers wins:

1. `KV_REST_API_URL` / `KV_REST_API_TOKEN` environment variables
2. `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`
3. `%APPDATA%\HAMU-GPT\server.json`
4. `server.json` **next to `store.py`** — i.e. this folder

Number 4 was missing until recently, which is why a `server.json` sitting in
this folder used to do nothing at all.

---

## Downloads — the "Download for Windows" button

The Agent-mode dialog on the website offers the desktop app. Pressing
**Download for Windows** fetches `HAMUGPT-Setup.exe`.

### It works even when your own PC is off

The installer is **deployed with the site**, not served from your machine:

```
HAMU-GPT-WEB\
  public\
    HAMUGPT-Setup.exe      <- the file Vercel's CDN hands out
  vercel.json              <- /download/windows -> /HAMUGPT-Setup.exe
```

`vercel.json` rewrites `/download/windows` straight to that static file. So
the request never reaches the Python function and **never touches your PC** —
Vercel serves it from its own network, 24/7.

### Why it must NOT go through the function

Vercel caps a serverless **response** at **4.5 MB**. The installer is 24.6 MB.
If `/download/windows` were answered by `wsgi.py`, the download would fail on
the deployed site while working perfectly on a local run — a nasty, confusing
difference. Hence the rewrite.

The WSGI route still exists (`nexus_app._serve_download`) so that a **local**
run works too, and so any host without that limit can serve it directly.

### Verified locally

```
GET /download/windows  ->  200, 25,761,954 bytes
Content-Disposition: attachment; filename="HAMUGPT-Setup.exe"
Content-Type: application/octet-stream
Cache-Control: public, max-age=300
```

Byte-for-byte identical to the installer the build produced (md5 compared).

### After you rebuild the installer

The copy in `public/` is a **snapshot**. A new `HAMUGPT-Setup.exe` in the
installer folder does **not** update it. Copy it across and redeploy:

```bash
copy "..\hamu-gpt installer\HAMUGPT-Setup.exe" public\HAMUGPT-Setup.exe
vercel --prod
```

Easy to forget, and the symptom is a download link that quietly serves the
previous version.

### Android

The **Download for Android** button is live. Same arrangement as Windows:

```
public\
  HAMUGPT-Setup.exe     <- Windows installer (24.6 MB)
  HAMU-GPT.apk          <- Android app      (0.18 MB)
```

`vercel.json` rewrites both `/download/windows` and `/download/android` to
those static files, so both are served from Vercel's CDN whether or not your
own PC is on.

**The Android app is a chat client, not an agent.** Agent mode runs commands on
the machine the app is on — on a phone that is the phone, not the PC you want
to control — and Android blocks shell access for non-root apps anyway. The
button's note says this plainly so nobody expects otherwise.

The APK is a WebView shell around the deployed site, so it updates itself
whenever the website is redeployed. Built in `..\hamu-gpt android\`; see
`README-ANDROID.md` there for the full build.

**Both files in `public/` are snapshots.** Rebuilding either one does NOT
update the copy here. Re-copy and redeploy, or the download links quietly
serve the previous build.
