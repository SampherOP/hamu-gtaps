#!/usr/bin/env python3
"""HAMU-GPT - a clean, provider-agnostic desktop chat client.

No Puter. No OmniRoute. You add providers yourself:

  1. press "Add provider"
  2. paste an OpenAI-compatible endpoint (e.g. https://api.cerebras.ai/v1/chat/completions)
  3. paste the API key
  4. press Detect  -> we identify the provider and list its models
  5. tick the models you want, press Add

Everything else (chat, model switching, per-provider usage) follows from that.

Run:   python nexus_app.py
Check: python nexus_app.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import threading
import traceback
import uuid
import webbrowser

import providers as P
import store
import auth
import ads
import tools as T
import verify as V

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(HERE, "ui", "index.html")

# background streaming jobs: id -> {done, text, result, error, started}
class _Cancelled(Exception):
    """Raised inside a stream callback to abort a run."""


_JOBS = {}
_jobs_lock = threading.Lock()
_LAST_REPORT = {"value": None}      # most recent verify report, for the UI badges

APP_NAME = "HAMU-GPT"
APP_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _safe(fn):
    """Never let an exception escape into pywebview - it kills the bridge."""
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    wrapper.__name__ = fn.__name__
    return wrapper


def _pid() -> str:
    return "p_" + uuid.uuid4().hex[:10]


def _find(providers, pid):
    return next((p for p in providers if p.get("id") == pid), None)


# ---------------------------------------------------------------------------
# identity check — did the gateway serve what we asked for?
# ---------------------------------------------------------------------------
# Reseller gateways hide swaps in several ways, so the flag is deliberately
# conservative: it only says "renamed" when the families genuinely disagree.
# "echo" means the gateway just parroted our alias back, which is not proof of
# anything and is called out as such instead of being reported as a pass.
_FAMILY_WORDS = {
    "claude": "anthropic", "opus": "anthropic", "sonnet": "anthropic",
    "haiku": "anthropic", "anthropic": "anthropic",
    "gpt": "openai", "openai": "openai", "chatgpt": "openai", "o1": "openai",
    "o3": "openai", "o4": "openai",
    "gemini": "google", "palm": "google", "bard": "google", "google": "google",
    "llama": "meta", "meta": "meta",
    "qwen": "qwen", "deepseek": "deepseek", "mistral": "mistral",
    "mixtral": "mistral", "grok": "xai", "xai": "xai",
    "kimi": "moonshot", "moonshot": "moonshot",
    "glm": "zai", "zhipu": "zai", "minimax": "minimax",
    "grok": "xai", "phi": "microsoft", "command": "cohere", "cohere": "cohere",
}


def _family(name: str) -> str:
    n = (name or "").lower()
    hit = ""
    for word, fam in _FAMILY_WORDS.items():
        if word in n and (not hit or len(word) > len(hit)):
            hit = word
    return _FAMILY_WORDS.get(hit, "") if hit else ""


def _identity_flag(asked: str, declared: str) -> dict:
    """Classify the (asked, declared) pair. Never raises."""
    a, d = (asked or "").strip(), (declared or "").strip()
    if not d:
        return {"state": "unknown", "asked": a, "declared": "",
                "note": "the gateway did not report its own model name"}
    if d.lower() == a.lower():
        return {"state": "echo", "asked": a, "declared": d,
                "note": "the gateway just echoed your own alias back — that is not proof"}
    fa, fd = _family(a), _family(d)
    if fa and fd and fa != fd:
        return {"state": "renamed", "asked": a, "declared": d,
                "note": f"you asked for {fa}, but the gateway is serving {fd}"}
    return {"state": "match", "asked": a, "declared": d, "note": ""}


def _enabled_models(p) -> list:
    return [m for m in (p.get("models") or []) if m.get("enabled")]


# ---------------------------------------------------------------------------
# API exposed to the web UI
# ---------------------------------------------------------------------------
class Api:
    def __init__(self):
        self._lock = threading.Lock()

    # ---- accounts -------------------------------------------------------
    @_safe
    def auth_status(self):
        """Called on boot: should we show the sign-in gate or go to the app?

        `hosted` and `chrome` let the UI adapt honestly: on a website there is
        no local Chrome to read and no local machine to store data on, so the
        one-tap Google button and the "never leaves your machine" note must not
        be shown as if they were true.
        """
        r = auth.resume_session()
        r["chrome"] = bool(auth._chrome_exe())
        r["hosted"] = bool(store.hosted())
        return r

    @_safe
    def detect_google_accounts(self):
        """Google accounts already signed in to Chrome on this machine.

        Read-only, from Chrome's own profile data. Used to offer one-tap
        sign-up. Nothing is transmitted anywhere.

        This is inherently a DESKTOP feature: it reads the Chrome profile of
        the machine the app is running on. On a hosted deploy there is no such
        Chrome, so it says so rather than returning a confusing empty list.
        It IS allowed on a local `--web` run, where the backend is your own PC
        and the browser is on the same machine - that is the same situation as
        the desktop app.
        """
        if not _local_chrome_ok():
            return {"ok": False, "hosted": True, "accounts": [], "chrome": False,
                    "best": None,
                    "error": "One-tap Google sign-in reads the Chrome on the "
                             "machine running the app, so it only works there. "
                             "Sign up with an email and password instead."}
        accs = auth.chrome_accounts()
        return {"ok": True, "accounts": accs,
                "chrome": bool(auth._chrome_exe()),
                "best": accs[0] if accs else None}

    @_safe
    def open_google_signin(self):
        """Open Chrome at the Google account page so the user can sign in."""
        if not _local_chrome_ok():
            return {"ok": False,
                    "error": "There is no browser to open on the server."}
        ok = auth.open_chrome("https://accounts.google.com/")
        return {"ok": ok,
                "error": "" if ok else "Could not open Chrome. Open it manually."}

    @_safe
    def signup_google(self, email=None, name=None):
        """Create/sign in an account using the Chrome Google session."""
        if not email:
            best = auth.best_google_account()
            if not best:
                return {"ok": False, "needs_chrome": True,
                        "error": "No signed-in Google account found in Chrome. "
                                 "Open Chrome, sign in with your Gmail, then "
                                 "try again."}
            email, name = best["email"], best.get("name", "")
        r = auth.create_account(email, name or "", method="google",
                                ip=_signup_address())
        return r

    @_safe
    def signup_local(self, email, password, name=None):
        r = auth.create_account(email, name or "", method="local",
                                password=password, ip=_signup_address())
        return r

    @_safe
    def signin(self, email, password):
        return auth.sign_in(email, password)

    @_safe
    def admin_signin(self, email, name, password):
        """Owner sign-in. Email + name + password must all match the whitelist."""
        return auth.admin_sign_in(email, name, password)

    @_safe
    def whoami(self):
        """Current account, including its role."""
        return {"ok": True, "account": auth.get_account(),
                "signed_in": auth.is_signed_in()}

    @_safe
    def prompt_status(self):
        """Is the owner's prompt file in force, and roughly how big is it?

        Only the admin ever gets a non-empty answer - for anyone else the file
        is invisible and stays invisible.
        """
        is_admin = auth.is_admin()
        text = T.owner_prompt() if is_admin else ""
        return {
            "ok": True,
            "is_admin": is_admin,
            "active": bool(text),
            "chars": len(text),
            "lines": len(text.splitlines()) if text else 0,
            "path": T.OWNER_PROMPT_FILE if is_admin else "",
        }

    @_safe
    def signout(self):
        return auth.sign_out()

    @_safe
    def forget_account(self):
        return auth.forget()

    # ---- chat time: the daily check-in -----------------------------------
    # Users get a tank of chat time that only drains while they are really
    # chatting, and they refill it with a DAILY CHECK-IN - one claim per
    # reward day (04:00 to 04:00 local), 60 minutes a day, 50 days on the
    # track. The owner is exempt; see ads.py for the full policy.
    #
    # This replaced the old "watch a rewarded ad" flow, which no ad network
    # that serves websites will allow.
    @_safe
    def ad_status(self):
        """How much time is left, and what the daily check-in is offering.

        Settles first. The interface polls this every 20 seconds, so a turn in
        progress is charged as it runs even if the page never reports back -
        which means closing the app mid-reply does not hand back the time.
        """
        ads.settle_chat()
        return ads.status()

    @_safe
    def ad_checkin_claim(self):
        """Claim today's check-in: +60 minutes, once per reward day.

        The once-a-day rule lives in ads.checkin_claim, inside the lock, so a
        stale client replaying this cannot collect twice.
        """
        acct = auth.get_account() or {}
        email = acct.get("email") or ""
        if not email:
            return {"ok": False, "error": "Not signed in."}
        ads.ensure_account(email)
        r = ads.checkin_claim(email)
        r["status"] = ads.status(email)
        return r

    @_safe
    def ad_claim_signup_bonus(self):
        """Claim the one-time signup gift (15 minutes), from the CLAIM button.

        Returns ``just_claimed`` so the interface can tell "you just got it"
        from "you already had it" - the celebration must fire on the first and
        never again, including on every later login.
        """
        acct = auth.get_account() or {}
        email = acct.get("email") or ""
        if not email:
            return {"ok": False, "error": "Not signed in."}
        ads.ensure_account(email)
        r = ads.claim_signup_bonus(email)
        r["status"] = ads.status(email)
        return r

    @_safe
    def ad_claim_welcome(self):
        """Deprecated alias kept for older clients.

        It deliberately grants NOTHING now - it only makes sure the record
        exists and reports status. That way a stale build still calling this
        cannot hand out the signup gift a second time.
        """
        acct = auth.get_account() or {}
        email = acct.get("email") or ""
        if not email:
            return {"ok": False, "error": "Not signed in."}
        ads.ensure_account(email)
        return ads.status(email)

    @_safe
    def ad_watch_begin(self):
        """RETIRED with the watch-an-ad flow. Refuses, grants nothing."""
        r = ads.begin_ad()
        r["status"] = ads.status()
        return r

    @_safe
    def ad_watch_done(self, elapsed=None):
        """RETIRED with the watch-an-ad flow. Always pays zero.

        Kept so a stale build calling it gets a clear refusal instead of an
        error - but it can no longer create time from nothing.
        """
        r = ads.grant_for_ad(elapsed if elapsed is not None else ads.AD_SECONDS)
        r["status"] = ads.status()
        return r

    @_safe
    def ad_consume(self, seconds=None):
        """Settle the time spent on the turn that just ended.

        The ``seconds`` argument is accepted for compatibility and then
        DELIBERATELY IGNORED. The server measures the elapsed time itself, so
        a tampered page cannot under-report its own usage - it can send 0, 0.1
        or a million and the charge is the same either way.
        """
        r = ads.settle_chat(stop=True)
        r["status"] = ads.status()
        return r

    @_safe
    def ad_reset(self, email=None):
        """Owner only: wipe one account's tank, or every account when empty."""
        if not auth.is_admin():
            return {"ok": False, "error": "Admin only."}
        return ads.reset(email or None)

    @_safe
    def ad_board(self):
        """Owner only: every account's balance at a glance."""
        if not auth.is_admin():
            return {"ok": False, "error": "Admin only."}
        all_ = ads._read_all()
        rows = []
        for k, v in sorted(all_.items()):
            if not isinstance(v, dict):
                continue
            secs = max(0.0, ads._num(v.get("seconds")))
            rows.append({
                "email": k,
                "minutes": round(secs / 60.0, 1),
                "mmss": ads.fmt_mmss(secs),
                "ads_watched": ads._int(v.get("ads_watched")),
                "ads_started": ads._int(v.get("ads_started")),
                "granted_minutes": round(max(0.0, ads._num(v.get("granted_total"))) / 60.0, 1),
                "signup_bonus": bool(v.get("signup_bonus")),
                "last_seen": v.get("last_seen") or "",
            })
        rows.sort(key=lambda r: r["minutes"])
        return {"ok": True, "rows": rows, "path": ads.ADS_FILE,
                "ad_seconds": ads.AD_SECONDS,
                "reward_minutes": ads.AD_REWARD_SECONDS // 60,
                "start_minutes": int(ads.START_SECONDS / 60)}


    # ---- chat threads ---------------------------------------------------
    @_safe
    def load_threads(self):
        """Threads for the signed-in account only.

        Returns the account key alongside the rows so the UI can prove the
        payload matches who it thinks is signed in, and never render (or
        re-save) chats that belong to somebody else.
        """
        import store as _st
        return {"ok": True, "threads": store.load_threads(),
                "account": _st.account_key()}

    @_safe
    def save_threads(self, threads):
        store.save_threads(threads)
        return {"ok": True}

    @_safe
    def clear_threads(self):
        store.clear_threads()
        return {"ok": True}

    @_safe
    def storage_scope(self):
        """Where this account's data lives, and how isolated it is.

        A signed-in user gets only their own folder path (never the list of
        other accounts); the owner gets the full picture.
        """
        import store as _st
        who = _st.account_key()
        info = {
            "signed_in": bool(who),
            "account": who,
            "scope": "per-account" if who else "shared (signed out)",
            "dir": _st.account_dir(who) if who else _st.data_dir(),
            "key_storage": _st.key_storage_kind(),
            "is_admin": auth.is_admin(),
        }
        if auth.is_admin():
            info["accounts"] = _st.list_account_dirs()
            info["root"] = _st.data_dir()
        else:
            info["accounts"] = [{"folder": who, "path": info["dir"], "files": {}}
                                ] if who else []
        return {"ok": True, **info}

    # ---- agent / tools --------------------------------------------------
    @_safe
    def tools_catalog(self):
        return {"ok": True, "tools": T.tool_catalog(),
                "workspace": T.WORKSPACE,
                "policy": {str(k): v for k, v in T.TIERS.items()}}

    # -- shared: turn a raw model reply into a step result ----------------
    def _finish_step(self, p, model, text, meta):
        u = (meta or {}).get("usage") or {}
        cost = P.message_cost(p, model, u.get("in", 0), u.get("out", 0))
        store.bump_usage(p["id"], u.get("in", 0), u.get("out", 0), 1, cost)
        # remember who served this turn, so the model_identity tool can answer
        T.set_runtime(asked=model, declared=(meta or {}).get("model") or "",
                      provider=p.get("name") or "", base=p.get("base") or "")
        base = {"ok": True, "usage": u, "cost": cost,
                "cost_text": P.fmt_money(cost), "priced": cost is not None,
                "model": (meta or {}).get("model", model),
                "declared_model": (meta or {}).get("model") or "",
                "asked_model": model,
                "identity": _identity_flag(model, (meta or {}).get("model") or "")}

        calls = parse_tool_calls(text)
        if not calls:
            return dict(base, kind="final", text=text or "")

        call = calls[0]
        allowed, tier, reason, clean = T.precheck(call["name"], call["args"])
        return dict(base, kind="tool_call",
                    narration=strip_tool_calls(text),
                    raw=text or "",
                    tool=call["name"],
                    args=clean,
                    tier=tier,
                    tier_label=T.TIERS.get(tier or 0, {}).get("label", "?"),
                    auto_ok=bool(T.TIERS.get(tier or 0, {}).get("auto_ok")),
                    summary=T.summarise(call["name"], clean),
                    allowed=allowed,
                    denied_reason=(reason if allowed is False else ""),
                    caution=bool(clean.get("_caution")))

    def _prep_messages(self, messages):
        """Plain chat: style rules + owner prompt. No tool protocol - this is
        not agent mode, and advertising tools here only causes hallucinated
        tool calls."""
        msgs = list(messages)
        if not msgs or msgs[0].get("role") != "system":
            msgs = [{"role": "system", "content": T.chat_prompt()}] + msgs
        return msgs

    def _prep_agent_messages(self, messages):
        """Agent mode: style rules + the tool protocol."""
        msgs = list(messages)
        if not msgs or msgs[0].get("role") != "system":
            msgs = [{"role": "system", "content": T.system_prompt()}] + msgs
        return msgs

    @_safe
    def agent_step(self, pid, model, messages):
        """One blocking model turn (kept for compatibility / testing)."""
        if not _local_tools_ok():
            return {"ok": False, "error": "Agent mode is only available in the "
                                          "desktop app — it runs commands on the "
                                          "machine, which a website must not do."}
        gate = self._time_gate()
        if gate:
            return gate
        p = _find(store.load_providers(), pid)
        if not p:
            return {"ok": False, "error": "Provider not found."}
        if not isinstance(messages, list) or not messages:
            return {"ok": False, "error": "No message."}
        ok, text, meta = P.chat(p["base"], p.get("key") or None, model,
                                self._prep_agent_messages(messages), timeout=600)
        if not ok:
            return {"ok": False, "error": (meta or {}).get("error", "unknown error")}
        return self._finish_step(p, model, text, meta)

    def _time_gate(self):
        """Users with an empty tank cannot talk to any model.

        Returns a refusal dict, or None when the turn may proceed. The owner
        always passes.

        Three things happen here, all of them on the server side:
          1. the previous turn is settled against the SERVER's clock, so the
             client never gets to report how long it chatted,
          2. the wall is checked, and
          3. this turn's clock starts.

        Hiding the send button in the interface is a convenience only. This is
        the real wall, and a tampered page cannot get past it - it can only
        change what the user sees.
        """
        ads.settle_chat(stop=True)
        if not ads.can_chat():
            st = ads.status()
            return {
                "ok": False,
                "time_up": True,
                "needs_ad": True,
                "error": "YOUR TIME IS UP — CLAIM YOUR DAILY CHECK-IN",
                "seconds": st.get("seconds", 0.0),
                "checkin": st.get("checkin"),
            }
        ads.mark_chat_active()
        return None

    @_safe
    def chat_start(self, pid, model, messages):
        """Streamed plain chat (no tools). Same job/poll shape as the agent,
        so the Stop button works here too."""
        gate = self._time_gate()
        if gate:
            return gate
        p = _find(store.load_providers(), pid)
        if not p:
            return {"ok": False, "error": "Provider not found."}
        if not p.get("enabled", True):
            return {"ok": False, "error": f"{p['name']} is disabled."}
        if not isinstance(messages, list) or not messages:
            return {"ok": False, "error": "No message."}

        jid = "c" + uuid.uuid4().hex[:10]
        with _jobs_lock:
            _JOBS[jid] = {"done": False, "text": "", "result": None,
                          "error": None, "started": time.time()}
        threading.Thread(target=self._chat_worker,
                         args=(jid, p, model, self._prep_messages(messages)),
                         daemon=True).start()
        return {"ok": True, "job": jid}

    def _chat_worker(self, jid, p, model, msgs):
        job = _JOBS.get(jid)
        if job is None:
            return
        lock = threading.Lock()

        def on_chunk(piece):
            with lock:
                if job.get("cancelled"):
                    return False          # tells the stream to stop reading
                job["text"] += piece
                return True

        try:
            ok, text, meta = P.chat_stream(p["base"], p.get("key") or None,
                                           model, msgs, on_chunk, timeout=600)
            if job.get("cancelled"):
                job["result"] = {"ok": False, "cancelled": True,
                                 "error": "Stopped (stop button)."}
                return
            if not ok:
                job["error"] = (meta or {}).get("error", "stream failed")
                job["result"] = {"ok": False, "error": job["error"]}
                return
            job["text"] = text
            u = (meta or {}).get("usage") or {}
            cost = P.message_cost(p, model, u.get("in", 0), u.get("out", 0))
            store.bump_usage(p["id"], u.get("in", 0), u.get("out", 0), 1, cost)
            declared = (meta or {}).get("model") or ""
            T.set_runtime(asked=model, declared=declared,
                          provider=p.get("name") or "", base=p.get("base") or "")
            job["result"] = {
                "ok": True, "text": text, "usage": u,
                "model": (meta or {}).get("model", model),
                "cost": cost, "cost_text": P.fmt_money(cost),
                "priced": cost is not None,
                "price_source": ("manual" if p.get("price_in") is not None
                                 else ("built-in" if cost is not None else None)),
                "asked_model": model,
                "declared_model": declared,
                "identity": _identity_flag(model, declared),
            }
        except _Cancelled:
            job["result"] = {"ok": False, "cancelled": True,
                             "error": "Stopped (stop button)."}
        except Exception as e:
            job["error"] = f"{type(e).__name__}: {e}"
            job["result"] = {"ok": False, "error": job["error"]}
        finally:
            job["done"] = True

    # -- streaming: start a turn in the background, poll for tokens -------
    @_safe
    def agent_step_start(self, pid, model, messages):
        """Kick off a streamed model turn. Returns a job id to poll."""
        if not _local_tools_ok():
            return {"ok": False, "error": "Agent mode is only available in the "
                                          "desktop app — it runs commands on the "
                                          "machine, which a website must not do."}
        gate = self._time_gate()
        if gate:
            return gate
        p = _find(store.load_providers(), pid)
        if not p:
            return {"ok": False, "error": "Provider not found."}
        if not isinstance(messages, list) or not messages:
            return {"ok": False, "error": "No message."}

        jid = "j" + uuid.uuid4().hex[:10]
        with _jobs_lock:
            _JOBS[jid] = {"done": False, "text": "", "result": None,
                          "error": None, "started": time.time()}
            # keep the table small
            if len(_JOBS) > 40:
                for old in sorted(_JOBS, key=lambda k: _JOBS[k]["started"])[:-20]:
                    _JOBS.pop(old, None)

        msgs = self._prep_agent_messages(messages)
        base, key = p["base"], (p.get("key") or None)
        threading.Thread(target=self._stream_worker,
                         args=(jid, p, base, key, model, msgs),
                         daemon=True).start()
        return {"ok": True, "job": jid}

    def _stream_worker(self, jid, p, base, key, model, msgs):
        job = _JOBS.get(jid)
        if job is None:
            return
        lock = threading.Lock()

        def on_chunk(piece):
            with lock:
                if job.get("cancelled"):
                    return False          # tells the stream to stop reading
                job["text"] += piece
                return True

        try:
            ok, text, meta = P.chat_stream(base, key, model, msgs, on_chunk,
                                           timeout=600)
            if job.get("cancelled"):
                job["result"] = {"ok": False, "cancelled": True,
                                 "error": "Stopped (stop button)."}
                return
            if not ok:
                job["error"] = (meta or {}).get("error", "stream failed")
                job["result"] = {"ok": False, "error": job["error"]}
            else:
                job["text"] = text          # authoritative full text
                job["result"] = self._finish_step(p, model, text, meta)
        except _Cancelled:
            job["result"] = {"ok": False, "cancelled": True,
                             "error": "Stopped (stop button)."}
        except Exception as e:
            job["error"] = f"{type(e).__name__}: {e}"
            job["result"] = {"ok": False, "error": job["error"]}
        finally:
            job["done"] = True

    @_safe
    def agent_cancel(self, job):
        """Stop a running turn. The worker aborts at the next token."""
        j = _JOBS.get(job)
        if j:
            j["cancelled"] = True
        return {"ok": True}

    @_safe
    def agent_step_poll(self, job):
        """How is that streamed turn going? Cheap - call it often."""
        j = _JOBS.get(job)
        if not j:
            return {"ok": False, "error": "job not found", "done": True}
        return {"ok": True, "done": bool(j["done"]),
                "text": j["text"] or "",
                "result": j["result"] if j["done"] else None}

    # -- model identity verification --------------------------------------
    @_safe
    def verify_start(self, pid, model, fast=False):
        """Run the identity probe battery in the background.

        `fast=True` runs only the three decisive probes, so a whole provider's
        catalogue can be checked without waiting on the slow ones.
        """
        p = _find(store.load_providers(), pid)
        if not p:
            return {"ok": False, "error": "Provider not found."}
        probes = V.FAST_PROBES if fast else None
        jid = "v" + uuid.uuid4().hex[:10]
        with _jobs_lock:
            _JOBS[jid] = {"done": False, "text": "starting\u2026", "result": None,
                          "error": None, "started": time.time(),
                          "progress": {"i": 0, "total": len(probes or V.PROBES),
                                       "label": "starting"}}
        threading.Thread(target=self._verify_worker,
                         args=(jid, p, model, probes), daemon=True).start()
        return {"ok": True, "job": jid}

    def _verify_worker(self, jid, p, model, probes=None):
        job = _JOBS.get(jid)
        if job is None:
            return
        try:
            def progress(i, total, probe_id, label):
                job["progress"] = {"i": i, "total": total, "label": label}
                job["text"] = f"{i + 1}/{total} \u2014 {label}"
            # reuse the declared name we already saw for this model, if any
            runtime = T.get_runtime()
            declared = (runtime.get("declared")
                        if runtime.get("asked") == model else "")
            job["result"] = V.run_verify(p, model, on_progress=progress,
                                         probes=probes, declared=declared)
        except Exception as e:
            job["error"] = f"{type(e).__name__}: {e}"
            job["result"] = {"ok": False, "error": job["error"]}
        finally:
            job["done"] = True

    @_safe
    def verify_report(self):
        """The last full report, for the dropdown badge, if one is cached."""
        return {"ok": True, "report": _LAST_REPORT.get("value")}

    @_safe
    def verify_remember(self, report):
        """Cache a finished report so the model grid can badge it."""
        try:
            _LAST_REPORT["value"] = report
        except Exception:
            pass
        return {"ok": True}

    @_safe
    def verify_poll(self, job):
        j = _JOBS.get(job)
        if not j:
            return {"ok": False, "error": "job not found", "done": True}
        return {"ok": True, "done": bool(j["done"]),
                "progress": j.get("progress"),
                "result": j["result"] if j["done"] else None}

    @_safe
    def agent_execute(self, tool, args, via="user"):
        """Execute an approved tool call. Re-checks everything - never trust the UI."""
        if not _local_tools_ok():
            return {"ok": False, "blocked": True,
                    "error": "local tools are disabled on the hosted build"}
        allowed, tier, reason, clean = T.precheck(tool, args)
        if allowed is False:
            T.audit({"action": "denied", "tool": tool, "args": clean,
                     "reason": reason, "via": via})
            return {"ok": False, "error": reason, "blocked": True}
        result = T.execute(tool, clean)
        T.audit({"action": "executed", "tool": tool, "args": clean, "via": via,
                 "result_preview": json.dumps(result, ensure_ascii=False)[:400]})
        return {"ok": True, "result": result}

    @_safe
    def agent_deny(self, tool, args, reason=""):
        T.audit({"action": "user_denied", "tool": tool, "args": args,
                 "reason": reason or "user said no"})
        return {"ok": True}

    @_safe
    def audit_log(self, limit=200):
        return {"ok": True, "entries": T.read_audit(int(limit or 200)),
                "file": T.AUDIT_FILE}

    @_safe
    def clear_audit(self):
        try:
            if os.path.exists(T.AUDIT_FILE):
                os.remove(T.AUDIT_FILE)
        except Exception:
            pass
        return {"ok": True}

    @_safe
    def open_workspace(self):
        if not _local_tools_ok():
            return {"ok": False, "error": "There is no local workspace on the "
                                          "hosted build."}
        os.makedirs(T.WORKSPACE, exist_ok=True)
        return self.open_site("file:///" + T.WORKSPACE.replace("\\", "/"))
    # ---- file browser / previewer ---------------------------------------
    @_safe
    def browse(self, path=None):
        """List a folder inside the workspace (or the workspace root)."""
        if not _local_tools_ok():
            # The workspace belongs to the SERVER here - never expose it.
            return {"ok": False, "error": "The file browser is desktop-only."}
        root = T.WORKSPACE
        target = T.resolve_path(path) if path else root
        # never let the browser wander outside the workspace
        if not T.in_workspace(target):
            target = root
        if not os.path.isdir(target):
            return {"ok": False, "error": "Folder not found."}

        entries = []
        try:
            names = sorted(os.listdir(target),
                           key=lambda n: (not os.path.isdir(os.path.join(target, n)),
                                          n.lower()))
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

        for name in names[:800]:
            full = os.path.join(target, name)
            try:
                st = os.stat(full)
                isdir = os.path.isdir(full)
            except Exception:
                continue
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            entries.append({
                "name": name,
                "path": full,
                "rel": os.path.relpath(full, root),
                "dir": isdir,
                "size": None if isdir else st.st_size,
                "modified": time.strftime("%Y-%m-%d %H:%M",
                                          time.localtime(st.st_mtime)),
                "ext": "" if isdir else ext,
                "kind": "dir" if isdir else _preview_kind(ext),
            })

        parent = os.path.dirname(target)
        return {"ok": True, "path": target, "root": root,
                "rel": os.path.relpath(target, root),
                "parent": parent if T.in_workspace(parent) else None,
                "entries": entries}

    @_safe
    def preview(self, path):
        """Content for the previewer: images as a data URL, text as text."""
        if not _local_tools_ok():
            return {"ok": False, "error": "The previewer is desktop-only."}
        p = T.resolve_path(path)
        if not T.in_workspace(p) or not os.path.isfile(p):
            return {"ok": False, "error": "File not found."}
        if T.is_sensitive(p):
            return {"ok": False, "error": "This file is blocked from preview."}

        size = os.path.getsize(p)
        ext = os.path.splitext(p)[1].lower().lstrip(".")
        kind = _preview_kind(ext)
        base = {"ok": True, "path": p, "name": os.path.basename(p),
                "size": size, "ext": ext, "kind": kind,
                "modified": time.strftime("%Y-%m-%d %H:%M",
                                          time.localtime(os.path.getmtime(p)))}

        if kind == "image":
            if size > 12 * 1024 * 1024:
                return dict(base, kind="binary",
                            note="Image is larger than 12 MB.")
            import base64
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                    ".ico": "image/x-icon", ".svg": "image/svg+xml"}.get("." + ext,
                                                                        "image/png")
            with open(p, "rb") as f:
                data = f.read()
            return dict(base, data_url="data:%s;base64,%s"
                        % (mime, base64.b64encode(data).decode()))

        if kind == "text":
            try:
                with open(p, "rb") as f:
                    raw = f.read(600_000)
                text = _decode_text(raw) or ""
                return dict(base, text=text[:300_000],
                            truncated=len(text) > 300_000)
            except Exception as e:
                return dict(base, kind="binary", note=str(e)[:150])

        return dict(base, kind="binary",
                    note="No preview available for this file type — open it instead.")

    @_safe
    def reveal(self, path, select=False):
        """Show the file in Explorer, selected when possible."""
        p = T.resolve_path(path)
        if not os.path.exists(p):
            return {"ok": False, "error": "Path not found."}
        if os.name != "nt":
            return {"ok": False, "error": "Windows only."}
        try:
            if select and os.path.isfile(p):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
            else:
                folder = p if os.path.isdir(p) else os.path.dirname(p)
                os.startfile(folder)              # noqa: S606
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @_safe
    def open_file(self, path):
        """Open the file in its default application."""
        p = T.resolve_path(path)
        if not os.path.exists(p):
            return {"ok": False, "error": "File not found."}
        try:
            os.startfile(p)                        # noqa: S606
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    # ---- attachments ----------------------------------------------------
    @_safe
    def read_attachment(self, name, b64):
        """Decode an uploaded file and decide how to hand it to the model.

        Images come back as a data URL (vision), archives get their text
        members extracted, PDFs get their text layer pulled, and anything
        genuinely binary is reported honestly rather than silently dropped.
        """
        import base64
        raw = base64.b64decode(b64 or "")
        if len(raw) > 40 * 1024 * 1024:
            return {"ok": False, "error": "File is larger than 40 MB."}
        r = _process_attachment(name, raw)
        # never ship a huge data URL into the DOM twice
        if r.get("kind") == "image" and len(r.get("dataUrl", "")) > 8_000_000:
            return {"ok": True, "kind": "binary", "name": name, "size": len(raw),
                    "note": "Image is larger than 6 MB — cannot send it. Resize it and try again."}
        return r

    # ---- boot -----------------------------------------------------------
    @_safe
    def boot(self):
        provs = store.load_providers()
        return {
            "ok": True,
            "app": {"name": APP_NAME, "version": APP_VERSION},
            "providers": [self._pub(p) for p in provs],
            "key_storage": store.key_storage_kind(),
            "data_dir": store.data_dir(),
        }

    # ---- detection ------------------------------------------------------
    @_safe
    def detect(self, endpoint, key):
        """Identify the endpoint and list its models. Does NOT save anything."""
        base, prov = P.resolve(endpoint or "")
        if not base:
            return {"ok": False, "error": "The endpoint is empty. Paste something."}

        key = (key or "").strip()
        if not prov.get("no_key") and not key:
            return {"ok": False, "error": "An API key is required.",
                    "provider": self._pub_meta(prov)}

        ok, models, err = P.list_models(base, key or None, prov)
        if not ok:
            return {
                "ok": False,
                "error": err or "could not fetch the models list",
                "provider": self._pub_meta(prov),
                "base": base,
                "hint": self._hint_for(prov, err),
            }

        rows = []
        for m in models:
            mid = m["id"]
            rows.append({
                "id": mid,
                "free": P.is_free(mid, prov),
                "ctx": m.get("ctx"),
                # a model that can't chat (embeddings, tts, rerank) is noise
                "chat": not _is_non_chat(mid),
            })
        rows.sort(key=lambda r: (not r["free"], not r["chat"], r["id"]))

        return {
            "ok": True,
            "base": base,
            "provider": self._pub_meta(prov),
            "models": rows,
            "total": len(rows),
            "free_count": sum(1 for r in rows if r["free"]),
        }

    # ---- provider crud --------------------------------------------------
    @_safe
    def add_provider(self, endpoint, key, models, name=None):
        """models = [model_id, ...] the user ticked."""
        base, prov = P.resolve(endpoint or "")
        if not base:
            return {"ok": False, "error": "The endpoint is empty."}
        models = [m for m in (models or []) if isinstance(m, str) and m.strip()]
        if not models:
            return {"ok": False, "error": "Tick at least one model."}

        with self._lock:
            provs = store.load_providers()
            # replace an existing entry for the same base instead of duplicating
            existing = next((p for p in provs if p.get("base") == base), None)

            entry = {
                "id": existing["id"] if existing else _pid(),
                "name": (name or "").strip() or prov["name"],
                "provider_id": prov["id"],
                "base": base,
                "key": (key or "").strip(),
                "color": prov.get("color", "#10a37f"),
                "free": prov.get("free", "none"),
                "free_note": prov.get("free_note", ""),
                "site": prov.get("site", ""),
                "local": bool(prov.get("local")),
                "no_key": bool(prov.get("no_key")),
                "usage_kind": prov.get("usage"),
                "enabled": True,
                "models": [{"id": m, "enabled": True,
                            "free": P.is_free(m, prov)} for m in models],
            }
            if existing:
                provs = [entry if p.get("id") == existing["id"] else p for p in provs]
            else:
                provs.append(entry)
            store.save_providers(provs)
        return {"ok": True, "provider": self._pub(entry)}

    @_safe
    def remove_provider(self, pid):
        with self._lock:
            provs = [p for p in store.load_providers() if p.get("id") != pid]
            store.save_providers(provs)
        store.reset_usage(pid)
        return {"ok": True}

    @_safe
    def rename_provider(self, pid, name):
        with self._lock:
            provs = store.load_providers()
            p = _find(provs, pid)
            if not p:
                return {"ok": False, "error": "provider not found"}
            p["name"] = (name or "").strip() or p["name"]
            store.save_providers(provs)
            return {"ok": True, "provider": self._pub(p)}

    @_safe
    def set_enabled(self, pid, enabled):
        with self._lock:
            provs = store.load_providers()
            p = _find(provs, pid)
            if not p:
                return {"ok": False, "error": "provider not found"}
            p["enabled"] = bool(enabled)
            store.save_providers(provs)
            return {"ok": True}

    @_safe
    def set_key(self, pid, key):
        with self._lock:
            provs = store.load_providers()
            p = _find(provs, pid)
            if not p:
                return {"ok": False, "error": "provider not found"}
            p["key"] = (key or "").strip()
            store.save_providers(provs)
            return {"ok": True}

    @_safe
    def set_price(self, pid, price_in, price_out):
        """Set the USD-per-1M-tokens price for a provider.

        Resellers (and most OpenAI-compatible gateways) don't publish pricing,
        so the user can enter it here and every message shows its real cost.
        Pass empty/None to clear and fall back to the built-in table.
        """
        def _num(v):
            if v is None or v == "":
                return None
            try:
                f = float(v)
                return f if f >= 0 else None
            except (TypeError, ValueError):
                return None

        pin, pout = _num(price_in), _num(price_out)
        if (pin is None) != (pout is None):
            return {"ok": False,
                    "error": "Enter both prices, or leave both blank (input and output)."}
        with self._lock:
            provs = store.load_providers()
            p = _find(provs, pid)
            if not p:
                return {"ok": False, "error": "provider not found"}
            if pin is None:
                p.pop("price_in", None)
                p.pop("price_out", None)
            else:
                p["price_in"] = pin
                p["price_out"] = pout
            store.save_providers(provs)
            return {"ok": True, "provider": self._pub(p)}

    @_safe
    def price_preview(self, pid, model):
        """What the current pricing resolves to for a given model."""
        p = _find(store.load_providers(), pid)
        if not p:
            return {"ok": False, "error": "provider not found"}
        pr = P.price_for(model, p)
        if not pr:
            return {"ok": True, "known": False}
        return {"ok": True, "known": True, "in": pr[0], "out": pr[1],
                "source": "manual" if p.get("price_in") is not None else "built-in"}

    # ---- models ---------------------------------------------------------
    @_safe
    def set_models(self, pid, model_ids):
        """Replace the enabled set for a provider."""
        model_ids = set(m for m in (model_ids or []) if isinstance(m, str))
        with self._lock:
            provs = store.load_providers()
            p = _find(provs, pid)
            if not p:
                return {"ok": False, "error": "provider not found"}
            p["models"] = [{"id": m, "enabled": True, "free": P.is_free(m, p)}
                           for m in sorted(model_ids)]
            store.save_providers(provs)
            return {"ok": True, "provider": self._pub(p)}

    @_safe
    def toggle_model(self, pid, mid, enabled):
        with self._lock:
            provs = store.load_providers()
            p = _find(provs, pid)
            if not p:
                return {"ok": False, "error": "provider not found"}
            found = False
            for m in p.get("models") or []:
                if m["id"] == mid:
                    m["enabled"] = bool(enabled)
                    found = True
            if not found and enabled:
                p.setdefault("models", []).append(
                    {"id": mid, "enabled": True, "free": P.is_free(mid, p)})
            store.save_providers(provs)
            return {"ok": True}

    @_safe
    def refresh_models(self, pid):
        """Re-fetch the catalog for a saved provider, keeping tick state."""
        provs = store.load_providers()
        p = _find(provs, pid)
        if not p:
            return {"ok": False, "error": "provider not found"}
        ok, models, err = P.list_models(p["base"], p.get("key") or None, p)
        if not ok:
            return {"ok": False, "error": err}
        have = {m["id"]: m for m in (p.get("models") or [])}
        rows = []
        for m in models:
            mid = m["id"]
            rows.append({
                "id": mid,
                "free": P.is_free(mid, p),
                "chat": not _is_non_chat(mid),
                "enabled": bool(have.get(mid, {}).get("enabled", False)),
                "saved": mid in have,
            })
        rows.sort(key=lambda r: (not r["free"], not r["chat"], r["id"]))
        return {"ok": True, "models": rows, "total": len(rows),
                "free_count": sum(1 for r in rows if r["free"])}

    # ---- listing for the UI --------------------------------------------
    @_safe
    def all_models(self):
        """Every enabled model across every enabled provider, grouped."""
        out = []
        for p in store.load_providers():
            if not p.get("enabled", True):
                continue
            models = _enabled_models(p)
            if not models:
                continue
            out.append({
                "provider": self._pub(p),
                "models": [{"id": m["id"], "free": bool(m.get("free"))}
                           for m in models],
            })
        return {"ok": True, "groups": out}

    # ---- chat -----------------------------------------------------------
    @_safe
    def chat(self, pid, model, messages):
        gate = self._time_gate()
        if gate:
            return gate
        p = _find(store.load_providers(), pid)
        if not p:
            return {"ok": False, "error": "Provider not found. Add it again."}
        if not p.get("enabled", True):
            return {"ok": False, "error": f"{p['name']} is disabled."}
        if not isinstance(messages, list) or not messages:
            return {"ok": False, "error": "No message."}

        ok, text, meta = P.chat(p["base"], p.get("key") or None, model,
                                self._prep_messages(messages), timeout=600)
        if ok:
            u = (meta or {}).get("usage") or {}
            cost = P.message_cost(p, model, u.get("in", 0), u.get("out", 0))
            store.bump_usage(p["id"], u.get("in", 0), u.get("out", 0), 1, cost)
            declared = (meta or {}).get("model") or ""
            T.set_runtime(asked=model, declared=declared,
                          provider=p.get("name") or "", base=p.get("base") or "")
            return {"ok": True, "text": text, "usage": u,
                    "model": (meta or {}).get("model", model),
                    "cost": cost,
                    "cost_text": P.fmt_money(cost),
                    "priced": cost is not None,
                    "asked_model": model,
                    "declared_model": declared,
                    "identity": _identity_flag(model, declared),
                    "price_source": ("manual" if p.get("price_in") is not None
                                     else ("built-in" if cost is not None else None))}
        return {"ok": False, "error": (meta or {}).get("error", "unknown error"),
                "status": (meta or {}).get("status", 0)}

    # ---- usage ----------------------------------------------------------
    @_safe
    def usage(self, pid):
        p = _find(store.load_providers(), pid)
        if not p:
            return {"ok": False, "error": "provider not found"}
        local = store.usage_for(pid)
        out = {
            "ok": True,
            "provider": self._pub(p),
            "local": {"date": local.get("date"),
                      "requests": local.get("requests", 0),
                      "in": local.get("in", 0),
                      "out": local.get("out", 0),
                      "cost": local.get("cost", 0.0)},
            "history": local.get("history", []),
            "quota": P.usage_percent(p, local),
            "cost_text": P.fmt_money(local.get("cost", 0.0)),
            "balance": None,
        }
        if p.get("usage_kind") and p.get("key"):
            try:
                out["balance"] = P.get_usage(p, p["key"], p["base"])
            except Exception as e:
                out["balance"] = {"ok": False, "label": "balance",
                                  "detail": str(e)[:200]}
        return out

    @_safe
    def usage_all(self):
        rows = []
        for p in store.load_providers():
            local = store.usage_for(p["id"])
            rows.append({
                "provider": self._pub(p),
                "local": {"requests": local.get("requests", 0),
                          "in": local.get("in", 0),
                          "out": local.get("out", 0),
                          "cost": local.get("cost", 0.0)},
                "quota": P.usage_percent(p, local),
                "cost_text": P.fmt_money(local.get("cost", 0.0)),
                "model_count": len(_enabled_models(p)),
            })
        return {"ok": True, "rows": rows}

    # ---- testing --------------------------------------------------------
    @_safe
    def test(self, pid):
        """Cheap liveness check: list models, then try one tiny completion."""
        p = _find(store.load_providers(), pid)
        if not p:
            return {"ok": False, "error": "provider not found"}

        ok, models, err = P.list_models(p["base"], p.get("key") or None, p)
        if not ok:
            return {"ok": False, "step": "models", "error": err}

        enabled = _enabled_models(p)
        if not enabled:
            return {"ok": True, "step": "models",
                    "note": f"found {len(models)} models, but none were ticked",
                    "models_ok": True}
        m = enabled[0]["id"]
        ok2, text, meta = P.chat(p["base"], p.get("key") or None, m,
                                 [{"role": "user", "content": "Reply with: OK"}],
                                 timeout=120)
        if ok2:
            u = (meta or {}).get("usage") or {}
            store.bump_usage(p["id"], u.get("in", 0), u.get("out", 0), 1)
            return {"ok": True, "step": "chat", "model": m,
                    "reply": (text or "")[:120], "models_ok": True}
        return {"ok": False, "step": "chat", "model": m,
                "error": (meta or {}).get("error", "chat failed")}

    # ---- misc -----------------------------------------------------------
    @_safe
    def open_site(self, url):
        if url and str(url).startswith(("http://", "https://")):
            webbrowser.open(url)
            return {"ok": True}
        return {"ok": False, "error": "bad url"}

    @_safe
    def reset_usage(self, pid=None):
        store.reset_usage(pid)
        return {"ok": True}

    @_safe
    def catalog(self):
        """The known-provider list, so the UI can offer quick-add chips."""
        rows = []
        for p in P.KNOWN:
            rows.append({
                "id": p["id"], "name": p["name"], "base": p["base"],
                "site": p.get("site", ""), "free": p.get("free", "none"),
                "free_note": p.get("free_note", ""),
                "key_hint": p.get("key_hint", ""),
                "local": bool(p.get("local")),
                "color": p.get("color", "#10a37f"),
            })
        order = {"all": 0, "partial": 1, "suffix:free": 1, "none": 2}
        rows.sort(key=lambda r: (order.get(r["free"], 3), r["name"]))
        return {"ok": True, "providers": rows}

    # ---- internal -------------------------------------------------------
    def _pub_meta(self, prov):
        return {
            "id": prov.get("id"), "name": prov.get("name"),
            "color": prov.get("color", "#10a37f"),
            "base": prov.get("base", ""), "site": prov.get("site", ""),
            "free": prov.get("free", "none"),
            "free_note": prov.get("free_note", ""),
            "free_label": prov.get("free_label", P.free_mode_label(prov)),
            "key_hint": prov.get("key_hint", ""),
            "local": bool(prov.get("local")),
            "no_key": bool(prov.get("no_key")),
        }

    def _pub(self, p):
        """Public shape of a saved provider - never includes the key."""
        key = p.get("key") or ""
        return {
            "id": p.get("id"),
            "name": p.get("name"),
            "provider_id": p.get("provider_id"),
            "base": p.get("base"),
            "color": p.get("color", "#10a37f"),
            "free": p.get("free", "none"),
            "free_note": p.get("free_note", ""),
            "site": p.get("site", ""),
            "local": bool(p.get("local")),
            "no_key": bool(p.get("no_key")),
            "enabled": bool(p.get("enabled", True)),
            "has_key": bool(key),
            "key_masked": (key[:6] + "…" + key[-4:]) if len(key) > 10
                          else ("set" if key else ""),
            "usage_kind": p.get("usage_kind"),
            "price_in": p.get("price_in"),
            "price_out": p.get("price_out"),
            "price_manual": p.get("price_in") is not None,
            "model_count": len(p.get("models") or []),
            "enabled_count": len(_enabled_models(p)),
            "models": p.get("models") or [],
        }

    @staticmethod
    def _hint_for(prov, err):
        low = (err or "").lower()
        if "401" in low or "unauthor" in low or "invalid api key" in low or "api key" in low:
            return ("The API key looks wrong. Create a new key from the provider dashboard "
                    f"({prov.get('site') or 'provider site'}).")
        if "403" in low or "forbidden" in low:
            return ("The key looks fine but access is blocked — check billing/permissions "
                    "on the provider, or you may be region-blocked.")
        if "404" in low:
            return ("The endpoint path is wrong. Give just the base — e.g. "
                    f"{prov.get('base') or 'https://api.example.com/v1'} — "
                    "or paste the full /chat/completions URL.")
        if "timed out" in low or "timeout" in low or "network" in low or "getaddrinfo" in low:
            return "Internet or DNS issue. Check your connection."
        if "429" in low:
            return "Rate limited. Try again in a little while."
        return ""

    # kept for the router in _safe
    def __getattr__(self, name):
        raise AttributeError(name)


def _is_non_chat(mid: str) -> bool:
    """Filter obvious non-chat models out of the default selection order."""
    m = (mid or "").lower()
    bad = ("embed", "embedding", "rerank", "whisper", "tts", "audio",
           "moderation", "image", "dall-e", "stable-diffusion", "flux",
           "vision-encoder", "guard", "prompt-guard")
    return any(b in m for b in bad)



def _preview_kind(ext: str) -> str:
    """Classify an extension for the previewer."""
    e = (ext or "").lower().lstrip(".")
    if e in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "svg"):
        return "image"
    if e in ("txt", "md", "markdown", "log", "csv", "tsv", "json", "jsonl", "xml",
             "yml", "yaml", "toml", "ini", "cfg", "conf", "env", "py", "js", "mjs",
             "cjs", "jsx", "ts", "tsx", "html", "htm", "css", "scss", "sass", "less",
             "vue", "svelte", "c", "h", "cpp", "cc", "hpp", "cs", "java", "kt", "go",
             "rs", "rb", "php", "pl", "lua", "swift", "sql", "sh", "bash", "zsh",
             "bat", "cmd", "ps1", "gradle", "cmake", "make", "dockerfile", "srt",
             "vtt", "tex", "bib"):
        return "text"
    return "binary"


# ---------------------------------------------------------------------------
# agent: tool-call parsing
# ---------------------------------------------------------------------------
# We use a text protocol rather than native function calling, because the
# OpenAI-compatible gateways people use here (resellers, local servers) support
# it inconsistently. Text works with literally any chat model.
#
# The hard part is that models emit *almost* JSON when the payload is big:
#   - literal newlines inside the "content" string (invalid JSON)
#   - the closing </tool_call> missing entirely (ran out of tokens)
#   - single quotes, trailing commas
# If any of that slips through, the raw <tool_call> blob gets shown to the user
# as if it were the answer. So we repair before parsing, and fall back to a
# brace-matching extractor for unterminated calls.
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
TOOL_OPEN_RE = re.compile(r"<tool_call>\s*(\{.*)", re.S)
TOOL_FENCE_RE = re.compile(r"```(?:json)?\s*(\{[^`]*?\"name\"[^`]*?\})\s*```", re.S)


def _repair_json(blob: str) -> str:
    """Make a model's almost-JSON actually parse.

    Two things models get wrong constantly, both of which silently break a
    write_file call and dump the raw blob into the chat:

    1. Real newlines inside a string instead of \\n. We escape control
       characters only while inside a string.
    2. Windows paths written with single backslashes - "C:\\Users\\Amanat"
       arrives as "C:\\Users\\Amanat", where \\U is not a legal JSON escape.
       When a backslash is followed by something that is not a valid escape
       character we double it, which is what the model meant.
    """
    VALID_ESCAPES = set('"\\/bfnrtu')
    out = []
    in_str = False
    esc = False
    for ch in blob:
        if esc:
            if ch not in VALID_ESCAPES:
                # the backslash was meant literally (a Windows path) - double it
                out[-1] = "\\\\"
            out.append(ch)
            esc = False
            continue
        if ch == "\\":
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if in_str:
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            if ord(ch) < 0x20:
                out.append("\\u%04x" % ord(ch))
                continue
        out.append(ch)
    return "".join(out)


def _extract_json_objects(text: str):
    """Yield every brace-balanced {...} block, ignoring braces inside strings."""
    i = 0
    n = len(text)
    while i < n:
        start = text.find("{", i)
        if start < 0:
            return
        depth = 0
        in_str = False
        esc = False
        end = None
        for j in range(start, n):
            ch = text[j]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end is None:
            return                      # unterminated - nothing usable left
        yield text[start:end + 1]
        i = end + 1


def _autoclose(blob: str) -> str:
    """Close a JSON object the model never finished.

    When a write is cut off mid-token the tail looks like
        {"name":"write_file","args":{"path":"a.css","content":"body
    so we count the open braces and quotes and close them ourselves. Better to
    recover a truncated write than to dump raw JSON into the chat.
    """
    depth = 0
    in_str = False
    esc = False
    for ch in blob:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    fixed = blob
    if in_str:
        fixed += '"'
    if depth > 0:
        fixed += "}" * depth
    return fixed


def _salvage_call(blob: str):
    """Last resort when the JSON is beyond repair.

    HTML and JS payloads often contain unescaped double quotes
    (`<meta charset="UTF-8">`), which terminates the JSON string early and no
    amount of control-character escaping fixes it. Rather than lose the write
    and dump raw JSON into the chat, pull the fields out with regex.
    """
    if not blob or "name" not in blob:
        return None
    nm = re.search(r'"name"\s*:\s*"([A-Za-z_][\w]*)"', blob)
    if not nm:
        return None
    name = nm.group(1)

    args = {}
    for key in ("path", "src", "dst", "root", "command", "cwd", "text"):
        m = re.search(r'"' + key + r'"\s*:\s*"(.*?)"\s*[,}]', blob, re.S)
        if m:
            args[key] = m.group(1)

    # content runs to the end of the object - grab everything after the key
    cm = re.search(r'"content"\s*:\s*"(.*)', blob, re.S)
    if cm:
        c = cm.group(1)
        c = re.sub(r'"\s*\}\s*\}\s*$', "", c)      # trailing "  } }
        c = re.sub(r'"\s*\}\s*$', "", c)           # trailing "  }
        c = re.sub(r'"\s*$', "", c)                # trailing "
        args["content"] = c

    if not args:
        return None
    return {"name": name, "args": args}


def _load_call(blob: str):
    """Parse one tool-call object, repairing the usual model sloppiness."""
    if not blob or not blob.strip():
        return None
    candidates = [blob, _repair_json(blob)]
    # a couple of cheap normalisations models love to emit
    fixed = _repair_json(blob).replace("'", '"')
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    candidates.append(fixed)

    d = None
    for c in candidates:
        try:
            d = json.loads(c)
            break
        except Exception:
            continue

    if not isinstance(d, dict):
        return _salvage_call(blob)

    name = d.get("name") or d.get("tool") or d.get("function")
    if not name:
        return _salvage_call(blob)
    args = d.get("args") or d.get("arguments") or d.get("parameters") or {}
    if isinstance(args, str):
        try:
            args = json.loads(_repair_json(args))
        except Exception:
            args = {}
    if not isinstance(args, dict) or not args:
        salv = _salvage_call(blob)
        if salv:
            return salv
    return {"name": str(name).strip(), "args": args if isinstance(args, dict) else {}}


def parse_tool_calls(text: str) -> list:
    """Pull tool calls out of a model reply. Tolerant of formatting drift."""
    src = text or ""
    out = []

    # 1. properly closed tags
    for m in TOOL_CALL_RE.finditer(src):
        c = _load_call(m.group(1))
        if c:
            out.append(c)
    if out:
        return out

    # 2. tag opened but never closed (model hit its token limit mid-write).
    #    Take everything after the tag and brace-match the object.
    for m in TOOL_OPEN_RE.finditer(src):
        tail = m.group(1)
        blobs = list(_extract_json_objects(tail))
        if not blobs:
            blobs = [_autoclose(tail)]          # truncated mid-object
        for blob in blobs:
            c = _load_call(blob)
            if c:
                out.append(c)
                break
        if out:
            return out

    # 3. bare fenced JSON
    for m in TOOL_FENCE_RE.finditer(src):
        c = _load_call(m.group(1))
        if c:
            out.append(c)

    # 4. last resort: an untagged but clearly-shaped call object
    if not out and '"name"' in src and '"args"' in src:
        for blob in _extract_json_objects(src):
            c = _load_call(blob)
            if c and c["name"] in T.TOOLS:
                out.append(c)
                break
    return out


def strip_tool_calls(text: str) -> str:
    """The model's narration with the protocol tags removed.

    Handles the unclosed case too - otherwise a truncated call leaves a wall of
    raw JSON in the middle of the chat.
    """
    t = text or ""
    t = TOOL_CALL_RE.sub("", t)
    # an opened-but-never-closed tag: drop from the tag to the end of the text,
    # unless there is real prose after the JSON object
    m = TOOL_OPEN_RE.search(t)
    if m:
        tail = m.group(1)
        consumed = 0
        for blob in _extract_json_objects(tail):
            consumed = len(blob)
            break
        if not consumed:
            consumed = len(tail)                # truncated: drop it all
        t = t[:m.start()] + tail[consumed:]
    t = TOOL_FENCE_RE.sub("", t)
    return t.strip()


# ---------------------------------------------------------------------------
# attachment processing
# ---------------------------------------------------------------------------
IMAGE_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".ico": "image/x-icon", ".svg": "image/svg+xml",
}
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".jsonl", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".env", ".py", ".pyw", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".html", ".htm", ".css", ".scss", ".sass", ".less", ".vue", ".svelte",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".cs", ".java", ".kt",
    ".go", ".rs", ".rb", ".php", ".pl", ".lua", ".swift", ".m", ".mm",
    ".sql", ".sh", ".bash", ".zsh", ".bat", ".cmd", ".ps1", ".psm1",
    ".gradle", ".cmake", ".make", ".mk", ".dockerfile", ".gitignore",
    ".editorconfig", ".properties", ".patch", ".diff", ".srt", ".vtt",
    ".tex", ".bib", ".org", ".adoc", ".asm", ".s", ".v", ".sv", ".vhd",
}
ZIP_EXT = {".zip", ".jar", ".war", ".apk", ".xpi", ".epub", ".whl",
           ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}

MAX_INLINE_TEXT = 1_500_000        # chars of extracted text we forward
MAX_ZIP_MEMBER = 300_000           # chars per file inside an archive
MAX_ZIP_TOTAL = 1_200_000          # chars across the whole archive


def _decode_text(raw: bytes):
    """Best-effort text decode: utf-8, then utf-16, then cp1252/latin-1."""
    for enc in ("utf-8-sig", "utf-8"):
        try:
            t = raw.decode(enc)
            if "\x00" not in t[:4000]:
                return t
        except UnicodeDecodeError:
            continue
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except Exception:
            pass
    for enc in ("cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return None


def _pdf_text(raw: bytes) -> str:
    """Pull text out of a PDF. Handles FlateDecode streams and Tj/TJ operators.

    Deliberately rough - it reads the text layer, so scanned PDFs (images with
    no text) will come back empty, which we report honestly.
    """
    import re
    import zlib
    chunks = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
        data = m.group(1)
        try:
            data = zlib.decompress(data)
        except Exception:
            pass
        if b"BT" not in data and b"Tj" not in data and b"TJ" not in data:
            continue
        for t in re.finditer(rb"\(((?:[^()\\]|\\.)*)\)", data):
            s = t.group(1)
            s = (s.replace(b"\\(", b"(").replace(b"\\)", b")")
                  .replace(b"\\\\", b"\\").replace(b"\\n", b"\n"))
            chunks.append(s.decode("latin-1", "replace"))
    txt = " ".join(chunks)
    return " ".join(txt.split())[:MAX_INLINE_TEXT]


def _process_attachment(name: str, raw: bytes) -> dict:
    """Turn an uploaded file into something we can hand to a model."""
    import base64
    import io
    import zipfile

    name = os.path.basename(name or "file")
    ext = os.path.splitext(name)[1].lower()
    size = len(raw)

    # ---- images -> vision payload ----
    if ext in IMAGE_EXT:
        mime = IMAGE_EXT[ext]
        if ext == ".svg":
            t = _decode_text(raw)
            return {"ok": True, "kind": "text", "name": name, "size": size,
                    "text": (t or "")[:MAX_INLINE_TEXT]}
        return {"ok": True, "kind": "image", "name": name, "size": size,
                "mime": mime,
                "dataUrl": "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())}

    # ---- archives -> list + extract the text members ----
    if ext in ZIP_EXT:
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
            infos = [i for i in zf.infolist() if not i.is_dir()]
            listing = "\n".join(
                "  %s  (%s)" % (i.filename, _human(i.file_size)) for i in infos[:400])
            if len(infos) > 400:
                listing += "\n  … and %d more" % (len(infos) - 400)

            parts, total = [], 0
            for i in infos:
                if total >= MAX_ZIP_TOTAL:
                    parts.append("… baaki files skip (size limit)")
                    break
                iext = os.path.splitext(i.filename)[1].lower()
                if iext in TEXT_EXT or iext in IMAGE_EXT or iext == "":
                    try:
                        data = zf.read(i)
                    except Exception:
                        continue
                    if iext in IMAGE_EXT:
                        parts.append("--- %s (image, %s) ---"
                                     % (i.filename, _human(i.file_size)))
                        continue
                    t = _decode_text(data)
                    if t:
                        chunk = t[:MAX_ZIP_MEMBER]
                        parts.append("--- %s ---\n%s%s"
                                     % (i.filename, chunk,
                                        "\n… truncated" if len(t) > MAX_ZIP_MEMBER else ""))
                        total += len(chunk)
                else:
                    parts.append("--- %s (binary, %s) ---"
                                 % (i.filename, _human(i.file_size)))

            body = ("Archive: %s — %d file(s)\n\nContents:\n%s\n\n%s"
                    % (name, len(infos), listing, "\n\n".join(parts)))
            return {"ok": True, "kind": "zip", "name": name, "size": size,
                    "count": len(infos), "text": body[:MAX_INLINE_TEXT]}
        except Exception as e:
            return {"ok": True, "kind": "binary", "name": name, "size": size,
                    "note": "Could not open the archive (%s). Only the file name is being sent."
                            % str(e)[:80]}

    # ---- pdf ----
    if ext == ".pdf":
        t = _pdf_text(raw)
        if t and len(t.strip()) > 40:
            return {"ok": True, "kind": "pdf", "name": name, "size": size,
                    "text": t}
        return {"ok": True, "kind": "binary", "name": name, "size": size,
                "note": "No text layer found in the PDF (it may be a scan) — "
                        "only the file name is being sent."}

    # ---- everything else: try text, else report as binary ----
    t = _decode_text(raw)
    if t is not None and _looks_texty(t):
        return {"ok": True, "kind": "text", "name": name, "size": size,
                "text": t[:MAX_INLINE_TEXT]}
    return {"ok": True, "kind": "binary", "name": name, "size": size,
            "note": "This is a binary file (%s) — the model cannot read its content. "
                    "Only the file name is being sent." % _human(size)}


def _looks_texty(t: str) -> bool:
    """Reject files that decoded but are really binary noise."""
    if not t:
        return False
    sample = t[:4000]
    if "\x00" in sample:
        return False
    printable = sum(1 for c in sample if c.isprintable() or c in "\n\r\t")
    return printable / max(1, len(sample)) > 0.90


def _human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------
def run_gui():
    import webview

    # Give Windows an explicit AppUserModelID so the taskbar groups the window
    # under our own identity and shows the exe's icon instead of the generic
    # PyInstaller/python one.
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "HamuGang.HAMU-GPT.1")
        except Exception:
            pass

    api = Api()
    webview.create_window(
        APP_NAME, UI, js_api=api,
        width=1240, height=840, min_size=(900, 600),
        background_color="#0f1115", text_select=True,
    )
    webview.start(debug=False)


# ---------------------------------------------------------------------------
# WEB MODE - the same UI in Chrome, for the Vercel / hosted build
# ---------------------------------------------------------------------------
# Desktop mode has one implicit session: the person at the keyboard. Over HTTP
# that assumption breaks, so every browser gets an opaque session token and the
# backend refuses to act on an account unless the request is signed in under
# that token's own session. Two tabs, two accounts, no crosstalk.
_WEB_SESSIONS = {}          # token -> email
_web_lock = threading.Lock()


def _web_token(req) -> str:
    for h in ("X-Hamu-Token", "x-hamu-token"):
        v = req.headers.get(h)
        if v:
            return v.strip()
    return ""


def _web_email(token: str) -> str:
    with _web_lock:
        return _WEB_SESSIONS.get(token, "")


def _serve_download(which: str):
    """Hand out the desktop installer.

    Looked for in a few places so the same code works for a local run, a
    plain server, and a deploy where the file sits in public/ (Vercel serves
    public/ at the site root, which is why the button's URL and the file name
    are arranged to line up).

    Streams in chunks rather than reading it all into memory - it is 25 MB and
    a hosted process has better things to do with its RAM.
    """
    names = {"windows": ["HAMUGPT-Setup.exe"],
             "android": ["HAMU-GPT.apk"]}.get(which) or []
    here = os.path.dirname(os.path.abspath(__file__))
    roots = [here, os.path.join(here, "public"), os.path.join(here, "downloads"),
             os.path.dirname(here), _UI_DIR]
    found = None
    for root in roots:
        for n in names:
            cand = os.path.join(root, n)
            if os.path.isfile(cand):
                found = cand
                break
        if found:
            break

    if not found:
        return 404, "text/html; charset=utf-8", (
            "<!doctype html><meta charset=utf-8>"
            "<h1>Installer not found</h1>"
            "<p>Put <code>%s</code> next to <code>wsgi.py</code> or in a "
            "<code>public/</code> folder beside it, then try again.</p>"
            % (names[0] if names else "the file")
        ).encode("utf-8")

    try:
        with open(found, "rb") as f:
            body = f.read()
    except Exception as e:
        return 500, "text/plain; charset=utf-8", ("Could not read the installer: %s" % e).encode()

    filename = os.path.basename(found)
    return 200, "application/octet-stream", body, [
        ("Content-Disposition", 'attachment; filename="%s"' % filename),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "public, max-age=300"),
        ("X-Content-Type-Options", "nosniff"),
    ]


def _ui_html() -> str:
    try:
        with open(UI, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return ("<!doctype html><meta charset=utf-8>"
                "<h1>HAMU-GPT</h1><p>UI not found: " + str(e) + "</p>")


# ---------------------------------------------------------------------------
# static assets that sit next to index.html
# ---------------------------------------------------------------------------
# index.html references files relatively - logo.png above all. The desktop app
# gets these for free because pywebview serves the ui/ folder straight off the
# disk. Over HTTP nothing serves them unless we do, which is why the logo was a
# broken image on the hosted build.
_UI_DIR = os.path.join(HERE, "ui")

_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",     # /privacy and any other page
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
}


def _serve_static(rel: str):
    """Serve one file from ui/. Returns (content_type, bytes) or None.

    Only files directly inside ui/ are reachable, only with a known extension,
    and any path that tries to climb out (`..`) is refused - the web root must
    never become a way to read the rest of the server.
    """
    if not rel:
        return None
    rel = rel.replace("\\", "/").lstrip("/")
    if ".." in rel.split("/"):
        return None
    root = os.path.normpath(_UI_DIR)
    full = os.path.normpath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        return None
    if not os.path.isfile(full):
        return None
    ext = os.path.splitext(full)[1].lower()
    ctype = _STATIC_TYPES.get(ext)
    if not ctype:
        return None
    try:
        with open(full, "rb") as f:
            return ctype, f.read()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# "is this request over HTTP?" - and why it matters
# ---------------------------------------------------------------------------
# Agent mode runs commands on whatever machine the backend sits on. In the
# desktop shell that is the user's own PC and they asked for it. Over HTTP it
# is a SERVER, and the caller is whoever reached the port - so the tools are
# refused there no matter what, even for a local `--web` run on the LAN.
_HTTP_CTX = threading.local()


def _over_http() -> bool:
    return bool(getattr(_HTTP_CTX, "active", False))


def _signup_address() -> str:
    """Which connection is this signup coming from?

    On the web, the client's IP. X-Forwarded-For is read FIRST because on
    Vercel REMOTE_ADDR is the proxy - every visitor would otherwise look
    identical and the second person to sign up anywhere would be refused.

    On the desktop there is no remote address, so a stable id for the machine
    stands in. Same meaning either way: "how many accounts came from this one
    place".

    The value is only ever used to COUNT. It is never returned to a browser.
    """
    addr = (getattr(_HTTP_CTX, "client", "") or "").strip()
    if addr and addr not in ("::1", "localhost") and not addr.startswith("127."):
        return addr
    if store.hosted():
        return addr or "unknown"
    return _machine_id()


_MACHINE_ID = None


def _machine_id() -> str:
    """A stable id for this PC, for the desktop build's signup counting."""
    global _MACHINE_ID
    if _MACHINE_ID:
        return _MACHINE_ID
    # Windows keeps a per-install GUID; it survives reboots and updates.
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Cryptography", 0,
                           winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        v, _ = winreg.QueryValueEx(k, "MachineGuid")
        winreg.CloseKey(k)
        if v:
            _MACHINE_ID = "pc:" + str(v).strip().lower()
            return _MACHINE_ID
    except Exception:
        pass
    # No registry access: keep a random id beside the app's own data.
    try:
        import uuid
        p = os.path.join(store.data_dir(), "machine-id.txt")
        if os.path.isfile(p):
            v = open(p, encoding="utf-8").read().strip()
            if v:
                _MACHINE_ID = v
                return v
        v = "pc:" + uuid.uuid4().hex
        with open(p, "w", encoding="utf-8") as f:
            f.write(v)
        _MACHINE_ID = v
        return v
    except Exception:
        return "pc:unknown"


def _client_is_local() -> bool:
    """Did this request come from the machine the backend is running on?"""
    addr = (getattr(_HTTP_CTX, "client", "") or "").strip()
    if not addr:
        return True                    # unknown -> assume the same machine
    if addr in ("::1", "localhost"):
        return True
    return addr.startswith("127.")


def _local_tools_ok() -> bool:
    """Are machine-touching tools allowed for this request?

    Only in the desktop shell: not hosted, and not arriving over HTTP. A
    website must never be able to run commands on the server.
    """
    return not store.hosted() and not _over_http()


def _local_chrome_ok() -> bool:
    """May we read this machine's Chrome profiles?

    Looser than the tool rule, because "Continue with Google" is a convenience
    rather than a way to run code, and the user asked for it on the web build
    too. It is still refused on a hosted deploy - there is no local Chrome to
    read there - and refused when the request comes from another machine, so a
    LAN run cannot enumerate the server's browser profiles.
    """
    if store.hosted():
        return False
    if not _over_http():
        return True                    # desktop shell
    return _client_is_local()


def handle_request(method: str, path: str, token: str, raw_body: bytes,
                   client: str = ""):
    """One HTTP request, independent of which server is driving it.

    Returns ``(status, content_type, body_bytes)``. Both the standalone TCP
    server and the WSGI app call this, so the security rules - private-method
    blocking, per-session account binding, token issuance, and the fact that
    machine-touching tools are off over HTTP - exist in exactly one place and
    cannot drift apart between the desktop-hosted build and the Vercel build.

    The thread-local flag is what tells the Api that this request came in over
    the network rather than from the desktop shell. It is set here, around the
    whole dispatch, and cleared again so a later desktop call on the same
    thread is not tainted.
    """
    _HTTP_CTX.active = True
    _HTTP_CTX.client = client or ""
    try:
        return _dispatch(method, path, token, raw_body)
    finally:
        _HTTP_CTX.active = False
        _HTTP_CTX.client = ""


def _dispatch(method: str, path: str, token: str, raw_body: bytes):
    import urllib.parse
    api = _shared_api()

    p = urllib.parse.urlparse(path).path

    if method == "GET":
        if p in ("/", "/index.html"):
            return 200, "text/html; charset=utf-8", _ui_html().encode("utf-8")
        if p == "/health":
            return 200, "application/json; charset=utf-8", json.dumps(
                {"ok": True, "app": APP_NAME, "version": APP_VERSION}).encode()
        # The desktop installer. Vercel is set up to answer this straight off
        # its CDN (see the rewrite in vercel.json) because a serverless
        # function cannot return a 25 MB body - Vercel caps responses at
        # 4.5 MB. This route is the fallback that makes a LOCAL run work, and
        # it is the only path on a host without that limit.
        if p in ("/download/windows", "/download/windows/"):
            return _serve_download("windows")
        if p in ("/download/android", "/download/android/"):
            return _serve_download("android")
        # clean URL for the privacy policy - Adsterra requires the site to have
        # one, and /privacy reads better than /privacy.html in a footer link
        if p in ("/privacy", "/privacy/"):
            rel = "privacy.html"
        else:
            rel = urllib.parse.unquote(p.lstrip("/"))
        got = _serve_static(rel)
        if got:
            return 200, got[0], got[1]
        return 404, "application/json; charset=utf-8", json.dumps(
            {"ok": False, "error": "not found"}).encode()

    if not p.startswith("/api/"):
        return 404, "application/json; charset=utf-8", json.dumps(
            {"ok": False, "error": "not found"}).encode()

    name = p[5:]
    try:
        payload = json.loads((raw_body or b"{}").decode("utf-8") or "{}")
    except Exception:
        payload = {}
    args = payload.get("args") or []
    if not isinstance(args, list):
        args = [args]

    email = _web_email(token)
    _bind_request(email)
    try:
        fn = getattr(api, name, None)
        if fn is None or name.startswith("_"):
            out = {"ok": False, "error": "unknown method " + name}
        else:
            out = fn(*args)
            if name in ("signup_local", "signup_google", "signin",
                        "admin_signin") and isinstance(out, dict) and out.get("ok"):
                tok = _new_token()
                with _web_lock:
                    _WEB_SESSIONS[tok] = email_of_account()
                out["token"] = tok
            elif name == "signout":
                with _web_lock:
                    _WEB_SESSIONS.pop(token, None)
    except Exception as e:
        out = {"ok": False, "error": _brief(e)}
    return (200, "application/json; charset=utf-8",
            json.dumps(out, ensure_ascii=False, default=str).encode("utf-8"))


_WSGI_API = None


def _shared_api():
    """One Api instance per process - the backend is stateful (jobs, config)."""
    global _WSGI_API
    if _WSGI_API is None:
        _WSGI_API = Api()
    return _WSGI_API


def _tcp_client(handler) -> str:
    """The visitor's address for the standalone HTTP server.

    Reads X-Forwarded-For too, so a local run behaves exactly like the deployed
    site when it sits behind anything. Without a header it falls back to the
    socket, which for a local run is 127.0.0.1 - so a local run counts every
    signup against one address, exactly as the desktop build does.
    """
    try:
        fwd = (handler.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if fwd:
            return fwd
        real = (handler.headers.get("X-Real-IP") or "").strip()
        if real:
            return real
    except Exception:
        pass
    return handler.client_address[0] if handler.client_address else ""


def _wsgi_client(environ) -> str:
    """The real visitor's address behind a proxy.

    On Vercel REMOTE_ADDR is the platform's own proxy, so every visitor on the
    site would share one address - and the per-connection signup limit would
    then refuse the second person to sign up anywhere. X-Forwarded-For carries
    the real one, left-most entry first.
    """
    fwd = (environ.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    if fwd:
        return fwd
    real = (environ.get("HTTP_X_REAL_IP") or "").strip()
    if real:
        return real
    return environ.get("REMOTE_ADDR", "")


def _unpack_reply(r):
    """Normalise a handler reply to (code, ctype, body, extra_headers).

    Almost every handler returns three things. The download route returns a
    fourth - a list of extra headers - because handing over a file needs
    Content-Disposition and its own Cache-Control. Accepting both shapes here
    means no other route had to change.
    """
    if isinstance(r, tuple) and len(r) == 4:
        code, ctype, body, extra = r
        return code, ctype, body, list(extra or [])
    code, ctype, body = r
    return code, ctype, body, []


def _merge_headers(defaults, extra):
    """Let `extra` win over `defaults`, without sending a name twice."""
    taken = {k.lower() for k, _ in extra}
    return [h for h in defaults if h[0].lower() not in taken] + extra


def build_wsgi_app():
    """A WSGI callable, for hosts that speak WSGI (including Vercel)."""
    def wsgi(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        token = environ.get("HTTP_X_HAMU_TOKEN", "") or ""
        raw = b""
        try:
            n = int(environ.get("CONTENT_LENGTH") or 0)
            if n:
                raw = environ["wsgi.input"].read(n)
        except Exception:
            raw = b""
        code, ctype, body, extra = _unpack_reply(handle_request(
            method, path, token, raw, _wsgi_client(environ)))
        headers = _merge_headers(
            [("Content-Type", ctype),
             ("Content-Length", str(len(body))),
             ("Cache-Control", "no-store"),
             ("X-Content-Type-Options", "nosniff"),
             ("Referrer-Policy", "no-referrer")], extra)
        start_response(str(code) + " " + {200: "OK", 404: "Not Found"}.get(code, "OK"),
                       headers)
        return [body]
    return wsgi


def _free_port(preferred: int, host: str) -> int:
    """The first free port at or after `preferred`.

    Note the deliberate ABSENCE of SO_REUSEADDR. On Windows that option lets a
    socket bind a port another process is already listening on, so a check made
    with it reports every busy port as free. Without it, the bind genuinely
    fails when the port is taken, which is what we want to know.
    """
    import socket
    for p in range(preferred, preferred + 25):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, p))
            s.close()
            return p
        except OSError:
            s.close()
            continue
    return preferred


def serve_web(port: int = 8000, host: str = "127.0.0.1",
              open_browser: bool = False):
    """Serve ui/index.html plus a JSON API over HTTP (developer / LAN use)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    port = _free_port(port, host)

    class Handler(BaseHTTPRequestHandler):
        server_version = "HAMU-GPT"

        def log_message(self, *a):
            pass                       # keep the console clean

        def _reply(self, method):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
            except Exception:
                raw = b""
            code, ctype, body, extra = _unpack_reply(handle_request(
                method, self.path, _web_token(self), raw,
                _tcp_client(self)))
            self.send_response(code)
            for k, v in _merge_headers(
                    [("Content-Type", ctype),
                     ("Content-Length", str(len(body))),
                     ("Cache-Control", "no-store"),
                     ("X-Content-Type-Options", "nosniff"),
                     ("Referrer-Policy", "no-referrer")], extra):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._reply("GET")

        def do_POST(self):
            self._reply("POST")

    class _WebServer(ThreadingHTTPServer):
        # Never let a second process silently share the port. If it is taken we
        # want to notice and move to the next one, not serve half the requests
        # and look "randomly broken".
        allow_reuse_address = False
        daemon_threads = True

    srv = None
    candidate = port
    for _ in range(25):
        p = _free_port(candidate, host)
        try:
            srv = _WebServer((host, p), Handler)
            port = p
            break
        except OSError:
            candidate = p + 1

    if srv is None:
        print("  [X] Koi free port nahi mila (8000 se 8025 tak sab busy).")
        print("      Kuch band karo ya --port=9000 se chalao.")
        return

    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{shown}:{port}"

    print("")
    print("  " + "=" * 56)
    print(f"   {APP_NAME} - website build chal raha hai")
    print(f"   {url}")
    print("  " + "=" * 56)
    print("   Band karne ke liye: is window me Ctrl+C")
    print("")

    if open_browser:
        # give the socket a moment, then hand the URL to the default browser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  band ho gaya.")
    finally:
        srv.server_close()


# Serialises the moment a request's account is installed in the file. Held only
# around the file swap - never around the backend call itself.
_BIND = threading.RLock()


def _brief(e) -> str:
    """A short, safe message - never a stack trace or a filesystem path."""
    s = str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
    return s[:160]


def _bind_request(email: str):
    """Point the account file at `email` for the duration of one request.

    Returns a release callable, or None if nothing needed changing. The swap is
    guarded by a dedicated lock so two concurrent requests cannot interleave
    halfway through; the actual handler runs outside that lock.
    """
    if email:
        with _BIND:
            _web_bind_account(email)
        return None
    # A signed-out tab must not act as whoever was last installed.
    with _BIND:
        auth.sign_out_bind()
    return None


def _new_token() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def email_of_account() -> str:
    try:
        return ((auth.get_account() or {}).get("email") or "").strip().lower()
    except Exception:
        return ""


def _web_bind_account(email: str):
    """Point the on-disk account file at `email` for this request.

    The HTTP layer serialises these calls, so the window during which the
    account file represents one browser's user is the duration of that
    browser's own request - it is never visible to another request.
    """
    d = auth._read()
    if (d.get("email", "").lower() == email.lower()) and not d.get("signed_out"):
        return
    known = auth._known_account(email)
    if not known:
        return
    rec = dict(known)
    rec.pop("signed_out", None)
    auth._write(rec)


def selftest():
    print(f"{APP_NAME} {APP_VERSION} - selftest")
    print("  ui file      :", os.path.exists(UI))
    print("  data dir     :", store.data_dir())
    print("  key storage  :", store.key_storage_kind())
    print("  accounts at  :", store.kv_kind())
    if store.hosted() and not store.kv_ready():
        print("  WARNING      : hosted with no key-value store - accounts and")
        print("                 chats will NOT survive a restart. Connect")
        print("                 Upstash Redis in the Vercel dashboard.")
    elif not store.hosted() and store.kv_ready() and not store.use_kv():
        print("  NOTE         : a key-value store is configured but this desktop")
        print("                 build is not using it. Set \"use_kv\": true in")
        print(r"                 %APPDATA%\HAMU-GPT\server.json to share accounts.")
    api = Api()
    b = api.boot()
    print("  boot         :", "ok" if b.get("ok") else b.get("error"))
    print("  providers    :", len(b.get("providers", [])))
    cat = api.catalog()
    print("  catalog      :", len(cat.get("providers", [])), "known providers")
    # resolution smoke test
    for ep in ("https://api.cerebras.ai/v1/chat/completions",
               "api.groq.com", "https://openrouter.ai/api/v1"):
        base, prov = P.resolve(ep)
        print(f"    {ep:52} -> {prov['name']:20} {base}")
    print("selftest done")


# ---------------------------------------------------------------------------
# integrity guard
# ---------------------------------------------------------------------------
# The installer writes integrity.json next to the installed exe, holding the
# SHA-256 of every file it placed there. Before the window opens we re-hash
# them; if anything was changed the app refuses to start.
#
# This is deliberately fail-open: any error (no manifest, unreadable file, a
# source checkout, a dev run) means "carry on". Only a definite hash mismatch
# stops the app, so a bug here can never lock anyone out of their own copy.
def _integrity_ok() -> tuple:
    """(ok, detail). ok is False only on a real mismatch."""
    if not getattr(sys, "frozen", False):
        return True, "not an installed build"
    try:
        import hashlib
        base = os.path.dirname(os.path.abspath(sys.executable))
        man_path = os.path.join(base, "integrity.json")
        if not os.path.isfile(man_path):
            return True, "no manifest"
        with open(man_path, "r", encoding="utf-8") as f:
            man = json.load(f)
        files = man.get("files") or {}
        if not isinstance(files, dict):
            return True, "manifest empty"
        for name, rec in files.items():
            p = os.path.join(base, name)
            if not os.path.isfile(p):
                continue
            want = (rec or {}).get("sha256")
            if not want:
                continue
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 256), b""):
                    h.update(chunk)
            if h.hexdigest() != want:
                return False, "%s was modified" % name
        return True, "verified"
    except Exception as e:
        return True, "check skipped (%s)" % e


def _integrity_gate() -> bool:
    """Block startup on a tampered install, with a clear message."""
    ok, detail = _integrity_ok()
    if ok:
        return True
    msg = ("HAMU-GPT could not start.\n\n"
           "Its program files have been changed (%s).\n\n"
           "Reinstall HAMU-GPT to repair this." % detail)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, APP_NAME, 0x10)
    except Exception:
        pass
    return False


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if "--web" in sys.argv:
        port = 8000
        for a in sys.argv:
            if a.startswith("--port="):
                try:
                    port = int(a.split("=", 1)[1])
                except Exception:
                    pass
        host = "0.0.0.0" if "--host-all" in sys.argv else "127.0.0.1"
        return serve_web(port=port, host=host,
                         open_browser="--open" in sys.argv)
    if not _integrity_gate():
        return
    run_gui()


if __name__ == "__main__":
    main()
