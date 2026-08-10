"""Redaction helpers - keep Google auth material out of logs.

Every byte of subprocess stdout/stderr passes through `scrub_text` before it
reaches a log handler, and every argv we log passes through `scrub_args`. State
we persist (error strings, artifact URLs, quarantine reasons) goes through it
too, since state.json outlives any single run.

The pipeline itself never reads, stores, or forwards credentials: auth lives
entirely inside notebooklm-py's own storage. This module exists so that a stray
cookie in an error message cannot leak into pipeline.log.

Patterns are matched against the shapes notebooklm-py actually produces: HTTP
cookie headers, Playwright `storage_state.json` (where the cookie *name* is a
value and the secret sits under a generic "value" key), Python-repr cookie
dicts, and OAuth/master-token blobs.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# Cookie names Google uses for session auth.
_COOKIE_NAMES = (
    "SID", "HSID", "SSID", "APISID", "SAPISID", "NID", "SIDCC", "LSID",
    "__Secure-1PSID", "__Secure-1PSIDTS", "__Secure-1PSIDCC", "__Secure-3PSID",
    "__Secure-3PSIDTS", "__Secure-3PSIDCC", "OSID", "COMPASS",
)
# Longest first: alternation is leftmost-first, so "SID" must not win over
# "__Secure-1PSIDTS" at the same position.
_COOKIE_ALT = "|".join(re.escape(n) for n in sorted(_COOKIE_NAMES, key=len, reverse=True))

# Every pattern redacts its whole match, except for two optional named groups
# that are preserved as context: `keep` (text before the secret) and `keep2`
# (text after it). All other groups must be non-capturing.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # SID=value / "SID": "value" / 'SID': 'value'
    re.compile(
        rf"(?P<keep>[\"']?(?<![A-Za-z0-9_])(?:{_COOKIE_ALT})(?![A-Za-z0-9_])"
        rf"[\"']?\s*[=:]\s*)[\"']?[^\s;,\"'}}]+[\"']?",
        re.IGNORECASE,
    ),
    # `Authorization: Bearer <credential>`. This MUST run before the generic
    # key/value rule below: that rule would otherwise consume "Bearer" as the
    # value and leave the actual credential in the log.
    re.compile(
        r"(?P<keep>\b(?:Bearer|Basic|SAPISIDHASH|SAPISID1PHASH|SAPISID3PHASH)\s+)"
        r"[^\s,;\"']{6,}",
        re.IGNORECASE,
    ),
    # Secret-ish key/value pairs, quoted or bare. The lookahead skips values that
    # are just an auth scheme - the rule above has already handled those.
    re.compile(
        r"(?P<keep>\b(?:oauth[_-]?token|master[_-]?token|access[_-]?token"
        r"|refresh[_-]?token|id[_-]?token|api[_-]?key|authorization|cookie"
        r"|storage_state)\b\s*[=:]\s*)"
        r"(?!(?:Bearer|Basic|SAPISIDHASH|SAPISID1PHASH|SAPISID3PHASH)\b)"
        r"\"?[^\s,;\"']{6,}\"?",
        re.IGNORECASE,
    ),
    # Google OAuth / master-token blobs.
    re.compile(r"\bya29\.[A-Za-z0-9._\-]+"),
    re.compile(r"\b(?:oauth2_4|aas_et)/[A-Za-z0-9._\-]+"),
    # Playwright storage_state, both member orders.
    re.compile(
        rf"(?P<keep>\"name\"\s*:\s*\"(?:{_COOKIE_ALT})\"\s*,\s*\"value\"\s*:\s*)\"[^\"]*\"",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<keep>\"value\"\s*:\s*)\"[^\"]*\""
        rf"(?P<keep2>\s*,\s*\"name\"\s*:\s*\"(?:{_COOKIE_ALT})\")",
        re.IGNORECASE,
    ),
    # Any long opaque "value" member: storage_state is full of them, and none of
    # them are ours to log.
    re.compile(r"(?P<keep>\"value\"\s*:\s*)\"[^\"]{16,}\""),
    # JSON members whose key name itself looks secret.
    re.compile(
        r"(?P<keep>\"[A-Za-z0-9_\-]*(?:token|cookie|secret|password|sid)[A-Za-z0-9_\-]*\""
        r"\s*:\s*)\"[^\"]*\"",
        re.IGNORECASE,
    ),
)

# Flags whose *following* argv element is a secret.
_SECRET_FLAGS = frozenset({"--oauth-token", "--android-id", "--master-token-value"})


def _replace(match: re.Match[str]) -> str:
    groups = match.groupdict()
    return f"{groups.get('keep') or ''}{REDACTED}{groups.get('keep2') or ''}"


def scrub_text(text: str | None) -> str:
    """Return `text` with anything that looks like auth material replaced."""
    if not text:
        return ""
    out = text
    for pattern in _PATTERNS:
        out = pattern.sub(_replace, out)
    return out


def scrub_args(args: list[str]) -> list[str]:
    """Return a loggable copy of an argv list, with secret values masked."""
    out: list[str] = []
    mask_next = False
    for arg in args:
        if mask_next:
            out.append(REDACTED)
            mask_next = False
            continue
        if arg in _SECRET_FLAGS:
            out.append(arg)
            mask_next = True
            continue
        if "=" in arg and arg.split("=", 1)[0] in _SECRET_FLAGS:
            out.append(f"{arg.split('=', 1)[0]}={REDACTED}")
            continue
        out.append(scrub_text(arg))
    return out
