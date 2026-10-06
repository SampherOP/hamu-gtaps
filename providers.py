#!/usr/bin/env python3
"""Provider engine for Nexus Chat.

Everything here is network-only and provider-agnostic:

  * KNOWN          - registry of providers we can identify by hostname
  * normalize_base - turn any pasted endpoint into a usable API base
  * identify       - match a base URL against the registry
  * list_models    - GET {base}/models and normalise the reply
  * is_free        - decide whether a model is on a free tier
  * chat           - OpenAI-compatible /chat/completions call
  * get_usage      - real balance/quota where the provider exposes it

Nothing in this module touches the UI or the filesystem.
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30
UA = "NexusChat/1.0 (+https://localhost)"

# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
# free:  "all"          -> the whole catalog is on a free tier
#        "suffix:free"  -> only ids ending with the given suffix are free
#        "partial"      -> some models are free, we can't tell which
#        "none"         -> paid only
# usage: how to read a real balance, or None (we fall back to a local counter)

KNOWN = [
    # ---- free tiers, no credit card -------------------------------------
    {
        "id": "cerebras", "name": "Cerebras", "color": "#f55036",
        "hosts": ["api.cerebras.ai"],
        "base": "https://api.cerebras.ai/v1",
        "site": "https://cerebras.ai/cloud",
        "free": "all",
        "free_note": "1M tokens/day free, 14,400 req/day, no card",
        "key_hint": "csk-...",
    },
    {
        "id": "groq", "name": "Groq", "color": "#f55036",
        "hosts": ["api.groq.com"],
        "base": "https://api.groq.com/openai/v1",
        "site": "https://console.groq.com",
        "free": "all",
        "free_note": "Free tier, ~1,000 req/day, no card",
        "key_hint": "gsk_...",
    },
    {
        "id": "google", "name": "Google AI Studio", "color": "#4285f4",
        "hosts": ["generativelanguage.googleapis.com"],
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "site": "https://aistudio.google.com/apikey",
        "free": "all",
        "free_note": "Free tier (Gemini Flash). Note: free-tier data may be used for training.",
        "key_hint": "AIza...",
    },
    {
        "id": "openrouter", "name": "OpenRouter", "color": "#8b5cf6",
        "hosts": ["openrouter.ai"],
        "base": "https://openrouter.ai/api/v1",
        "site": "https://openrouter.ai/keys",
        "free": "suffix:free",
        "free_note": "Models ending in :free are free (50 req/day, 1000 with $10 credit)",
        "key_hint": "sk-or-...",
        "usage": "openrouter",
    },
    {
        "id": "github", "name": "GitHub Models", "color": "#6e7681",
        "hosts": ["models.inference.ai.azure.com", "models.github.ai"],
        "base": "https://models.inference.ai.azure.com",
        "site": "https://github.com/settings/tokens",
        "free": "all",
        "free_note": "Free with a GitHub account (50-150 req/day)",
        "key_hint": "ghp_... / github_pat_...",
    },
    {
        "id": "cloudflare", "name": "Cloudflare Workers AI", "color": "#f38020",
        "hosts": ["api.cloudflare.com"],
        "base": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        "site": "https://dash.cloudflare.com",
        "free": "all",
        "free_note": "10,000 neurons/day free",
        "key_hint": "Cloudflare API token",
        "needs_account": True,
    },
    {
        "id": "nvidia", "name": "NVIDIA NIM", "color": "#76b900",
        "hosts": ["integrate.api.nvidia.com"],
        "base": "https://integrate.api.nvidia.com/v1",
        "site": "https://build.nvidia.com",
        "free": "partial",
        "free_note": "1,000 free credits on signup",
        "key_hint": "nvapi-...",
    },
    {
        "id": "sambanova", "name": "SambaNova", "color": "#e11d48",
        "hosts": ["api.sambanova.ai"],
        "base": "https://api.sambanova.ai/v1",
        "site": "https://cloud.sambanova.ai",
        "free": "partial",
        "free_note": "$5 free credit, no card, 30-day expiry",
        "key_hint": "SambaNova key",
    },
    {
        "id": "mistral", "name": "Mistral", "color": "#ff7000",
        "hosts": ["api.mistral.ai"],
        "base": "https://api.mistral.ai/v1",
        "site": "https://console.mistral.ai",
        "free": "partial",
        "free_note": "Free 'experiment' tier available",
        "key_hint": "Mistral key",
    },
    {
        "id": "cohere", "name": "Cohere", "color": "#39594d",
        "hosts": ["api.cohere.ai", "api.cohere.com"],
        "base": "https://api.cohere.ai/compatibility/v1",
        "site": "https://dashboard.cohere.com/api-keys",
        "free": "partial",
        "free_note": "Free trial key, 1,000 calls/month",
        "key_hint": "Cohere key",
    },
    # ---- paid ------------------------------------------------------------
    {
        "id": "openai", "name": "OpenAI", "color": "#10a37f",
        "hosts": ["api.openai.com"],
        "base": "https://api.openai.com/v1",
        "site": "https://platform.openai.com/api-keys",
        "free": "none", "free_note": "Paid", "key_hint": "sk-...",
    },
    {
        "id": "anthropic", "name": "Anthropic", "color": "#d97757",
        "hosts": ["api.anthropic.com"],
        "base": "https://api.anthropic.com/v1",
        "site": "https://console.anthropic.com",
        "free": "none", "free_note": "Paid", "key_hint": "sk-ant-...",
    },
    {
        "id": "deepseek", "name": "DeepSeek", "color": "#4d6bfe",
        "hosts": ["api.deepseek.com"],
        "base": "https://api.deepseek.com/v1",
        "site": "https://platform.deepseek.com",
        "free": "none", "free_note": "Very cheap, paid", "key_hint": "sk-...",
        "usage": "deepseek",
    },
    {
        "id": "moonshot", "name": "Moonshot / Kimi", "color": "#000000",
        "hosts": ["api.moonshot.cn", "api.moonshot.ai"],
        "base": "https://api.moonshot.ai/v1",
        "site": "https://platform.moonshot.ai",
        "free": "none", "free_note": "Paid", "key_hint": "sk-...",
        "usage": "moonshot",
    },
    {
        "id": "xai", "name": "xAI (Grok)", "color": "#111111",
        "hosts": ["api.x.ai"],
        "base": "https://api.x.ai/v1",
        "site": "https://console.x.ai",
        "free": "none", "free_note": "Paid", "key_hint": "xai-...",
    },
    {
        "id": "together", "name": "Together AI", "color": "#0f6fff",
        "hosts": ["api.together.xyz", "api.together.ai"],
        "base": "https://api.together.xyz/v1",
        "site": "https://api.together.xyz/settings/api-keys",
        "free": "none", "free_note": "Paid", "key_hint": "Together key",
    },
    {
        "id": "fireworks", "name": "Fireworks AI", "color": "#ff5c00",
        "hosts": ["api.fireworks.ai"],
        "base": "https://api.fireworks.ai/inference/v1",
        "site": "https://fireworks.ai/account/api-keys",
        "free": "partial", "free_note": "$1 free credit", "key_hint": "fw_...",
    },
    {
        "id": "perplexity", "name": "Perplexity", "color": "#20808d",
        "hosts": ["api.perplexity.ai"],
        "base": "https://api.perplexity.ai",
        "site": "https://www.perplexity.ai/settings/api",
        "free": "none", "free_note": "Paid", "key_hint": "pplx-...",
    },
    {
        "id": "novita", "name": "Novita AI", "color": "#7c3aed",
        "hosts": ["api.novita.ai"],
        "base": "https://api.novita.ai/v3/openai",
        "site": "https://novita.ai",
        "free": "partial", "free_note": "Free credit on signup", "key_hint": "sk_...",
    },
    {
        "id": "hyperbolic", "name": "Hyperbolic", "color": "#0ea5e9",
        "hosts": ["api.hyperbolic.xyz"],
        "base": "https://api.hyperbolic.xyz/v1",
        "site": "https://app.hyperbolic.xyz",
        "free": "partial", "free_note": "Free credit on signup", "key_hint": "Hyperbolic key",
    },
    {
        "id": "ai21", "name": "AI21 Labs", "color": "#e4002b",
        "hosts": ["api.ai21.com"],
        "base": "https://api.ai21.com/studio/v1",
        "site": "https://studio.ai21.com",
        "free": "partial", "free_note": "$10 free credit, 3-month expiry", "key_hint": "AI21 key",
    },
    # ---- local -----------------------------------------------------------
    {
        "id": "ollama", "name": "Ollama (local)", "color": "#8b8b8b",
        "hosts": ["127.0.0.1:11434", "localhost:11434"],
        "base": "http://127.0.0.1:11434/v1",
        "site": "https://ollama.com",
        "free": "all", "free_note": "100% local, unlimited, free",
        "key_hint": "not needed", "local": True, "no_key": True,
    },
    {
        "id": "lmstudio", "name": "LM Studio (local)", "color": "#8b8b8b",
        "hosts": ["127.0.0.1:1234", "localhost:1234"],
        "base": "http://127.0.0.1:1234/v1",
        "site": "https://lmstudio.ai",
        "free": "all", "free_note": "100% local, unlimited, free",
        "key_hint": "not needed", "local": True, "no_key": True,
    },
    {
        "id": "llamacpp", "name": "llama.cpp (local)", "color": "#8b8b8b",
        "hosts": ["127.0.0.1:8080", "localhost:8080"],
        "base": "http://127.0.0.1:8080/v1",
        "site": "https://github.com/ggml-org/llama.cpp",
        "free": "all", "free_note": "100% local, unlimited, free",
        "key_hint": "not needed", "local": True, "no_key": True,
    },
]

GENERIC = {
    "id": "custom", "name": "Custom provider", "color": "#10a37f",
    "hosts": [], "base": "", "site": "",
    "free": "partial", "free_note": "Unknown — we could not identify this host",
    "key_hint": "API key",
}

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")

# ---------------------------------------------------------------------------
# documented free-tier limits (per day)
# ---------------------------------------------------------------------------
# These are the providers' own published numbers, used to show a "% used" bar.
# They change without notice - treat them as a guide, not a contract. `None`
# means the provider doesn't publish a usable number, so we only show the raw
# local counter for that axis.
LIMITS = {
    "cerebras":   {"req_day": 14400, "tok_day": 1_000_000,
                   "note": "1M tokens/day · 14,400 req/day"},
    "groq":       {"req_day": 1000,  "tok_day": 100_000,
                   "note": "~1,000 req/day · token cap usually binds first"},
    "google":     {"req_day": 1500,  "tok_day": None,
                   "note": "1,500 req/day on Gemini Flash"},
    "github":     {"req_day": 150,   "tok_day": None,
                   "note": "150 req/day (low tier) · 50 for high tier"},
    "openrouter": {"req_day": 50,    "tok_day": None,
                   "note": "50 free-model req/day · 1,000 after $10 credit"},
    "cloudflare": {"req_day": None,  "tok_day": None,
                   "note": "10,000 neurons/day"},
    "nvidia":     {"req_day": None,  "tok_day": None,
                   "note": "1,000 credits on signup"},
    "sambanova":  {"req_day": None,  "tok_day": None,
                   "note": "$5 credit, 30-day expiry"},
    "cohere":     {"req_day": 33,    "tok_day": None,
                   "note": "1,000 calls/month (~33/day)"},
    "mistral":    {"req_day": None,  "tok_day": None,
                   "note": "free experiment tier"},
    "fireworks":  {"req_day": None,  "tok_day": None,
                   "note": "$1 credit"},
    "novita":     {"req_day": None,  "tok_day": None,
                   "note": "free credit on signup"},
    "hyperbolic": {"req_day": None,  "tok_day": None,
                   "note": "free credit on signup"},
}


def limits_for(provider: dict) -> dict | None:
    """Daily free-tier limits for a provider dict (from the registry or a saved row)."""
    pid = (provider or {}).get("provider_id") or (provider or {}).get("id")
    lim = LIMITS.get(pid)
    if not lim:
        return None
    if (provider or {}).get("local"):
        return None
    return dict(lim)


def usage_percent(provider: dict, local: dict):
    """How much of today's free tier has been used.

    Returns {"pct": 0-100, "axis": "requests"|"tokens"|None, "limit": n,
             "used": n, "note": str, "exhausted": bool} or None if unknown.

    The binding axis is whichever we hit first, so we report the worse of the two.
    """
    lim = limits_for(provider)
    if not lim:
        return None
    req_used = int((local or {}).get("requests") or 0)
    tok_used = int(((local or {}).get("in") or 0) + ((local or {}).get("out") or 0))

    candidates = []
    if lim.get("req_day"):
        candidates.append(("requests", req_used, lim["req_day"]))
    if lim.get("tok_day"):
        candidates.append(("tokens", tok_used, lim["tok_day"]))
    if not candidates:
        return {"pct": None, "axis": None, "limit": None, "used": None,
                "note": lim.get("note", ""), "exhausted": False}

    axis, used, cap = max(candidates, key=lambda c: c[1] / c[2])
    pct = min(100.0, (used / cap) * 100.0) if cap else None
    return {
        "pct": round(pct, 1) if pct is not None else None,
        "axis": axis,
        "limit": cap,
        "used": used,
        "note": lim.get("note", ""),
        "exhausted": bool(cap and used >= cap),
    }


# ---------------------------------------------------------------------------
# pricing - USD per 1,000,000 tokens, (input, output)
# ---------------------------------------------------------------------------
# Best-effort list prices for well-known models. Providers change these often,
# and resellers add their own margin, so the UI always lets the user set an
# explicit per-provider price which wins over anything here.
PRICES = {
    # openai
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "o3": (2.00, 8.00),
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
    "gpt-oss-120b": (0.15, 0.60),
    "gpt-oss-20b": (0.05, 0.20),
    # anthropic
    "claude-opus-4": (15.00, 75.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-haiku": (0.25, 1.25),
    "claude-haiku-4-5": (1.00, 5.00),
    # google
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemma-3-27b": (0.10, 0.20),
    # meta
    "llama-3.3-70b": (0.59, 0.79),
    "llama-3.1-70b": (0.59, 0.79),
    "llama-3.1-8b": (0.05, 0.08),
    "llama-4-scout": (0.11, 0.34),
    "llama-4-maverick": (0.20, 0.60),
    # deepseek
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "deepseek-v3": (0.27, 1.10),
    # qwen
    "qwen3-32b": (0.10, 0.30),
    "qwen3-235b": (0.20, 0.60),
    "qwen2.5-72b": (0.35, 0.40),
    # mistral
    "mistral-large": (2.00, 6.00),
    "mistral-small": (0.10, 0.30),
    "ministral-3b": (0.04, 0.04),
    # others
    "kimi-k2": (0.60, 2.50),
    "glm-4.5": (0.60, 2.20),
    "grok-3": (3.00, 15.00),
    "grok-4": (3.00, 15.00),
}


def price_for(model_id: str, provider: dict | None = None):
    """(price_in, price_out) per 1M tokens, or None if unknown.

    Order of precedence:
      1. an explicit price saved on the provider (resellers set this)
      2. exact id match in PRICES
      3. longest substring match in PRICES  (so "openai/gpt-4o-mini-2024" hits gpt-4o-mini)
    """
    prov = provider or {}

    pin = prov.get("price_in")
    pout = prov.get("price_out")
    if pin is not None and pout is not None:
        try:
            return float(pin), float(pout)
        except (TypeError, ValueError):
            pass

    mid = (model_id or "").lower()
    if not mid:
        return None
    if mid in PRICES:
        return PRICES[mid]

    # Substring match, but guarded: the match must not sit inside a longer
    # version-like token. Without this, "gpt-5.6-sol" would match "gpt-5" and
    # we'd show a confidently wrong price - worse than showing none.
    best = None
    for key in PRICES:
        idx = mid.find(key)
        if idx < 0:
            continue
        after = mid[idx + len(key): idx + len(key) + 1]
        if after and (after.isdigit() or after == "."):
            continue                      # gpt-5 does NOT match gpt-5.6-sol
        before = mid[idx - 1: idx] if idx > 0 else ""
        if before and before.isalnum():
            continue                      # don't match inside a longer word
        if best is None or len(key) > len(best):
            best = key
    return PRICES[best] if best else None


def message_cost(provider: dict, model_id: str, in_tokens, out_tokens):
    """USD for one exchange. Returns None when we have no price for the model."""
    p = price_for(model_id, provider)
    if not p:
        return None
    pin, pout = p
    return (int(in_tokens or 0) / 1e6) * pin + (int(out_tokens or 0) / 1e6) * pout


def fmt_money(v, decimals=6):
    """Small amounts need more precision than a currency formatter gives."""
    if v is None:
        return ""
    if v == 0:
        return "$0"
    if v < 0.000001:
        return "<$0.000001"
    if v < 0.01:
        return "$" + f"{v:.6f}".rstrip("0").rstrip(".")
    if v < 1:
        return "$" + f"{v:.4f}"
    return "$" + f"{v:.2f}"

# ---------------------------------------------------------------------------
# http helpers
# ---------------------------------------------------------------------------
def _opener(url: str):
    """Localhost must bypass any ambient proxy; everything else respects it.

    The SSL context has to be attached via an HTTPSHandler - OpenerDirector.open()
    does not take a `context` argument (only urllib.request.urlopen does).
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    handlers = []
    ctx = _ssl_ctx()
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    if host in LOCAL_HOSTS or host.endswith(".local"):
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _ssl_ctx():
    try:
        return ssl.create_default_context()
    except Exception:
        return None


def request(url, method="GET", body=None, key=None, extra=None, timeout=TIMEOUT):
    """Low-level JSON request. Returns (status, parsed_or_None, raw_text)."""
    headers = {"accept": "application/json", "user-agent": UA}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    if key:
        headers["authorization"] = "Bearer " + key.strip()
    if extra:
        headers.update(extra)

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener(url).open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw or "{}"), raw
            except Exception:
                return r.status, None, raw
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        parsed = None
        try:
            parsed = json.loads(raw or "{}")
        except Exception:
            pass
        return e.code, parsed, raw
    except Exception as e:
        return 0, None, str(e)


def error_text(status, parsed, raw):
    """Turn whatever came back into one readable line.

    It says WHERE the failure came from. A bare "Internal Server Error" reads
    like our own app broke, when in fact the provider's gateway answered - and
    that is a completely different thing to fix.
    """
    msg = ""
    if parsed:
        err = parsed.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("detail") or json.dumps(err)[:200]
        elif err:
            msg = str(err)
        else:
            msg = parsed.get("message") or parsed.get("detail") or ""
    if not msg and raw:
        msg = raw.strip()[:400]
    msg = str(msg or "").strip()

    if not status:
        return msg or "network error - the provider could not be reached"
    if status >= 500:
        return ("HTTP %d from the provider (their side, not this app): %s"
                % (status, msg or "server error"))
    if status == 429:
        return "HTTP 429 - rate limited by the provider. %s" % (msg or
                                                                "Try again shortly.")
    if status in (401, 403):
        return "HTTP %d - the API key was rejected. %s" % (status, msg or
                                                           "Check the key for this provider.")
    if status == 404:
        return "HTTP 404 - the provider does not know that model. %s" % (
            msg or "Check the model name.")
    return "HTTP %d: %s" % (status, msg) if msg else "HTTP %d" % status


# ---------------------------------------------------------------------------
# endpoint handling
# ---------------------------------------------------------------------------
def normalize_base(endpoint: str) -> str:
    """Accept anything a user might paste and return a usable API base.

    https://api.cerebras.ai/v1/chat/completions -> https://api.cerebras.ai/v1
    https://api.cerebras.ai/v1                  -> https://api.cerebras.ai/v1
    https://api.cerebras.ai                     -> https://api.cerebras.ai/v1
    api.cerebras.ai                             -> https://api.cerebras.ai/v1
    """
    s = (endpoint or "").strip().strip('"').strip("'")
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    p = urllib.parse.urlsplit(s)
    path = (p.path or "").rstrip("/")

    # strip known suffixes
    for suffix in ("/chat/completions", "/completions", "/responses", "/embeddings"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    path = path.rstrip("/")

    # already ends in a version segment -> keep it
    if re.search(r"/v\d+[a-z]*$", path) or path.endswith("/openai"):
        pass
    elif path in ("", "/"):
        path = "/v1"
    # anything else (custom gateway paths) is left alone

    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, "", ""))


def identify(base: str) -> dict:
    """Match a base URL against KNOWN. Always returns a dict."""
    host = (urllib.parse.urlparse(base).hostname or "").lower()
    port = urllib.parse.urlparse(base).port
    hostport = f"{host}:{port}" if port else host
    for p in KNOWN:
        for h in p["hosts"]:
            if h == hostport or h == host or host.endswith("." + h.split(":")[0]):
                return dict(p)
    g = dict(GENERIC)
    g["base"] = base
    g["detected_host"] = host
    return g


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
def _extract_models(parsed):
    """Handle OpenAI {data:[...]}, Google {models:[...]}, bare lists."""
    if not isinstance(parsed, dict):
        return []
    items = parsed.get("data")
    if not isinstance(items, list):
        items = parsed.get("models")
    if not isinstance(items, list):
        return []
    out = []
    for m in items:
        if isinstance(m, str):
            out.append({"id": m})
        elif isinstance(m, dict):
            mid = m.get("id") or m.get("name") or m.get("model")
            if not mid:
                continue
            mid = str(mid)
            # google returns "models/gemini-..." -> keep the tail
            if mid.startswith("models/"):
                mid = mid.split("/", 1)[1]
            ctx = (m.get("context_length") or m.get("context_window")
                   or m.get("max_context_length") or m.get("inputTokenLimit"))
            out.append({"id": mid, "ctx": ctx})
    return out


def list_models(base: str, key: str | None, provider: dict | None = None):
    """GET {base}/models. Returns (ok, models, error)."""
    url = base.rstrip("/") + "/models"
    status, parsed, raw = request(url, key=key, timeout=25)
    if status == 200 and parsed is not None:
        models = _extract_models(parsed)
        if models:
            return True, models, ""
        return True, [], "endpoint replied but listed no models"
    return False, [], error_text(status, parsed, raw)


def is_free(model_id: str, provider: dict) -> bool:
    mode = (provider or {}).get("free", "none")
    if mode == "all":
        return True
    if isinstance(mode, str) and mode.startswith("suffix:"):
        return model_id.endswith(mode.split(":", 1)[1])
    return False


def free_mode_label(provider: dict) -> str:
    mode = (provider or {}).get("free", "none")
    if mode == "all":
        return "whole catalog free"
    if isinstance(mode, str) and mode.startswith("suffix:"):
        return "free with " + mode.split(":", 1)[1] + " suffix"
    if mode == "partial":
        return "some free"
    return "paid"


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------
def chat(base: str, key: str | None, model: str, messages: list,
         timeout=600, extra_headers=None):
    """OpenAI-compatible chat completion. Returns (ok, text, meta)."""
    url = base.rstrip("/") + "/chat/completions"
    body = {"model": model, "messages": messages}
    status, parsed, raw = request(url, "POST", body, key=key,
                                  extra=extra_headers, timeout=timeout)
    if status == 200 and isinstance(parsed, dict):
        choices = parsed.get("choices") or []
        if not choices:
            return False, "", {"error": "empty response", "raw": raw[:300]}
        msg = choices[0].get("message") or {}
        text = msg.get("content")
        if isinstance(text, list):                       # anthropic-style blocks
            text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
        if text is None:
            text = choices[0].get("text") or ""
        usage = parsed.get("usage") or {}
        return True, str(text), {
            "model": parsed.get("model") or model,
            "usage": {
                "in": usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
                "out": usage.get("completion_tokens") or usage.get("output_tokens") or 0,
            },
        }
    return False, "", {"error": error_text(status, parsed, raw), "status": status}


def chat_stream(base: str, key: str | None, model: str, messages: list,
                on_chunk, timeout=600):
    """Streaming chat completion, with one retry for transient failures.

    Gateways hand out 500s, 502s, 503s and 429s that work fine on a second
    try, and a stream that ends with no content at all is usually the same
    kind of blip. Retrying once turns most of those into a real answer instead
    of an error the user has to notice and re-send by hand.

    Only transient failures are retried. A 401 (bad key), a 403 or a 404
    (wrong model) is never retried - that would waste time and bury the one
    message that actually explains the problem.
    """
    attempts = 2
    result = (False, "", {"error": "no attempt made", "status": 0})
    for attempt in range(attempts):
        started = time.time()
        ok, text, meta = _chat_stream_once(base, key, model, messages,
                                           on_chunk, timeout=timeout)
        if ok:
            return ok, text, meta
        result = (ok, text, meta)
        status = (meta or {}).get("status") or 0
        transient = (status == 0 or status == 408 or status == 429
                     or status >= 500)
        if not transient or attempt == attempts - 1:
            break
        # Only retry a FAST failure. A 500 that took half a minute to arrive
        # means the provider is genuinely struggling, and a second attempt
        # would just make the user wait another half minute for the same
        # answer. A 500 that comes back in two seconds is a blip, and retrying
        # it is exactly what fixes it.
        if time.time() - started > 10.0:
            break
        time.sleep(0.7)
    return result


def _chat_stream_once(base: str, key: str | None, model: str, messages: list,
                      on_chunk, timeout=600):
    """Streaming chat completion (SSE).

    Calls on_chunk(text_delta) as tokens arrive, and returns the same
    (ok, full_text, meta) shape as chat(). If the provider ignores `stream`
    and replies with a normal JSON body, we detect that and fall back cleanly.
    """
    url = base.rstrip("/") + "/chat/completions"
    body = {"model": model, "messages": messages, "stream": True}
    headers = {"accept": "text/event-stream", "content-type": "application/json",
               "user-agent": UA}
    if key:
        headers["authorization"] = "Bearer " + key.strip()
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    parts = []
    usage = {}
    real_model = model
    stopped = False
    try:
        with _opener(url).open(req, timeout=timeout) as r:
            ctype = (r.headers.get("content-type") or "").lower()
            if "text/event-stream" not in ctype:
                # provider ignored stream= -> read it as a normal reply
                raw = r.read().decode("utf-8", "replace")
                try:
                    parsed = json.loads(raw or "{}")
                except Exception:
                    return False, "", {"error": raw[:300] or "bad response"}
                return _from_completion(parsed, model, on_chunk)

            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    d = json.loads(data)
                except Exception:
                    continue
                if isinstance(d.get("usage"), dict):
                    usage = d["usage"]
                if d.get("model"):
                    real_model = d["model"]
                for ch in (d.get("choices") or []):
                    delta = ch.get("delta") or {}
                    piece = delta.get("content")
                    if piece is None and ch.get("text"):
                        piece = ch["text"]
                    if piece:
                        # on_chunk may return False to say "stop now". We must NOT
                        # swallow that - a bare `except: pass` here means a cancelled
                        # request keeps draining the whole stream in the background.
                        try:
                            keep_going = on_chunk(piece)
                        except Exception:
                            keep_going = True
                        if keep_going is False:
                            stopped = True
                            break
                        parts.append(piece)
                if stopped:
                    break
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        parsed = None
        try:
            parsed = json.loads(raw or "{}")
        except Exception:
            pass
        return False, "", {"error": error_text(e.code, parsed, raw), "status": e.code}
    except Exception as e:
        return False, "", {"error": str(e)[:300], "status": 0}

    text = "".join(parts)
    if not text.strip():
        return False, "", {"error": "provider streamed nothing back", "status": 0}
    return True, text, {
        "model": real_model,
        "usage": {
            "in": usage.get("prompt_tokens") or 0,
            "out": usage.get("completion_tokens") or 0,
        },
    }


def _from_completion(parsed, model, on_chunk):
    """Shared shape-conversion for a non-streamed body."""
    if not isinstance(parsed, dict):
        return False, "", {"error": "bad response"}
    choices = parsed.get("choices") or []
    if not choices:
        return False, "", {"error": "empty response"}
    msg = choices[0].get("message") or {}
    text = msg.get("content")
    if isinstance(text, list):
        text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
    text = str(text or choices[0].get("text") or "")
    try:
        on_chunk(text)
    except Exception:
        pass
    u = parsed.get("usage") or {}
    return True, text, {
        "model": parsed.get("model") or model,
        "usage": {"in": u.get("prompt_tokens") or 0,
                  "out": u.get("completion_tokens") or 0},
    }


# ---------------------------------------------------------------------------
# usage / balance
# ---------------------------------------------------------------------------
def get_usage(provider: dict, key: str, base: str):
    """Real balance where the provider exposes it. Returns dict or None.

    shape: {"ok":bool, "kind":str, "label":str, "detail":str, "remaining":num|None}
    """
    kind = provider.get("usage")
    if not kind:
        return None

    if kind == "openrouter":
        st, parsed, raw = request("https://openrouter.ai/api/v1/key", key=key, timeout=20)
        if st == 200 and isinstance(parsed, dict):
            d = parsed.get("data") or {}
            rem = d.get("limit_remaining")
            used = d.get("usage")
            daily = d.get("free_model_daily_requests") or {}
            parts = []
            if used is not None:
                parts.append(f"used all-time: ${float(used):.4f}")
            if rem is not None:
                parts.append(f"remaining: ${float(rem):.4f}")
            if daily:
                parts.append(f"free req today: {daily.get('used', 0)}/{daily.get('limit', '?')}")
            return {"ok": True, "kind": "credits", "label": "OpenRouter balance",
                    "detail": " · ".join(parts) or "no data",
                    "remaining": rem}
        return {"ok": False, "kind": "credits", "label": "OpenRouter balance",
                "detail": error_text(st, parsed, raw)}

    if kind == "deepseek":
        st, parsed, raw = request("https://api.deepseek.com/user/balance",
                                  key=key, timeout=20)
        if st == 200 and isinstance(parsed, dict):
            infos = parsed.get("balance_infos") or []
            if infos:
                b = infos[0]
                return {"ok": True, "kind": "credits",
                        "label": "DeepSeek balance",
                        "detail": f"{b.get('total_balance')} {b.get('currency','')}"
                                  f" (granted {b.get('granted_balance')})",
                        "remaining": b.get("total_balance")}
            return {"ok": True, "kind": "credits", "label": "DeepSeek balance",
                    "detail": "available: " + str(parsed.get("is_available"))}
        return {"ok": False, "kind": "credits", "label": "DeepSeek balance",
                "detail": error_text(st, parsed, raw)}

    if kind == "moonshot":
        for url in ("https://api.moonshot.ai/v1/users/me/balance",
                    "https://api.moonshot.cn/v1/users/me/balance"):
            st, parsed, raw = request(url, key=key, timeout=20)
            if st == 200 and isinstance(parsed, dict):
                d = parsed.get("data") or parsed
                return {"ok": True, "kind": "credits", "label": "Moonshot balance",
                        "detail": f"available {d.get('available_balance')} "
                                  f"{d.get('currency','')}",
                        "remaining": d.get("available_balance")}
        return {"ok": False, "kind": "credits", "label": "Moonshot balance",
                "detail": "could not read balance"}

    return None


# ---------------------------------------------------------------------------
# convenience
# ---------------------------------------------------------------------------
def resolve(endpoint: str):
    """Full pipeline: normalise -> identify -> pick the best base.

    Returns (base, provider_dict).

    If the user typed a bare host (or host + /v1) and we recognise the provider,
    we use the registry's canonical base - that is what fixes hosts whose real
    path is not /v1, e.g. Groq (/openai/v1) or GitHub Models (no /v1 at all).
    A custom path the user actually typed is always respected.
    """
    norm = normalize_base(endpoint)
    p = identify(norm)
    parsed = urllib.parse.urlparse(norm)
    path = (parsed.path or "").rstrip("/")

    is_known = p.get("id") != "custom"
    generic_path = path in ("", "/v1")
    if is_known and generic_path and p.get("base"):
        norm = p["base"]
    elif p.get("id") == "custom":
        p["base"] = norm

    p["base"] = norm
    p["free_label"] = free_mode_label(p)
    p["free"] = p.get("free", "none")
    return norm, p


def describe_provider(base: str) -> dict:
    """Identify + attach the normalised base, for the UI to display."""
    norm, p = resolve(base)
    return p
