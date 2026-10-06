# HAMU-GPT — two builds, one codebase

The same UI and the same backend, shipped two ways:

| | **Windows app** | **Chrome / hosted** |
|---|---|---|
| What it is | A desktop `.exe` | A website |
| Window | Native (pywebview + WebView2) | A browser tab |
| Backend | Bundled inside the exe | The same Python, behind HTTP |
| Where it runs | Your PC | Vercel (or any Python host) |
| Entry point | `HAMU-GPT.exe` | `wsgi.py` |

Both talk to **exactly the same backend code** — `nexus_app.Api`. There is no
second implementation to keep in sync, and the security rules (private-method
blocking, per-session account binding) live in one function,
`nexus_app.handle_request()`.

---

## 1. The Windows build

```
BUILD.bat
```

or by hand:

```
python -m PyInstaller --noconfirm --clean HAMU-GPT.spec
```

Output: `dist\HAMU-GPT.exe`. Nothing to install — the UI is bundled, and the
only third-party runtime (pywebview / WebView2) ships with Windows 10+.

---

## 2. The Chrome / hosted build

### Run it locally first

```
python nexus_app.py --web            # http://127.0.0.1:8000
python nexus_app.py --web --port=9000
python nexus_app.py --web --host-all # expose on the LAN (0.0.0.0)
```

Open the URL in Chrome. You get the identical app.

### Or run the WSGI app the way a host will

```
python wsgi.py
```

### Deploy to Vercel

1. Put this folder in a Git repo and push it to GitHub.
2. In Vercel, **New Project** → import that repo.
3. Framework preset: **Other**. Leave the build command empty.
4. Deploy. `vercel.json` already declares `wsgi.py` as the entry point
   and routes every path to it.
5. Vercel gives you a `*.vercel.app` domain immediately — add your own free
   domain from the project's **Domains** tab.

`requirements.txt` intentionally lists nothing for the web build: the whole
server is the Python standard library. Do **not** add `pywebview` there — a
server has no window to host, and it would only slow the deploy down.

---

## 3. Accounts, on both builds

Sign-in is **per account**, and so is everything the account owns.

Where data lives, on the desktop build:

```
%APPDATA%\HAMU-GPT\
  account.json          the current session (who is signed in right now)
  accounts-registry.json  every account this machine has seen
  ads.json              per-email chat-time balances
  accounts\
    alice@example.com\
      providers.json    her providers + encrypted API keys
      usage.json        her request/token counters
      threads.json      her chats
    bob@example.com\
      providers.json    completely separate
      usage.json
      threads.json
```

**Deciding who a request belongs to**

| Build | How the account is identified |
|---|---|
| Windows | The one signed-in session in `account.json` — there is one user at the keyboard |
| Chrome / hosted | An opaque per-tab token (`X-Hamu-Token`) mapped to an account server-side |

On the hosted build two browser tabs can hold **two different accounts at the
same time** and neither can see the other's chats, providers or time balance.
That is verified end to end in `_web_test.py`.

**Why a chat can never leak between accounts**

Layer 1 — the file. Each account has its own folder, chosen from its email.

Layer 2 — the ownership tag. Every thread is written with an `owner` field
holding the email it was created under. On load, `store.load_threads()` drops
any thread whose owner is a different account. Even if a file were copied into
the wrong folder, the clone would be rejected.

Layer 3 — the UI never keeps a shared copy. There is deliberately **no**
`localStorage` mirror of the chat list, because a WebView (and a Chrome
profile) shares one `localStorage` across every account that signs in. The
in-memory list is also tagged with the account it belongs to, and is dropped
the instant the signed-in email changes — so a stale row can never be saved
into a new account.

---

## 4. Security notes — and the honest limits

**What is protected**

- Passwords are hashed with PBKDF2-HMAC-SHA256, 200,000 rounds (see
  `auth.hash_password`). The plain password is never stored.
- Password comparisons use `hmac.compare_digest`, so a wrong guess cannot be
  timed.
- Provider API keys are encrypted with the Windows **DPAPI**, which ties the
  ciphertext to the Windows user account (`store._encrypt`).
- Private backend methods are unreachable over HTTP: any name starting with
  `_` is refused by `handle_request()`, so `_read`, `_write` and friends
  cannot be called from a browser.
- Responses never contain a password hash or a password field.
- Every response carries `Cache-Control: no-store`, `X-Content-Type-Options:
  nosniff` and `Referrer-Policy: no-referrer`.

**What is *not* protected — read this**

- **The account system is local.** On the Windows build it is a convenience
  lock, not a security boundary. Anyone who can read `%APPDATA%\HAMU-GPT\` can
  read the files, because Windows does not encrypt them. DPAPI protects the
  API keys against being copied to another machine; it does not stop someone
  at your keyboard.
- **The hosted build is only as private as the host.** Deployed to a public
  Vercel URL, anyone with the link can sign up and use it. The per-account
  isolation stops users seeing *each other's* data — it is not authentication
  against the world. If it will hold anything real, put it behind a private
  domain, a password, or Vercel's own access controls.
- **A hosted build stores server-side data in the server's filesystem.** On
  Vercel that is ephemeral: a redeploy or a cold start can wipe it. For
  durable hosted accounts you need a real database (Vercel Postgres, Supabase,
  Turso). The desktop build has no such problem — its files persist.
- **Running `--web --host-all` exposes the backend to your network.** Without
  TLS, anything on that network can read the traffic, including the session
  token. Keep it on `127.0.0.1` unless you have a reason and a trustworthy
  network.
- **A Windows window cannot be made truly unclosable.** Alt+F4 and Task
  Manager always win. The ad system's real protection is that closing an ad
  mid-way credits nothing — see `ads.grant_for_ad`.

---

## 5. Tests

```
python _isolate_test.py    # 24 - two accounts never share data (desktop)
python _web_test.py        # 21 - two browser tabs never share data (hosted)
python _ads_test.py        # 92 - the ad-for-time system
python _admin_test.py      # 49 - admin vs user
python _prompt_test.py     # 63 - the system prompt
python _security_test.py   # tool-use safety
python _e2e_test.py        # full flow against a fake provider
```

All of them pass. `_isolate_test.py` and `_web_test.py` are the two that
specifically reproduce and lock down the cross-account chat leak.
