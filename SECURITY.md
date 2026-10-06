# HAMU-GPT — what is protected, and what cannot be

This is written for the owner, in plain terms. It answers one question:
**if somebody tries to cheat the app or the website, what actually happens?**

---

## The one rule everything follows

**The browser is the user's.** Whatever runs there — DevTools, Tampermonkey,
a "Universal Global Web Editor" extension, a patched copy of the page — can
change anything it likes on **that user's screen**.

That is not a flaw in this app. It is how browsers work, and it is true of
every website in existence, including banks. **No site can prevent it.**

So this app does not try. Instead, nothing that matters is decided in the
browser.

| What | Where the real value lives |
|---|---|
| Chat-time balance | `ads.json` in the key-value store (Upstash) |
| How long a turn took | **the server's own clock** |
| Whether a turn is allowed | `nexus_app._time_gate`, before any model call |
| The check-in day | server, one claim per reward day |
| Accounts and passwords | server, hashed |
| The store token | server only — never sent to a browser |

---

## What a page editor can and cannot do

The extension in the screenshot can change `#timeVal` from `0:00` to `10:00`
and make it survive a refresh. It keeps its own copy of the page inside the
user's browser and re-applies the edit on every load.

**What that achieves: nothing.**

1. The number it changed is a **label**. It is not what the server checks.
2. When that user presses Send, the server looks at the **real** balance in
   the store — which the extension has no way to reach — and refuses the turn.
3. The interface **repaints from the server's values every 4 seconds**, and
   again whenever the tab regains focus, so the edit is wiped on its own.

The user gets a wrong-looking clock and a chat that will not send. That is the
worst they can do.

### And it cannot hide the ads

Hiding an ad element is possible, and it costs one impression. It cannot be
prevented, and it is not worth chasing: every ad network on earth has this, and
Adsterra pays on served impressions from its own side.

---

## Verified by audit

Run against the real server, not reasoned about:

| Check | Result |
|---|---|
| Does any API response contain the store token or URL? | **No** — checked `auth_status`, `ad_status`, `storage_scope`, `whoami`, `prompt_status` |
| Can a normal user call `ad_board` (the owner's board)? | **No** — `{"ok": false, "error": "Admin only."}` |
| Can a normal user call `ad_reset` (wipe an account)? | **No** — `Admin only.` |
| 11 rapid check-in claims in a row | **60 minutes**, not 660 |
| Can the client under-report its own chat time? | **No** — it sends no number; the server times the turn |
| Can a client send a huge number to drain someone? | **No** — the argument is ignored |
| Can agent mode be reached over HTTP? | **No** — refused, "only available in the desktop app" |
| Can a LAN visitor read the server's Chrome profiles? | **No** — `_local_chrome_ok()` requires the request to come from the machine itself |

`_tamper_test.py` covers the time system in **28 checks**; `_hosted_test.py`
covers the web build in **27**.

---

## The desktop app additionally

- **`integrity.json`** — the app re-hashes its own files before opening the
  window and refuses to start if they were altered. The installer writes this
  manifest, so editing the shipped `ui/index.html` on disk is caught.
- **Program Files is ACL-locked** by the installer, so the files are not
  writable by a normal user in the first place.
- **Agent tools** are reachable only through the desktop shell, never over the
  network.

---

## Where the honest limit is

If someone is determined and technical enough, they can run a modified copy of
the *client* and make it say anything. They cannot make the **server** agree.

The only thing that would be a real problem is if a secret lived in the page.
Nothing does — the store token is the one that matters, and it never leaves the
server. That is the single check worth repeating after any change:

```bash
curl -s -X POST http://127.0.0.1:8000/api/ad_status \
  -H 'Content-Type: application/json' -d '{"args":[]}' | grep -c upstash
# must print 0
```

---

## If you ever see a real hole

Tell me the exact steps. Anything that lets a user **gain time they did not
earn**, **reach another account**, or **read the store token** is a genuine bug
and gets fixed immediately. Everything else is the browser being the browser.
