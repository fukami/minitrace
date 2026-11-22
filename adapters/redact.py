"""Key-name redaction for configuration an adapter copies into a trace.

Some adapters carry a producer's configuration verbatim (vibe's config
snapshot, droid's settings sidecar, claude-code's environment snapshots),
and such objects hold credentials often enough that a shared trace must not
carry them as-is. The rule is by key name only, so it is predictable and
keeps the structure: a string or number under a key that names a secret
(`token`, `apiKey`, `client_secret`, `Authorization`, ...) is replaced by
MARKER, and so is every such value inside an object or list under that key.
The secret word must end the key name, optionally followed by one word such
as `value`, `header` or `pem`: `tokenUsage`, `api_key_env`, `token_limit`,
`auth_type` and `browser_auth_base_url` name a measure, a reference or a
setting, and are kept. Plurals count like the singular (`secrets`,
`api_keys`), except `tokens`, which as a scalar names a count
(`max_tokens`, `input_tokens`) and is secret only over an object or list.
An environment-style name ending in `_KEY` or `_PAT` (`OPENAI_KEY`,
`GH_PAT`) is secret; lower-case `*_key` is not (`sort_key`, `cache_key`),
except after a credential provider's name in any casing (`openai_key`,
`openAIKey`, `aws_key`), and lower-case `*_pat` is secret after `github`,
`gh` or `gitlab` (`gitHubPat`). Ruled 2026-10-22: trailing numbers are
ignored (`api_key_1`, `password2`, `api_key_2_value`), and a word that
runs a secret word on at its end is secret (`accesstoken`, `clientsecret`,
`csrftoken`), except a count, tokenizer or pagination word before `token`
(`totaltoken`, `eostoken`, `nexttoken`); other such compounds fail closed
(`completiontoken`, an `oauth2` section). Ruled 2026-10-24: `auth_key` is
a secret pair like `secret_key`, and every pair is secret run together too,
singular or plural, also at the end of a longer word (`secretkey`,
`authkeys`, `awssecretaccesskey`); ruled 2026-10-26, a run-on before a
separate `key` is not (`oauth_key`). A name is also read with an
upper-case run kept whole, so `SECRETkey` and `APIkey` are secret too
(ruled 2026-10-26). Booleans and nulls are kept. The caller records the count, so a reader can
tell "nothing to redact" from "not checked".

This does not find secrets under innocuous keys or inside free text; see
the README.
"""
from __future__ import annotations

import re
from typing import Any

__all__ = ["MARKER", "is_secret_key", "redact_by_key"]

MARKER = "[redacted]"

_WORDS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
# Read a second way, with an upper-case run before lower case kept whole,
# since _WORDS splits SECRETkey as `secre tkey` (ruled 2026-10-26). A
# capitalised word stays one word (Oauth, Nexttoken).
_CASE_RUNS = re.compile(r"[A-Z]{2,}(?=[a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_SECRET_WORDS = frozenset({
    "token", "secret", "password", "passwd", "pwd", "passphrase", "credential",
    "credentials", "auth", "oauth", "authorization", "cookie", "cookies",
    "bearer", "apikey", "privatekey", "jwt", "mnemonic", "dsn",
})
_SECRET_PAIRS = frozenset({
    ("api", "key"), ("access", "key"), ("private", "key"), ("session", "key"),
    ("signing", "key"), ("encryption", "key"), ("secret", "key"), ("client", "key"),
    ("auth", "key"), ("ssh", "key"), ("master", "key"), ("deploy", "key"), ("hmac", "key"),
    ("priv", "key"), ("seed", "phrase"), ("database", "url"),
})
# Secret only as the whole last word: run on at the end of a longer word
# they name something else (bypass, compass, mpw) (ruled 2026-10-29).
_WHOLE_WORDS = frozenset({"pass", "pw"})
# Checked before a trailing value word is dropped, since the pair ends in
# one (connection_string) (ruled 2026-10-29).
_VALUE_WORD_PAIRS = frozenset({("connection", "string"), ("connection", "strings")})
_PLURAL_WORDS = frozenset(w + "s" for w in _SECRET_WORDS if not w.endswith("s")) - {"tokens"}
_PLURAL_PAIRS = frozenset((a, b + "s") for a, b in _SECRET_PAIRS)
# A pair run together is secret too, also at the end of a longer word
# (secretkey, awssecretaccesskey, secretkeys; ruled 2026-10-24).
_JOINED_PAIRS = frozenset(a + b for a, b in _SECRET_PAIRS | _PLURAL_PAIRS)
# Secret over an object or list only: as a scalar the word names a count.
_COLLECTION_WORDS = frozenset({"tokens"})
_ENV_NAME = re.compile(r"[A-Z0-9]+(?:_[A-Z0-9]+)+")
_ENV_WORDS = frozenset({"key", "pat"})
# A word ending in one of these is secret too (accesstoken); `tokens` stays
# collection-only, as above.
_COMPOUND_ENDS = _SECRET_WORDS | _PLURAL_WORDS
# ...except a count, tokenizer or pagination word run on to `token`
# (totaltoken, eostoken, nexttoken; ruled 2026-10-22). Other compounds stay
# secret, failing closed (completiontoken, oauth2 and auth0 sections).
_COUNT_TOKEN_PREFIXES = frozenset({"total", "num", "max", "eos", "bos", "pad", "stop", "next", "page"})
# Lower-case `<provider>_key` and `<host>_pat` name a credential.
_PROVIDERS = frozenset({
    "openai", "anthropic", "gemini", "google", "mistral", "groq", "cohere", "deepseek",
    "openrouter", "together", "xai", "huggingface", "hf", "azure", "aws", "github", "gitlab",
    "stripe", "slack", "fireworks", "perplexity", "replicate",
})
_PAT_HOSTS = frozenset({"github", "gh", "gitlab"})
# One trailing word that still names the secret value itself.
_VALUE_WORDS = frozenset({"value", "string", "str", "header", "headers", "data", "b64", "base64",
                          "pem"})


def _words(key: str, pattern: re.Pattern[str] = _WORDS) -> list[str]:
    words = [w.lower() for w in pattern.findall(key)]
    _drop_numbers(words)
    if len(words) > 1 and words[-1] in _VALUE_WORDS:
        words.pop()
        _drop_numbers(words)   # api_key_2_value as api_key_value_2
    return words


def _drop_numbers(words: list[str]) -> None:
    while len(words) > 1 and words[-1].isdigit():
        words.pop()


def is_secret_key(key: str) -> bool:
    if any(_value_word_pair(key, p) for p in (_WORDS, _CASE_RUNS)):
        return True
    return _is_secret(_words(key), key) or _is_secret(_words(key, _CASE_RUNS), key)


def _value_word_pair(key: str, pattern: re.Pattern[str]) -> bool:
    words = [w.lower() for w in pattern.findall(key)]
    _drop_numbers(words)
    # One more value word may follow the pair, a number before it too
    # (connection_string_value, connection_string_2_value), as it may follow
    # any secret word.
    if len(words) > 2 and words[-1] in _VALUE_WORDS:
        trimmed = words[:-1]
        _drop_numbers(trimmed)
        if tuple(trimmed[-2:]) in _VALUE_WORD_PAIRS:
            return True
    return (tuple(words[-2:]) in _VALUE_WORD_PAIRS
            or any(words[-1].endswith(a + b) for a, b in _VALUE_WORD_PAIRS) if words else False)


def _is_secret(words: list[str], key: str) -> bool:
    if not words:
        return False
    last = words[-1]
    if last in _SECRET_WORDS or last in _PLURAL_WORDS or last in _WHOLE_WORDS:
        return True
    if any(last != end and last.endswith(end)
           and not (end == "token" and last[:-len(end)] in _COUNT_TOKEN_PREFIXES)
           for end in _COMPOUND_ENDS):
        return True
    if tuple(words[-2:]) in _SECRET_PAIRS or tuple(words[-2:]) in _PLURAL_PAIRS:
        return True
    if any(last.endswith(joined) for joined in _JOINED_PAIRS):
        return True
    # The provider may span several words (openAIKey, gitHubPat).
    names = {"".join(words[i:-1]) for i in range(len(words) - 1)}
    if (last == "key" and names & _PROVIDERS) or (last == "pat" and names & _PAT_HOSTS):
        return True
    return last in _ENV_WORDS and _ENV_NAME.fullmatch(key) is not None


def _is_secret_collection_key(key: str) -> bool:
    words = _words(key)
    return bool(words) and words[-1] in _COLLECTION_WORDS


def redact_by_key(value: Any) -> tuple[Any, int]:
    """Return a redacted copy of `value` and the number of values replaced.
    The input is not modified. A value nested too deeply to walk is replaced
    whole (fails closed), counted as one."""
    count = 0

    def walk(v: Any, under_secret_key: bool) -> Any:
        nonlocal count
        if isinstance(v, dict):
            return {k: walk(x, under_secret_key or (isinstance(k, str) and (
                        is_secret_key(k) or (isinstance(x, (dict, list))
                                             and _is_secret_collection_key(k)))))
                    for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, under_secret_key) for x in v]
        if under_secret_key and not isinstance(v, bool) and isinstance(v, (str, int, float)) \
                and v != "":
            count += 1
            return MARKER
        return v

    try:
        return walk(value, False), count
    except RecursionError:
        return MARKER, 1
