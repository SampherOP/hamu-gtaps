#!/usr/bin/env python3
"""Model identity verifier.

Reseller gateways advertise "Claude Opus 5.5" or "GPT-5.6" and quietly route you
to something much cheaper. You cannot prove a model's identity from outside with
100% certainty, but you CAN catch the common fakes, and you can collect enough
evidence to make a confident call.

What each probe actually tells you
----------------------------------
api_identity   The `model` field the gateway itself returns. If you asked for
               claude-opus-5-5 and it says llama-3.1-8b, that is a smoking gun.
self_id        What the model says it is. Weak on its own (models lie, and
               resellers can prepend a system prompt) but useful when it
               contradicts the thing you paid for.
letter_count   "How many r's in strawberry" - a real discriminator. Old and
               small models say 2. Modern frontier models say 3.
reasoning      The bat-and-ball problem. Cheap models fail it.
instruction    Strict JSON output. Small models add prose around it.
tokenizer      Token count for a fixed string. Every family tokenises
               differently, so this is a genuine fingerprint.
speed          Tokens per second. A "frontier" model that streams at 400 tok/s
               is almost certainly a small one.
context        A needle buried in a long context. Reveals a truncated window.
"""

from __future__ import annotations

import re
import time

import providers as P

# ---------------------------------------------------------------------------
# a fixed string whose token count differs per family - the fingerprint
# ---------------------------------------------------------------------------
FINGERPRINT_TEXT = (
    "The quick brown fox jumps over the lazy dog. "
    "Antidisestablishmentarianism. Supercalifragilisticexpialidocious. "
    "1234567890 !@#$%^&*() \u0645\u0627\u0646 \u0645\u06cc\u06ba \u062a\u0645\u06be\u0627\u0631\u06d2 "
    "\u0633\u0627\u062a\u06be \u06c1\u0648\u06ba\u06d4 "
    "def fibonacci(n): return n if n < 2 else fibonacci(n-1) + fibonacci(n-2)"
)

# rough token counts for that string, per family. Wider bands = more tolerance.
KNOWN_TOKEN_COUNTS = {
    "openai-gpt4": (88, 118),
    "openai-gpt5": (86, 116),
    "anthropic": (92, 126),
    "google-gemini": (84, 112),
    "meta-llama": (94, 130),
    "deepseek": (90, 122),
    "qwen": (86, 118),
}

# a needle the model must find deep in a haystack
NEEDLE = "PLUMBUS-7731"
HAYSTACK_FILLER = (
    "The maintenance log records routine inspections of the facility. "
    "Nothing unusual was reported during this period. "
)


def _q(prompt, system=None, max_tokens=64):
    """Convenience: build the message list for a single-turn probe."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    return msgs


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    """Normalise a model string for comparison.

    Resellers decorate the same model a dozen ways: 'claude-opus-5-5',
    'anthropic/claude-opus-5.5', 'Claude 3.5 Sonnet (latest)'. Strip the noise
    so 'claude-opus-5-5' and 'claude-opus-5.5' compare equal, but a genuine
    swap still stands out.
    """
    s = (s or "").lower().strip()
    s = s.split("/")[-1]                      # drop vendor prefix
    s = s.split(":")[-1]                      # drop ':free' style suffixes
    s = s.replace("_", "-").replace(".", "-").replace(" ", "-")
    s = re.sub(r"\(.*?\)", "", s)             # drop parentheticals
    s = re.sub(r"-+", "-", s).strip("-")
    for junk in ("-latest", "-beta", "-preview", "-exp", "-instruct", "-chat"):
        if s.endswith(junk):
            s = s[: -len(junk)]
    return s


# Which vendor family a model name belongs to. Longest match wins, so
# 'claude-3-opus' resolves to anthropic via 'claude', not via 'opus'.
FAMILY_WORDS = {
    "claude": "anthropic", "opus": "anthropic", "sonnet": "anthropic",
    "haiku": "anthropic", "anthropic": "anthropic",
    "gpt": "openai", "openai": "openai", "chatgpt": "openai",
    "gpt4": "openai", "gpt5": "openai",
    "gemini": "google", "palm": "google", "bard": "google", "google": "google",
    "llama": "meta", "meta": "meta", "mistral": "mistral", "mixtral": "mistral",
    "qwen": "qwen", "deepseek": "deepseek", "phi": "microsoft",
    "grok": "xai", "kimi": "moonshot", "moonshot": "moonshot",
    "glm": "zai", "zhipu": "zai", "minimax": "minimax",
    "command": "cohere", "cohere": "cohere",
}


def family_of(name: str) -> str:
    """Vendor family of a model string, or '' if unrecognised."""
    n = (name or "").lower()
    best = ""
    for word in FAMILY_WORDS:
        if word in n and len(word) > len(best):
            best = word
    return FAMILY_WORDS.get(best, "") if best else ""


def probe_api_identity(ctx):
    """What does the gateway call this model in its own response?

    This is the strongest probe in the battery, because it reads the reseller's
    own words rather than the model's opinion of itself. Three outcomes:

      mismatch  the families genuinely disagree -> the swap is admitted
      echo      the gateway parroted our alias back. Common, and while it is
                not proof of a swap, it is also NOT proof of honesty, so it is
                reported as a warning rather than a pass.
      match     the names agree and neither is a bare echo
    """
    ok, text, meta = ctx["call"]("Reply with the single word: ready",
                                max_tokens=8)
    if not ok:
        return {"status": "error", "detail": text}
    declared = ((meta or {}).get("model") or "").strip()
    asked = ctx["model"]
    if not declared:
        return {"status": "info",
                "detail": "gateway did not report a model name in the response"}

    na, nd = _norm(asked), _norm(declared)
    fa, fd = family_of(na), family_of(nd)

    if fa and fd and fa != fd:
        return {"status": "warn",
                "detail": (f"you asked for '{asked}' ({fa}) but the gateway's own "
                           f"response says '{declared}' ({fd}) - this is a different "
                           f"vendor's model")}

    if na == nd and asked.strip() == declared.strip():
        # Byte-for-byte identical. An upstream that canonicalises a name would
        # at least change the spelling (dots, vendor prefix, date suffix), so an
        # untouched mirror usually means the gateway reflected what you sent.
        # Plenty of honest resellers do this, so it is NOT a fraud signal - but
        # it is also not confirmation, and this probe will not pretend it is.
        return {"status": "info",
                "detail": (f"gateway returned the exact string you sent "
                           f"('{declared}'). This is inconclusive: it may be honest, "
                           f"or the alias may be rewritten. The other probes decide.")}

    if fa and fa == fd:
        return {"status": "ok",
                "detail": (f"you asked for '{asked}', the gateway reported "
                           f"'{declared}' - same family ({fa}), consistent")}

    return {"status": "info",
            "detail": f"you asked for '{asked}', the gateway reported '{declared}'"}


def probe_self_id(ctx):
    ok, text, meta = ctx["call"](
        "In ONE short line, state the exact AI model and version you are. "
        "Do not hedge, do not explain. Just the name. "
        "If you genuinely do not know your exact version, say 'unknown'.",
        max_tokens=40)
    if not ok:
        return {"status": "error", "detail": text}
    t = (text or "").strip().replace("\n", " ")[:120]
    if not t:
        return {"status": "warn", "detail": "no answer"}
    if "unknown" in t.lower() and len(t) < 40:
        return {"status": "info", "detail": "\u201cunknown\u201d - model is not claiming a version"}
    asked = ctx["model"]
    said = family_of(t)
    wanted = family_of(asked)
    # The gateway's own declared name is stronger evidence than self-ID; if we
    # have both, use the declared name as the comparison target.
    declared = (ctx.get("declared") or "").strip()
    target_name = declared or asked
    target_fam = family_of(target_name) or wanted
    if said and target_fam and said != target_fam:
        return {"status": "warn",
                "detail": (f"you asked for {wanted or '?'} "
                           f"(gateway says '{target_name}', {target_fam}), "
                           f"but it says: \u201c{t}\u201d")}
    # Names a family we recognise, and it agrees
    if said and (declared or wanted):
        return {"status": "ok", "detail": f"it says: \u201c{t}\u201d"}
    return {"status": "info", "detail": f"it says: \u201c{t}\u201d"}


def probe_letter_count(ctx):
    """strawberry: 3 r's. Old/small models say 2."""
    ok, text, meta = ctx["call"](
        "How many times does the letter 'r' appear in the word 'strawberry'? "
        "Answer with ONLY the number.",
        max_tokens=8)
    if not ok:
        return {"status": "error", "detail": text}
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    if not digits:
        return {"status": "warn", "detail": f"no number in the reply: \u201c{(text or '').strip()[:60]}\u201d"}
    n = int(digits[:2])
    if n == 3:
        return {"status": "ok", "detail": "answered 3 (correct - modern model behaviour)"}
    if n == 2:
        return {"status": "warn",
                "detail": "answered 2 (wrong). This is the classic signature of an "
                          "older or smaller model being sold as a frontier one."}
    return {"status": "warn", "detail": f"answered {n} (wrong, expected 3)"}


def probe_reasoning(ctx):
    """Bat and ball: $0.05. Cheap models usually say $0.10."""
    ok, text, meta = ctx["call"](
        "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the "
        "ball. How much does the ball cost? Answer with ONLY the amount.",
        max_tokens=12)
    if not ok:
        return {"status": "error", "detail": text}
    t = (text or "").strip().lower()
    if "0.05" in t or "5 cent" in t or "5c" in t:
        return {"status": "ok", "detail": "answered $0.05 (correct)"}
    if "0.10" in t or "10 cent" in t:
        return {"status": "warn",
                "detail": "answered $0.10 (the intuitive wrong answer). Weak reasoning."}
    return {"status": "info", "detail": f"answered: \u201c{t[:60]}\u201d"}


def probe_instruction(ctx):
    """Strict output. Small models wrap it in prose."""
    ok, text, meta = ctx["call"](
        'Reply with ONLY this JSON and nothing else: {"ok":true,"n":7}',
        max_tokens=40)
    if not ok:
        return {"status": "error", "detail": text}
    t = (text or "").strip()
    if t == '{"ok":true,"n":7}':
        return {"status": "ok", "detail": "exact match"}
    if '"ok"' in t and '"n"' in t and "7" in t:
        return {"status": "warn",
                "detail": f"right content, but it wrapped it in prose instead of "
                          f"following 'ONLY this JSON': \u201c{t[:60]}\u201d"}
    return {"status": "warn",
            "detail": f"ignored the instruction: \u201c{t[:80]}\u201d"}


def probe_tokenizer(ctx):
    """Token count for a fixed string - a real family fingerprint."""
    ok, text, meta = ctx["call"](FINGERPRINT_TEXT + "\n\nReply with: ok",
                                 max_tokens=8)
    if not ok:
        return {"status": "error", "detail": text}
    u = (meta or {}).get("usage") or {}
    n = int(u.get("in") or 0)
    if not n:
        return {"status": "info", "detail": "gateway reported no token usage"}
    matches = [fam for fam, (lo, hi) in KNOWN_TOKEN_COUNTS.items() if lo <= n <= hi]
    if not matches:
        return {"status": "info",
                "detail": f"{n} prompt tokens - outside every known family band"}
    return {"status": "info",
            "detail": f"{n} prompt tokens - consistent with: {', '.join(matches)}"}


def probe_speed(ctx):
    """Tokens/sec. Frontier models are not the fastest."""
    prompt = ("Write a 120-word description of a rainy city street at night. "
              "Plain prose, no lists.")
    t0 = time.time()
    ok, text, meta = ctx["call"](prompt, max_tokens=300)
    dt = max(0.01, time.time() - t0)
    if not ok:
        return {"status": "error", "detail": text}
    u = (meta or {}).get("usage") or {}
    out = int(u.get("out") or 0)
    if not out:
        out = max(1, len(text or "") // 4)
    tps = out / dt
    note = ""
    if tps > 250:
        note = " - unusually fast for a frontier model; small models stream this fast"
    elif tps > 120:
        note = " - fast"
    elif tps < 15:
        note = " - slow (consistent with a large model, or a slow gateway)"
    return {"status": "info", "detail": f"{tps:.0f} tok/s ({out} tokens in {dt:.1f}s){note}"}


def probe_context(ctx):
    """Bury a code in a long context and ask for it back."""
    filler = HAYSTACK_FILLER * 260            # ~ 13k chars, ~3k tokens
    doc = filler + f"\n\nIMPORTANT REFERENCE: {NEEDLE}\n\n" + filler
    ok, text, meta = ctx["call"](
        "Read the document below and answer with ONLY the IMPORTANT REFERENCE "
        "code it contains.\n\n" + doc,
        max_tokens=20)
    if not ok:
        return {"status": "error", "detail": text}
    t = (text or "").upper()
    u = (meta or {}).get("usage") or {}
    toks = int(u.get("in") or 0)
    if NEEDLE in t:
        return {"status": "ok",
                "detail": f"found the needle in ~{toks or '?'} tokens of context"}
    return {"status": "warn",
            "detail": f"could NOT find the needle in ~{toks or '?'} tokens of context "
                      f"(answered: \u201c{(text or '').strip()[:50]}\u201d)"}


PROBES = [
    ("api_identity", "Gateway-declared identity", probe_api_identity),
    ("self_id",      "Self-identification",       probe_self_id),
    ("letter_count", "Letter counting",           probe_letter_count),
    ("reasoning",    "Bat-and-ball reasoning",    probe_reasoning),
    ("instruction",  "Strict instruction",        probe_instruction),
    ("tokenizer",    "Tokenizer fingerprint",     probe_tokenizer),
    ("context",      "Context needle",            probe_context),
    ("speed",        "Generation speed",          probe_speed),
]

# The cheap, decisive subset. Two calls instead of eight, so the user can
# check a whole provider's catalogue without waiting on the slow probes.
FAST_PROBES = ["api_identity", "self_id", "letter_count"]


def run_verify(provider, model, on_progress=None, should_stop=None,
               probes=None, declared=None):
    """Run the probe battery. Returns a report dict.

    on_progress(index, total, probe_id, label)  - called before each probe
    should_stop()                               - return True to abort early
    probes                                      - list of probe ids to run
                                                  (default: all)
    declared                                    - the model name the gateway
                                                  already reported for this
                                                  model in this session, if any
    """
    base = provider["base"]
    key = provider.get("key") or None

    def call(prompt, max_tokens=64, system=None):
        ok, text, meta = P.chat(base, key, model, _q(prompt, system),
                                timeout=180)
        if ok:
            return True, text, meta
        # retry once - gateways are flaky
        time.sleep(0.6)
        ok, text, meta = P.chat(base, key, model, _q(prompt, system),
                                timeout=180)
        return ok, text, meta

    chosen = list(PROBES)
    if probes:
        want = set(probes)
        chosen = [p for p in PROBES if p[0] in want]

    ctx = {"call": call, "model": model, "provider": provider,
           "declared": declared or ""}
    results = []
    total = len(chosen)

    for i, (pid, label, fn) in enumerate(chosen):
        if should_stop and should_stop():
            break
        if on_progress:
            try:
                on_progress(i, total, pid, label)
            except Exception:
                pass
        t0 = time.time()
        try:
            r = fn(ctx)
        except Exception as e:
            r = {"status": "error", "detail": f"{type(e).__name__}: {e}"[:200]}
        r["id"] = pid
        r["label"] = label
        r["seconds"] = round(time.time() - t0, 1)
        results.append(r)

    # ---- overall assessment ----
    warns = [r for r in results if r["status"] == "warn"]
    errors = [r for r in results if r["status"] == "error"]
    oks = [r for r in results if r["status"] == "ok"]

    if len(errors) >= max(2, total // 2):
        verdict = "unreachable"
        headline = "The model never answered - most probes failed."
    elif len(warns) >= 3:
        verdict = "suspicious"
        headline = ("Several probes look wrong. This model is probably not what "
                    "it claims to be.")
    elif warns:
        verdict = "mixed"
        headline = "Mostly fine, but some things do not match."
    else:
        verdict = "consistent"
        headline = ("All probes pass - the model behaves the way it claims to. "
                    "Note: this is not proof, only strong evidence.")

    return {
        "ok": True,
        "model": model,
        "provider": provider.get("name"),
        "base": base,
        "verdict": verdict,
        "headline": headline,
        "identity": identity_summary(model, declared, results),
        "probes_ran": [r["id"] for r in results],
        "results": results,
        "counts": {"ok": len(oks), "warn": len(warns),
                   "info": len(results) - len(oks) - len(warns) - len(errors),
                   "error": len(errors)},
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def identity_summary(asked: str, declared: str, results: list) -> dict:
    """A single, glanceable verdict: is this model what it says it is?

    Combines the gateway's own declared name with the probe evidence. The
    states are deliberately honest - `echo` in particular means "cannot tell",
    not "genuine", because a gateway that rewrites the model field will look
    identical to an honest one at this layer.
    """
    asked = (asked or "").strip()
    declared = (declared or "").strip()
    by_id = {r.get("id"): r for r in (results or [])}

    api = by_id.get("api_identity") or {}
    api_status = api.get("status")
    if not declared and api.get("detail"):
        # The probe quotes the declared name in one of a few phrasings; pull it
        # back out so the summary works even when the caller had no `declared`.
        for pat in (r"response says '([^']+)'",
                    r"gateway reported '([^']+)'",
                    r"returned the exact string you sent \('([^']+)'\)"):
            m = re.search(pat, api["detail"])
            if m:
                declared = m.group(1)
                break

    fa, fd = family_of(asked), family_of(declared)

    # A cross-vendor family mismatch is decisive on its own - it does not need
    # the api_identity probe to have flagged it, because we derive it from the
    # same two strings. Only skip when the probe could not run at all.
    if fa and fd and fa != fd and api_status != "error":
        state = "renamed"
        label = "RENAMED"
        note = (f"You picked '{asked}', but the gateway is serving '{declared}'. "
                f"This provider is fake/renamed.")
    elif declared and _norm(asked) == _norm(declared):
        state = "echo"
        label = "ECHO"
        note = (f"The gateway is echoing your own name '{declared}' back - it is "
                f"hiding the real model name. Run the full probes to prove it.")
    elif api_status == "error":
        state = "unknown"
        label = "?"
        note = "The gateway never answered, so the identity could not be determined."
    else:
        state = "real"
        label = "REAL"
        note = (f"'{declared or asked}' is consistent - no family mismatch found.")

    suspects = [r for r in (results or [])
                if r.get("status") == "warn" and r.get("id") != "api_identity"]

    return {
        "state": state,
        "label": label,
        "asked": asked,
        "declared": declared,
        "provider": None,          # filled by caller if wanted
        "family_asked": fa,
        "family_declared": fd,
        "note": note,
        "suspect_probes": [r.get("id") for r in suspects],
    }
