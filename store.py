#!/usr/bin/env python3
"""Persistence for Nexus Chat.

Two files under the app data directory:

  providers.json  - configured providers. API keys are encrypted with the
                    Windows Data Protection API (DPAPI), which ties the
                    ciphertext to the current Windows user account. On
                    non-Windows (or if DPAPI is unavailable) we fall back to
                    base64 and mark the record so the UI can say so honestly.
  usage.json      - local request/token counters, per provider per day.

Chat threads stay in the browser's localStorage - they never need to touch
the filesystem.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import datetime as _dt
import json
import os
import threading
import time

APP = "HAMU-GPT"
LEGACY_APPS = ("NexusChat",)      # migrate config from the old name
# the folder this module lives in. In the frozen desktop app that is the
# PyInstaller bundle; in the web build it is the deployed project folder.
# server_config() uses it to find a server.json that travels with the code.
HERE = os.path.dirname(os.path.abspath(__file__))
_lock = threading.Lock()
_migrated = False


# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------
def hosted() -> bool:
    """Are we running as a hosted web service rather than the desktop app?

    This matters for two reasons: a serverless filesystem is read-only except
    for a temp dir, and the local agent tools must never be reachable by an
    anonymous visitor. Set HAMU_HOSTED=1 to force it (used by the web build
    and the tests).
    """
    if os.environ.get("HAMU_HOSTED"):
        return True
    for k in ("VERCEL", "VERCEL_ENV", "NOW_REGION",
              "AWS_LAMBDA_FUNCTION_NAME", "NETLIFY", "RENDER", "DYNO",
              "FLY_APP_NAME", "K_SERVICE"):
        if os.environ.get(k):
            return True
    return False


def data_dir() -> str:
    """Where this instance keeps its files.

    Windows desktop: %APPDATA%/HAMU-GPT
    Hosted/serverless: the temp dir, because everything else is read-only.
    Elsewhere: ~/.hamu-gpt

    On first run after the rename we copy any config from the previous folder
    name so the user doesn't lose their saved providers.
    """
    global _migrated
    import tempfile

    if hosted():
        # Serverless hosts mount the app read-only; /tmp is the only writable
        # place. It is per-instance and disappears on a cold start - see the
        # persistence note in DEPLOY.md.
        d = os.path.join(tempfile.gettempdir(), APP)
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d

    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.expanduser("~")

    if not _migrated:
        _migrated = True
        for old in LEGACY_APPS:
            src = os.path.join(base, old)
            if not os.path.isdir(src):
                continue
            for fname in ("providers.json", "usage.json"):
                dst_f = os.path.join(d, fname)
                src_f = os.path.join(src, fname)
                if os.path.exists(src_f) and not os.path.exists(dst_f):
                    try:
                        import shutil
                        shutil.copy2(src_f, dst_f)
                    except Exception:
                        pass
    return d


PROVIDERS_FILE = os.path.join(data_dir(), "providers.json")
USAGE_FILE = os.path.join(data_dir(), "usage.json")


# ---------------------------------------------------------------------------
# per-account scoping
# ---------------------------------------------------------------------------
# Everything below is keyed by the signed-in email, so two people sharing one
# PC keep separate providers, usage counters and chats - exactly like two
# accounts on a website. The account file itself stays global (it is the
# session), but nothing else does.
def account_key() -> str:
    """The lowercase email of the signed-in account, or '' when signed out.

    This must route through ``auth.is_signed_in()``, not just read the stored
    email: a signed-out session still has an email on disk, and treating that
    as live would let a signed-out app write into the previous user's folder.
    """
    try:
        import auth
        if not auth.is_signed_in():
            return ""
        return ((auth.get_account() or {}).get("email") or "").strip().lower()
    except Exception:
        return ""


def _acct_file(name: str) -> str:
    """Where a per-account file lives, or the global one when signed out."""
    k = account_key()
    if not k:
        return os.path.join(data_dir(), name)
    safe = "".join(c if (c.isalnum() or c in "._@-") else "_" for c in k)[:120]
    d = os.path.join(data_dir(), "accounts", safe)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return os.path.join(data_dir(), name)
    dst = os.path.join(d, name)
    # First run after the per-account switch: hand the old global files to
    # whoever is signed in now, so nobody loses their providers or chats.
    _adopt_legacy(dst, os.path.join(data_dir(), name))
    return dst


_ADOPTED = set()

# Sources already handed over, keyed by the source PATH (one entry per file
# type). This is only a fast path - the real, durable guard is that the source
# file is renamed away after a successful hand-over.
_LEGACY_CLAIMED = set()


def _adopt_legacy(dst: str, src: str):
    """Hand a pre-per-account global file to ONE account, once, for ever.

    The guard has to be durable. This runs on every app start, so an in-memory
    "already claimed" flag resets each time - which meant the global file was
    handed to whichever account happened to sign in FIRST after every restart.
    That is exactly how one person's providers ended up appearing in everybody
    else's account.

    So after a successful hand-over the source is RENAMED out of the way. From
    then on there is nothing left to adopt, and no later account can inherit it.

    Keyed per source path, because there is one global file per type (providers,
    usage, threads) and each has to be dealt with once.
    """
    if src in _LEGACY_CLAIMED:
        return
    _LEGACY_CLAIMED.add(src)
    if hosted():
        # Nothing to adopt: a hosted instance starts with an empty store, and
        # there are no pre-per-account files to hand over.
        return
    try:
        if not os.path.exists(src):
            return                       # nothing to hand over
        if not os.path.exists(dst):
            # This account is the one that was active when storage became
            # per-account, so it inherits the file.
            import shutil
            shutil.copy2(src, dst)
        # Either way the global file is spent now. Burn it so the next restart
        # - or the next account - cannot pick it up again.
        try:
            os.replace(src, src + ".imported")
        except Exception:
            pass
    except Exception:
        pass


def _account_name() -> str:
    """A human-readable label for a stored folder name (reverse lookup)."""
    return account_key()


def account_dir(email: str = "") -> str:
    """The folder holding one account's files (created if missing).

    Exposed so the app can report where a signed-in user's data lives, and so
    an admin can inspect isolation without guessing the folder name.
    """
    k = (email or account_key()).strip().lower()
    if not k:
        return data_dir()
    safe = "".join(c if (c.isalnum() or c in "._@-") else "_" for c in k)[:120]
    d = os.path.join(data_dir(), "accounts", safe)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return data_dir()
    return d


def list_account_dirs() -> list:
    """Every account this instance knows, with a size summary. Admin use only."""
    if use_kv():
        # No folders to list, so the key names are the inventory: every key
        # under hamu:accounts/<email>/ tells us that account exists.
        out = {}
        for k in _kv_keys(_KV_PREFIX + "accounts/*"):
            rest = k[len(_KV_PREFIX) + len("accounts/"):]
            if "/" not in rest:
                continue
            email, _, fname = rest.partition("/")
            if not email:
                continue
            files = out.setdefault(email, {})
            raw = _kv_get(k)
            files[fname] = len(raw) if isinstance(raw, str) else -1
        return [{"folder": e, "path": "kv:accounts/" + e, "files": f}
                for e, f in sorted(out.items())]

    root = os.path.join(data_dir(), "accounts")
    out = []
    try:
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            files = {}
            for f in ("providers.json", "usage.json", "threads.json"):
                fp = os.path.join(d, f)
                if os.path.exists(fp):
                    try:
                        files[f] = os.path.getsize(fp)
                    except Exception:
                        files[f] = -1
            out.append({"folder": name, "path": d, "files": files})
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# DPAPI (Windows only) - real encryption, no extra dependencies
# ---------------------------------------------------------------------------
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_available() -> bool:
    if os.name != "nt":
        return False
    try:
        ctypes.windll.crypt32
        return True
    except Exception:
        return False


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(b: _Blob) -> bytes:
    return ctypes.string_at(b.pbData, b.cbData)


def _encrypt(plain: str) -> str:
    if not plain:
        return ""
    if _dpapi_available():
        try:
            out = _Blob()
            ok = ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(_blob(plain.encode("utf-8"))), None, None, None,
                None, 0, ctypes.byref(out))
            if ok:
                raw = _blob_bytes(out)
                ctypes.windll.kernel32.LocalFree(out.pbData)
                return "dpapi:" + base64.b64encode(raw).decode()
        except Exception:
            pass
    # honest fallback - obfuscation only
    return "b64:" + base64.b64encode(plain.encode("utf-8")).decode()


def _decrypt(stored: str) -> str:
    if not stored:
        return ""
    try:
        if stored.startswith("dpapi:") and _dpapi_available():
            raw = base64.b64decode(stored[6:])
            out = _Blob()
            ok = ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(_blob(raw)), None, None, None, None, 0,
                ctypes.byref(out))
            if ok:
                val = _blob_bytes(out).decode("utf-8", "replace")
                ctypes.windll.kernel32.LocalFree(out.pbData)
                return val
            return ""
        if stored.startswith("b64:"):
            return base64.b64decode(stored[4:]).decode("utf-8", "replace")
    except Exception:
        return ""
    return ""


def key_storage_kind() -> str:
    return "Windows DPAPI (per-user encryption)" if _dpapi_available() else "base64 (no OS encryption available)"


# ---------------------------------------------------------------------------
# hosted storage: a key-value store instead of the filesystem
# ---------------------------------------------------------------------------
# A serverless host mounts the app read-only and wipes /tmp on every cold start,
# so anything saved to a file simply vanishes - accounts included. When hosted,
# every read and write goes to a Redis key-value store over its REST API
# instead.
#
# That REST API is why this needs no client library: plain urllib is enough, so
# the web build stays standard-library-only and requirements.txt stays empty.
#
# Set up by connecting Upstash Redis in the Vercel dashboard; it then provides
# KV_REST_API_URL and KV_REST_API_TOKEN. UPSTASH_* names are accepted too, as
# are the HAMU_KV_* ones for running this locally.
_KV_PREFIX = "hamu:"


def server_config() -> dict:
    """The owner's server.json, if one is present.

    Two places are searched, in order:

    1. ``%APPDATA%/HAMU-GPT/server.json`` - how the *installed desktop app* is
       pointed at a shared key-value store without anyone setting an env var.
    2. ``server.json`` next to this file - how the *web build* carries its own
       config, since a hosted deploy has no %APPDATA% and the folder is the
       only thing that travels with the code.

    Shape:

        {"use_kv": true,
         "kv_url": "https://xxx.upstash.io",
         "kv_token": "AX...."}

    Environment variables still win (see _kv_config), so a Vercel deploy is
    unaffected. Any unreadable or malformed file is simply ignored.
    """
    candidates = []
    try:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        candidates.append(os.path.join(base, APP, "server.json"))
    except Exception:
        pass
    # the folder this module lives in - the web build ships its config here
    try:
        candidates.append(os.path.join(HERE, "server.json"))
    except Exception:
        pass

    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d:
                return d
        except Exception:
            continue
    return {}


def _kv_config():
    """(url, token) for the key-value store, or (None, None) when unset."""
    cfg = server_config()
    url = (os.environ.get("KV_REST_API_URL")
           or os.environ.get("UPSTASH_REDIS_REST_URL")
           or os.environ.get("HAMU_KV_URL")
           or cfg.get("kv_url") or "").strip().rstrip("/")
    tok = (os.environ.get("KV_REST_API_TOKEN")
           or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
           or os.environ.get("HAMU_KV_TOKEN")
           or cfg.get("kv_token") or "").strip()
    return (url, tok) if url and tok else (None, None)


def kv_ready() -> bool:
    """Is a key-value store configured?"""
    return _kv_config()[0] is not None


def kv_kind() -> str:
    if not kv_ready():
        return "files (no key-value store configured)"
    if not use_kv():
        return "key-value store (configured, but this is the desktop build)"
    return "key-value store (Upstash Redis)"


def _kv_cmd(*args, timeout=10):
    """Run one Redis command over the REST API. Returns the result or None.

    Sent as a JSON array in the body rather than in the URL, because a chat
    history is far too big to put in a path.

    RETRIES ONCE. A single dropped connection used to return None, and None
    reads as "this key does not exist" - so one network blip could make an
    account look like it had never been created, or make the check-in look
    unclaimed. On a serverless host, where every cold start opens fresh
    connections, that blip is common enough to matter. Two attempts turn it
    from a data-loss-looking bug into a few hundred milliseconds.
    """
    url, tok = _kv_config()
    if not url:
        return None
    import urllib.request
    body = json.dumps(list(args)).encode("utf-8")
    headers = {"Authorization": "Bearer " + tok,
               "Content-Type": "application/json"}
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.loads(r.read().decode("utf-8"))
            if isinstance(j, dict) and j.get("error"):
                return None          # a real error from the store: do not retry
            return j.get("result") if isinstance(j, dict) else None
        except Exception:
            if attempt == 0:
                time.sleep(0.4)
                continue
            return None
    return None


def _kv_get(key: str):
    return _kv_cmd("GET", key)


def _kv_set(key: str, value: str):
    return _kv_cmd("SET", key, value)


def _kv_del(key: str):
    return _kv_cmd("DEL", key)


def _kv_keys(pattern: str) -> list:
    r = _kv_cmd("KEYS", pattern)
    return r if isinstance(r, list) else []


def _kv_key(path: str) -> str:
    """Map a data-dir path onto a key, so callers keep passing paths.

    `<data>/providers.json`                  -> hamu:providers.json
    `<data>/accounts/a@b.com/threads.json`   -> hamu:accounts/a@b.com/threads.json
    """
    try:
        root = os.path.normpath(data_dir())
        full = os.path.normpath(path)
        rel = os.path.relpath(full, root)
    except Exception:
        rel = os.path.basename(path)
    if rel.startswith("..") or os.path.isabs(rel):
        rel = os.path.basename(path)
    return _KV_PREFIX + rel.replace("\\", "/")


def kv_selftest() -> dict:
    """Write, read back and delete one key. Used by --selftest and the tests."""
    if not kv_ready():
        return {"ok": False, "error": "no key-value store configured"}
    probe = _KV_PREFIX + "__selftest__"
    _kv_set(probe, "ok")
    got = _kv_get(probe)
    _kv_del(probe)
    return {"ok": got == "ok", "read_back": got}


# ---------------------------------------------------------------------------
# desktop opt-in: the installed app can share one key-value store too
# ---------------------------------------------------------------------------
# A hosted deploy always uses the store. The *installed desktop app* can now
# opt in as well, so that the same email+password works on every PC and the
# owner's ad-time ledger is central instead of trapped on one machine.
#
# What stays local, and why: `account.json` is "who is signed in on THIS PC".
# Sharing it across machines would make two users fight over one session, so
# it is deliberately excluded. Everything else - the account registry (the
# email + password hash for every account), each account's providers, usage
# and threads, and the ads ledger - goes to the store.
_KV_LOCAL_ONLY = ("account.json",)


def _is_local_only(path: str) -> bool:
    return os.path.basename(path) in _KV_LOCAL_ONLY


def use_kv() -> bool:
    """Should reads and writes go to the key-value store on this build?"""
    if os.environ.get("HAMU_NO_KV"):
        # escape hatch: force plain local files even when a store is
        # configured. Handy for tests and for debugging a desktop install.
        return False
    if not kv_ready():
        return False
    if hosted():
        return True
    if os.environ.get("HAMU_USE_KV"):
        return True
    return bool(server_config().get("use_kv"))


# ---------------------------------------------------------------------------
# json io
# ---------------------------------------------------------------------------
def _read(path, default):
    if use_kv() and not _is_local_only(path):
        raw = _kv_get(_kv_key(path))
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, obj):
    if use_kv() and not _is_local_only(path):
        try:
            _kv_set(_kv_key(path), json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass
        return
    # Make sure the folder is really there before writing. data_dir() creates
    # it, but a hosted box can hand out a fresh /tmp, and a cleaner can remove
    # the folder under a running process - either way a missing parent used to
    # surface as FileNotFoundError on account.json.tmp instead of just working.
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    except Exception:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------
def load_providers() -> list:
    raw = _read(_acct_file("providers.json"), [])
    if not isinstance(raw, list):
        return []
    out = []
    for p in raw:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        p = dict(p)
        p["key"] = _decrypt(p.pop("key_enc", "") or p.get("key", ""))
        p.setdefault("models", [])
        p.setdefault("enabled", True)
        out.append(p)
    return out


def save_providers(providers: list):
    safe = []
    for p in providers:
        q = dict(p)
        q["key_enc"] = _encrypt(q.pop("key", "") or "")
        safe.append(q)
    with _lock:
        _write(_acct_file("providers.json"), safe)


# ---------------------------------------------------------------------------
# local usage counters
# ---------------------------------------------------------------------------
def _today() -> str:
    return _dt.date.today().isoformat()


def load_usage() -> dict:
    u = _read(_acct_file("usage.json"), {})
    return u if isinstance(u, dict) else {}


def bump_usage(provider_id: str, in_tokens=0, out_tokens=0, requests=1, cost=None):
    """Increment today's counters for a provider."""
    with _lock:
        u = load_usage()
        rec = u.setdefault(provider_id, {})
        day = _today()
        if rec.get("date") != day:
            rec["history"] = (rec.get("history") or [])[-30:]
            if rec.get("date"):
                rec["history"].append({
                    "date": rec["date"],
                    "requests": rec.get("requests", 0),
                    "in": rec.get("in", 0),
                    "out": rec.get("out", 0),
                    "cost": rec.get("cost", 0.0),
                })
            rec.update({"date": day, "requests": 0, "in": 0, "out": 0, "cost": 0.0})
        rec["requests"] = int(rec.get("requests", 0)) + requests
        rec["in"] = int(rec.get("in", 0)) + int(in_tokens or 0)
        rec["out"] = int(rec.get("out", 0)) + int(out_tokens or 0)
        if cost is not None:
            rec["cost"] = float(rec.get("cost", 0.0)) + float(cost)
        u[provider_id] = rec
        _write(_acct_file("usage.json"), u)
    return rec


def usage_for(provider_id: str) -> dict:
    rec = load_usage().get(provider_id) or {}
    if rec.get("date") != _today():
        return {"date": _today(), "requests": 0, "in": 0, "out": 0, "cost": 0.0,
                "history": rec.get("history") or []}
    return rec


def reset_usage(provider_id: str | None = None):
    with _lock:
        if provider_id:
            u = load_usage()
            u.pop(provider_id, None)
            _write(_acct_file("usage.json"), u)
        else:
            _write(_acct_file("usage.json"), {})


# ---------------------------------------------------------------------------
# chat threads
# ---------------------------------------------------------------------------
# Kept on disk rather than in the WebView's localStorage, so conversations
# survive app updates, cache clears, and WebView profile resets - and so each
# signed-in account sees only its own.
#
# Every thread carries an `owner` field: the email it was created under. On
# load, anything whose owner is a *different* account is dropped. This is the
# backstop that makes a stray thread impossible to hand to the wrong person,
# even if a file was copied or a build wrote to the wrong folder.
MAX_THREADS = 200
MAX_MESSAGES = 400


def _owner_tag() -> str:
    """The account key a thread written right now should carry."""
    return account_key()


def _owns(tag: str) -> bool:
    """Does `tag` belong to the account signed in now?

    An empty tag means the record predates the ownership field; it is accepted
    only when the thread sits in the signed-in account's own folder, which the
    call site already guarantees.
    """
    cur = _owner_tag()
    t = (tag or "").strip().lower()
    if not t:
        return True                 # legacy record in the right folder
    if not cur:
        return False                # signed out: nothing is ours
    return t == cur


def load_threads() -> list:
    raw = _read(_acct_file("threads.json"), [])
    if not isinstance(raw, list):
        return []
    out = []
    dropped = 0
    for t in raw:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        if not _owns(t.get("owner", "")):
            dropped += 1
            continue
        msgs = t.get("messages")
        out.append({
            "id": t["id"],
            "title": t.get("title", ""),
            "model": t.get("model", ""),
            "created": t.get("created"),
            "updated": t.get("updated"),
            "messages": msgs if isinstance(msgs, list) else [],
        })
    if dropped:
        # Persist the cleaned list so a foreign thread is not re-examined on
        # every boot. Best-effort: a failed write just means we drop it again.
        try:
            save_threads([dict(t, owner=_owner_tag()) for t in out])
        except Exception:
            pass
    return out


def save_threads(threads: list):
    if not isinstance(threads, list):
        return
    owner = _owner_tag()
    safe = []
    for t in threads[:MAX_THREADS]:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        # Never let a thread from another account be written into this file.
        if t.get("owner") and not _owns(t.get("owner")):
            continue
        msgs = t.get("messages")
        safe.append({
            "id": t["id"],
            "title": t.get("title", ""),
            "model": t.get("model", ""),
            "created": t.get("created"),
            "updated": t.get("updated"),
            "owner": owner,
            "messages": (msgs or [])[-MAX_MESSAGES:] if isinstance(msgs, list) else [],
        })
    with _lock:
        _write(_acct_file("threads.json"), safe)


def clear_threads():
    with _lock:
        _write(_acct_file("threads.json"), [])
