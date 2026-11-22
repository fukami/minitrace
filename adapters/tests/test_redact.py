"""Key-name redaction of configuration carried into traces (redact.py), and
its use by the vibe, droid and claude-code pre-processors."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

_ADAPTERS = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADAPTERS))
for sub in ("vibe", "droid"):
    sys.path.insert(0, str(_ADAPTERS / sub))

from redact import _SECRET_PAIRS, MARKER, is_secret_key, redact_by_key  # noqa: E402

SENTINEL = "not-a-real-value-0000"


def test_secret_key_names():
    for k in ("token", "apiKey", "api_key", "x-api-key", "APIKey", "client_secret",
              "Authorization", "authToken", "password", "privateKey", "cookies"):
        assert is_secret_key(k), k
    for k in ("access_token_value", "authHeader", "GITHUB_TOKEN", "client_key"):
        assert is_secret_key(k), k
    for k in ("api_key_env_var", "token_limit", "auth_type", "max_tokens", "tokenizer",
              "model", "keybindings", "key", "monkey", "session_id", "",
              "tokenUsage", "inclusiveTokenUsage", "browser_auth_base_url"):
        assert not is_secret_key(k), k


def test_plural_suffixed_and_env_style_names():
    # Plurals, a trailing `pem`, and env-style *_KEY / *_PAT names
    for k in ("secrets", "passwords", "api_keys", "apiKeys", "private_keys", "privateKeys",
              "client_secrets", "private_key_pem", "OPENAI_KEY", "GH_PAT", "GITHUB_PAT_VALUE"):
        assert is_secret_key(k), k
    # `tokens` as a scalar key names a count; lower-case *_key is a setting
    for k in ("tokens", "input_tokens", "totalTokens", "sort_key", "cache_key", "PAT",
              "SSH_KEY_PATH", "cert_pem"):
        assert not is_secret_key(k), k


def test_numbered_compound_and_provider_names():
    # Ruled 2026-10-22: trailing numbers are ignored; a word that runs a
    # secret word on at its end is secret; lower-case <provider>_key and
    # github/gh/gitlab _pat are secret.
    for k in ("api_key_1", "password2", "token_2", "apiKey2", "secrets_3", "api_key_1_2",
              "api_key_2_value", "api_key_value_2",
              "accesstoken", "clientsecret", "csrftoken", "OLDPWD", "ACCESSTOKEN", "sessioncookie",
              "completiontoken", "oauth2", "maxpassword", "nextsecret",
              "openai_key", "anthropic_key", "aws_key", "hf_key", "replicate_key", "sort_openai_key",
              "openAIKey", "OpenAIKey", "xAIKey", "deepSeekKey", "openRouterKey", "huggingFaceKey",
              "github_pat", "gh_pat", "gitlab_pat", "gitHubPat", "gitLabPat", "openai_key_value"):
        assert is_secret_key(k), k
    # A number alone, or a count, stays what it was; tokens stays a count,
    # and so does a count, tokenizer or pagination word before token; other
    # lower-case *_key and *_pat stay settings; a version suffix is not a
    # number.
    for k in ("version2", "key_1", "pat", "2", "max_tokens_2", "maxtokens", "inputtokens",
              "totaltoken", "numtoken", "maxtoken", "eostoken", "bostoken", "padtoken", "stoptoken",
              "nexttoken", "pagetoken", "NEXTTOKEN",
              "sort_key", "cache_key", "model_key", "file_pat", "openai_model", "github_url",
              "monkey", "tokenizer", "passthrough", "token_v2"):
        assert not is_secret_key(k), k
    providers = ("openai", "anthropic", "gemini", "google", "mistral", "groq", "cohere", "deepseek",
                 "openrouter", "together", "xai", "huggingface", "hf", "azure", "aws", "github", "gitlab",
                 "stripe", "slack", "fireworks", "perplexity", "replicate")
    assert all(is_secret_key(f"{p}_key") and is_secret_key(f"{p}Key") for p in providers)
    cfg = {"openai_key": SENTINEL, "api_key_1": SENTINEL, "nested": {"accesstoken": SENTINEL},
           "sort_key": "name", "max_tokens_2": 7}
    out, n = redact_by_key(cfg)
    assert SENTINEL not in json.dumps(out) and n == 3, (out, n)
    assert out["sort_key"] == "name" and out["max_tokens_2"] == 7, out


def test_every_secret_pair_run_together_in_any_casing_and_plural():
    # Ruled 2026-10-24: (auth, key) is a secret pair, and every pair is secret
    # run together as well, in any casing, singular or plural, and run on to
    # the end of a longer word (awssecretaccesskey). Ruled 2026-10-26: a run-on
    # before a separate `key` stays a setting (oauth_key, awssecretaccess_key),
    # and an upper-case run before a lower-case pair word is read whole
    # (SECRETkey as SECRET key).
    pairs = {("api", "key"), ("access", "key"), ("private", "key"), ("session", "key"),
             ("signing", "key"), ("encryption", "key"), ("secret", "key"), ("client", "key"),
             ("auth", "key"),
             # ruled 2026-10-29
             ("ssh", "key"), ("master", "key"), ("deploy", "key"), ("hmac", "key"), ("priv", "key"),
             ("seed", "phrase"), ("database", "url")}
    for a, b in sorted(pairs):
        A = a.capitalize()
        for s in ("", "s"):
            for k in (f"{a}_{b}{s}", f"{a}{b.capitalize()}{s}", f"{A}{b.capitalize()}{s}",
                      f"{a}_{b}{s}".upper(), f"{a}-{b}{s}", f"{a}{b}{s}", f"{a}{b}{s}".upper(),
                      f"{A}{b}{s}", f"aws{a}{b}{s}", f"aws{A}{b}{s}", f"aws_{a}{b}{s}",
                      f"{a}{b}{s}_value", f"{a}{b}{s}2", f"{a.upper()}{b}{s}",
                      f"aws{a.upper()}{b}{s}", f"{a.upper()}{b}{s}_value"):
                assert is_secret_key(k), k
    for k in ("secretkey", "SECRETKEY", "accesskey", "sessionkey", "signingkey", "encryptionkey",
              "authkey", "clientkey", "awssecretaccesskey", "secret_key", "access_key", "accessKey",
              "secretkeys", "secret_keys", "secretKeys", "auth_key", "authKey", "AUTH_KEY",
              "SECRETkey", "APIkey", "AUTHkey", "ACCESSkeys"):
        assert is_secret_key(k), k
    for k in ("key", "keys", "monkey", "turnkey", "hotkey", "passkey", "sort_key", "cache_key",
              "secretkeyring", "keysecret_id", "oauth_key", "subsession_key", "httpclient_key",
              "awssecretaccess_key", "awssecretaccessKey", "oauthKey", "HTTPServer",
              "XMLHttpRequest", "PASSthrough", "SORTkey", "OAUTHkey", "Oauth_key", "Laws_key",
              "Nexttoken", "Totaltoken", "Pagetoken", "Akey"):
        assert not is_secret_key(k), k
    cfg = {"secretkey": SENTINEL, "nested": {"authKey": SENTINEL, "AWSSECRETACCESSKEYS": [SENTINEL]},
           "APIkey": SENTINEL, "sort_key": "name", "oauth_key": "id"}
    out, n = redact_by_key(cfg)
    assert SENTINEL not in json.dumps(out) and n == 4, (out, n)
    assert out["sort_key"] == "name" and out["oauth_key"] == "id", out
    assert _SECRET_PAIRS == pairs


def test_tokens_is_secret_only_over_a_collection():
    cfg = {"tokens": {"github": SENTINEL, "npm": SENTINEL}, "api_keys": [SENTINEL],
           "max_tokens": 4096, "usage": {"input_tokens": 12, "tokens": 30}}
    out, n = redact_by_key(cfg)
    assert SENTINEL not in json.dumps(out)
    assert out["tokens"] == {"github": MARKER, "npm": MARKER} and out["api_keys"] == [MARKER]
    assert out["max_tokens"] == 4096 and out["usage"] == {"input_tokens": 12, "tokens": 30}
    assert n == 3


def test_redaction_keeps_structure_and_counts():
    cfg = {"model": "m", "providers": [{"name": "p", "api_key": SENTINEL,
                                        "api_key_env_var": "P_API_KEY"}],
           "auth": {"type": "oauth", "refresh": SENTINEL, "scopes": ["a", "b"], "on": True},
           "token_limit": 5, "password": ""}
    out, n = redact_by_key(cfg)
    assert SENTINEL not in json.dumps(out)
    assert out["providers"][0] == {"name": "p", "api_key": MARKER, "api_key_env_var": "P_API_KEY"}
    assert out["auth"] == {"type": MARKER, "refresh": MARKER, "scopes": [MARKER, MARKER], "on": True}
    assert out["token_limit"] == 5 and out["password"] == "" and out["model"] == "m"
    assert n == 5
    assert cfg["providers"][0]["api_key"] == SENTINEL      # input untouched


def test_too_deep_fails_closed():
    deep: dict = {}
    cur = deep
    for _ in range(5000):
        cur["a"] = {}
        cur = cur["a"]
    assert redact_by_key(deep) == (MARKER, 1)


def test_vibe_config_snapshot_is_redacted(tmp_path):
    import preprocess_vibe
    meta = {"config": {"active_model": "m", "providers": [{"name": "p", "api_key": SENTINEL}]}}
    c = preprocess_vibe.build_container(tmp_path, meta, [], [])
    assert SENTINEL not in json.dumps(c)
    assert c["config_snapshot"]["providers"][0]["api_key"] == MARKER
    assert c["config_snapshot_redacted"] == 1


def test_droid_settings_raw_is_redacted():
    import preprocess_droid
    p = preprocess_droid.project_settings({"model": "m", "customHeaders": {"Authorization": SENTINEL}})
    assert SENTINEL not in json.dumps(p)
    assert p["_raw"]["customHeaders"]["Authorization"] == MARKER and p["_raw_redacted"] == 1


def test_claude_code_environment_snapshot_is_redacted():
    spec = importlib.util.spec_from_file_location(
        "preprocess_claude_code", _ADAPTERS / "claude-code" / "preprocess_claude_code.py")
    assert spec is not None and spec.loader is not None
    pp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pp)
    rec = {"type": "attachment", "attachment": {"type": "environment",
           "snapshot": {"platform": "darwin", "GITHUB_TOKEN": SENTINEL}}}
    ctx = pp.collect_harness_context([rec])
    assert SENTINEL not in json.dumps(ctx)
    ext = ctx["ext_claude_code"]
    assert ext["environment_snapshots"] == [{"platform": "darwin", "GITHUB_TOKEN": MARKER}]
    assert ext["environment_redacted"] == 1


def test_credential_names_added_on_2026_10_29_are_secret():
    # Ruled 2026-10-29: ssh, master, deploy, hmac and priv keys, JWTs,
    # mnemonics and seed phrases, DSNs, database URLs and connection
    # strings are secret in every casing; pass and pw only as the whole last
    # word; their neighbours that name something else are kept.
    for k in ("ssh_key", "sshKey", "SSH_KEY", "master_key", "masterKey", "deploy_key", "deployKeys",
              "hmac_key", "hmacKey", "privkey", "priv_key", "privKey", "pass", "db_pass", "pw", "user_pw",
              "jwt", "id_jwt", "mnemonic", "walletMnemonic", "seed_phrase", "seedPhrase", "dsn", "sentry_dsn",
              "DATABASE_URL", "databaseUrl", "connection_string", "connectionString", "ConnectionString",
              "db_connection_string_2", "connectionstring", "dbconnectionstring",
              # the plural (.NET appsettings' ConnectionStrings section) and a
              # value word after the pair
              "connection_strings", "ConnectionStrings", "connectionstrings", "connection_string_value",
              "connection_string_2_value", "CONNECTION_STRINGS_2_VALUE"):
        assert is_secret_key(k), k
    for k in ("bypass", "compass", "mpw", "passes_total", "pw_policy", "jwt_expiry", "dsn_count",
              "seed", "random_seed", "database", "database_name", "connection", "connection_timeout",
              "string", "key", "sort_key", "monkey", "phrase"):
        assert not is_secret_key(k), k
    red, n = redact_by_key({"sentry_dsn": SENTINEL, "connection_string": SENTINEL, "pass": SENTINEL,
                            "seed": 7, "bypass": "x"})
    assert n == 3 and red == {"sentry_dsn": MARKER, "connection_string": MARKER, "pass": MARKER,
                              "seed": 7, "bypass": "x"}, (n, red)
