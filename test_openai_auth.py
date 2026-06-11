#!/usr/bin/env python3
"""Send a minimal OpenAI API request using credentials from Codex auth.json."""

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_PROMPT = "Reply with exactly: ok"
REFRESH_ENDPOINT = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_REFRESH_SCOPE = "openid profile email offline_access api.responses.write"
RESPONSES_WRITE_SCOPE = "api.responses.write"


def load_auth(auth_path: Path) -> Dict[str, Any]:
    try:
        with auth_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"Invalid JSON in {auth_path}: {exc}\n"
            "Note: JSON string values must be quoted, e.g. "
            '"OPENAI_API_KEY": "sk-..."'
        ) from exc
    except FileNotFoundError as exc:
        raise SystemExit(f"auth file not found: {auth_path}") from exc


def load_api_key(data: Dict[str, Any]) -> str:
    api_key = data.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("No OPENAI_API_KEY found in auth.json or environment.")
    if not isinstance(api_key, str):
        raise SystemExit("OPENAI_API_KEY must be a JSON string")
    return api_key


def load_chatgpt_access_token(data: Dict[str, Any]) -> str:
    tokens = data.get("tokens")
    access_token = None
    if isinstance(tokens, dict):
        access_token = tokens.get("access_token")
    access_token = access_token or os.getenv("OPENAI_ACCESS_TOKEN")
    if not access_token:
        raise SystemExit("No tokens.access_token found in auth.json or OPENAI_ACCESS_TOKEN.")
    if not isinstance(access_token, str):
        raise SystemExit("tokens.access_token must be a JSON string")
    return access_token


def load_chatgpt_refresh_token(data: Dict[str, Any]) -> str:
    tokens = data.get("tokens")
    refresh_token = None
    if isinstance(tokens, dict):
        refresh_token = tokens.get("refresh_token")
    refresh_token = refresh_token or os.getenv("OPENAI_REFRESH_TOKEN")
    if not refresh_token:
        raise SystemExit("No tokens.refresh_token found in auth.json or OPENAI_REFRESH_TOKEN.")
    if not isinstance(refresh_token, str):
        raise SystemExit("tokens.refresh_token must be a JSON string")
    return refresh_token


def load_chatgpt_account_id(data: Dict[str, Any]) -> Optional[str]:
    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        account_id = tokens.get("account_id")
    else:
        account_id = None
    account_id = account_id or data.get("account_id") or os.getenv("OPENAI_ACCOUNT_ID")
    if account_id is None:
        return None
    if not isinstance(account_id, str) or not account_id:
        return None
    return account_id


def decode_jwt_payload(token: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return {}


def decode_jwt_header(token: str) -> Dict[str, Any]:
    parts = token.split(".")
    if not parts or not parts[0]:
        return {}
    header = parts[0]
    header += "=" * (-len(header) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(header).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return {}


def format_timestamp(epoch: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(epoch, int) or isinstance(epoch, bool):
        return None
    try:
        iso = dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None
    now = int(time.time())
    if epoch < now:
        status = "expired"
    elif epoch == now:
        status = "expires_now"
    else:
        status = "not_expired"
    return {
        "unix": epoch,
        "iso": iso,
        "status": status,
        "seconds_until": epoch - now,
    }


TIME_CLAIM_FIELDS = (
    "iat",
    "exp",
    "nbf",
    "auth_time",
)


def summarize_time_claims(payload: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for field in TIME_CLAIM_FIELDS:
        value = payload.get(field)
        formatted = format_timestamp(value)
        if formatted is not None:
            summary[field] = formatted
    return summary


def summarize_identity(payload: Dict[str, Any]) -> Dict[str, Any]:
    keep = (
        "iss",
        "sub",
        "aud",
        "jti",
        "sid",
        "name",
        "email",
        "email_verified",
        "auth_provider",
    )
    out: Dict[str, Any] = {}
    for field in keep:
        if field in payload:
            out[field] = payload[field]
    return out


def summarize_openai(payload: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in ("chatgpt_account_id", "chatgpt_user_id", "user_id"):
        if key in payload:
            out[key] = payload[key]
    nested = payload.get("https://api.openai.com/auth")
    if isinstance(nested, dict):
        out["auth"] = {
            "chatgpt_account_id": nested.get("chatgpt_account_id"),
            "chatgpt_user_id": nested.get("chatgpt_user_id"),
            "chatgpt_plan_type": nested.get("chatgpt_plan_type"),
            "chatgpt_subscription_active_start": nested.get("chatgpt_subscription_active_start"),
            "chatgpt_subscription_active_until": nested.get("chatgpt_subscription_active_until"),
            "user_id": nested.get("user_id"),
        }
        if "organizations" in nested:
            out["organizations"] = nested["organizations"]
    return out


def decode_token_full(token: str) -> Dict[str, Any]:
    header = decode_jwt_header(token)
    payload = decode_jwt_payload(token)
    scopes = payload.get("scp") if isinstance(payload, dict) else None
    if isinstance(scopes, list):
        scope_list = [scope for scope in scopes if isinstance(scope, str)]
    elif isinstance(scopes, str):
        scope_list = scopes.split()
    else:
        scope_list = []
    return {
        "header": header,
        "payload": payload,
        "time_claims": summarize_time_claims(payload),
        "identity": summarize_identity(payload),
        "openai": summarize_openai(payload),
        "scopes": scope_list,
    }


def describe_token(token: str) -> str:
    decoded = decode_token_full(token)
    if not decoded["payload"]:
        return "token_info: unable to decode JWT payload"

    parts = []
    exp = decoded["time_claims"].get("exp")
    if exp:
        parts.append(f"exp={exp['iso']} ({exp['status']})")
    aud = decoded["identity"].get("aud")
    if aud:
        parts.append(f"aud={aud}")
    if decoded["scopes"]:
        parts.append(f"scp={decoded['scopes']}")
    return "token_info: " + ", ".join(parts)


def token_scopes(token: str) -> List[str]:
    return decode_token_full(token)["scopes"]


def select_credential(data: Dict[str, Any], auth_type: str) -> Tuple[str, str]:
    if auth_type == "api-key":
        return "api-key", load_api_key(data)
    if auth_type == "chatgpt":
        return "chatgpt", load_chatgpt_access_token(data)

    try:
        return "api-key", load_api_key(data)
    except SystemExit:
        return "chatgpt", load_chatgpt_access_token(data)


def build_opener(proxy: Optional[str]) -> urllib.request.OpenerDirector:
    if not proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )


def refresh_chatgpt_tokens(
    refresh_token: str, proxy: Optional[str], timeout: int, scope: str
) -> Dict[str, Any]:
    payload = {
        "client_id": CODEX_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": scope,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        REFRESH_ENDPOINT,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    opener = build_opener(proxy)
    try:
        with opener.open(req, timeout=timeout) as resp:
            refreshed = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise SystemExit(
                "Refresh token is expired or revoked. Re-authenticate with Codex. "
                f"HTTP 401: {detail}"
            ) from exc
        raise SystemExit(f"Refresh HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Refresh network error: {exc}") from exc

    access_token = refreshed.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise SystemExit("Refresh response did not include access_token")
    return refreshed


def merge_refreshed_auth(data: Dict[str, Any], refreshed: Dict[str, Any]) -> Dict[str, Any]:
    current_tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    tokens = dict(current_tokens)
    tokens["access_token"] = refreshed["access_token"]
    if isinstance(refreshed.get("refresh_token"), str) and refreshed["refresh_token"]:
        tokens["refresh_token"] = refreshed["refresh_token"]
    if isinstance(refreshed.get("id_token"), str) and refreshed["id_token"]:
        tokens["id_token"] = refreshed["id_token"]

    merged = dict(data)
    merged["tokens"] = tokens
    merged["last_refresh"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return merged


def write_auth_atomic(auth_path: Path, data: Dict[str, Any]) -> None:
    tmp_path = auth_path.with_name(f"{auth_path.name}.tmp.{os.getpid()}.{int(time.time())}")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(str(tmp_path), 0o600)
    os.replace(str(tmp_path), str(auth_path))


def request_llm(
    token: str,
    model: str,
    prompt: str,
    proxy: Optional[str],
    timeout: int,
    account_id: Optional[str] = None,
) -> Dict[str, Any]:
    payload = {
        "model": model,
        "input": prompt,
        "max_output_tokens": 32,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=body,
        headers=headers,
        method="POST",
    )
    opener = build_opener(proxy)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        info = parse_auth_error(detail)
        if "missing_scopes" in info:
            print(
                "error=insufficient_scope; "
                f"required={','.join(info['missing_scopes'])}; "
                f"http={exc.code}",
                file=sys.stderr,
            )
        else:
            print(f"error=http_{exc.code}; message={info.get('message', detail)}", file=sys.stderr)
        raise SystemExit(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Network error: {exc}") from exc


def extract_text(response: Dict[str, Any]) -> str:
    text = response.get("output_text")
    if isinstance(text, str):
        return text

    chunks: List[str] = []
    for item in response.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "".join(chunks)


_MISSING_SCOPES_RE = re.compile(r"Missing scopes:\s*([^\.\n]+)")


def parse_auth_error(detail: str) -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    try:
        payload = json.loads(detail)
    except (ValueError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = err.get("message")
            if isinstance(message, str):
                info["message"] = message
            code = err.get("code")
            if isinstance(code, str):
                info["code"] = code
        elif isinstance(err, str):
            info["message"] = err
    if "message" not in info:
        info["message"] = detail.strip()
    match = _MISSING_SCOPES_RE.search(info.get("message", ""))
    if match:
        scopes = [s.strip() for s in match.group(1).split(",") if s.strip()]
        info["missing_scopes"] = scopes
    return info


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test an OpenAI LLM request using credentials from Codex auth.json."
    )
    parser.add_argument("--auth", default="auth.json", help="path to auth.json")
    parser.add_argument(
        "--auth-type",
        choices=("auto", "api-key", "chatgpt"),
        default="auto",
        help="credential to use; chatgpt uses tokens.access_token unless --refresh is set",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="model name")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="prompt to send")
    parser.add_argument(
        "--proxy",
        default=os.getenv("TEST_PROXY"),
        help="HTTP(S) proxy, e.g. http://127.0.0.1:7890; defaults to TEST_PROXY",
    )
    parser.add_argument("--timeout", type=int, default=30, help="request timeout seconds")
    parser.add_argument("--raw", action="store_true", help="print full JSON response")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="refresh ChatGPT access_token from tokens.refresh_token before testing",
    )
    parser.add_argument(
        "--refresh-scope",
        default=DEFAULT_REFRESH_SCOPE,
        help="OAuth scope requested during --refresh",
    )
    parser.add_argument(
        "--write-back",
        action="store_true",
        help="with --refresh, atomically write refreshed tokens back to auth.json",
    )
    parser.add_argument(
        "--show-token-info",
        action="store_true",
        help="for ChatGPT auth, print decoded JWT exp/aud/scopes without printing the token",
    )
    parser.add_argument(
        "--no-account-id",
        dest="include_account_id",
        action="store_false",
        help="do not send the chatgpt-account-id header for ChatGPT auth",
    )
    parser.add_argument(
        "--check-scopes",
        action="store_true",
        help="decode the JWT and print scopes only; do not call /v1/responses",
    )
    parser.add_argument(
        "--decode-token",
        choices=("access", "id", "refresh", "all"),
        help="decode JWT(s) from auth.json without sending any network request; "
        "all prints every JWT plus a refresh_token summary",
    )
    parser.set_defaults(include_account_id=True)
    args = parser.parse_args()

    auth_path = Path(args.auth).expanduser()
    data = load_auth(auth_path)

    if args.write_back and not args.refresh:
        raise SystemExit("--write-back requires --refresh")

    account_id = load_chatgpt_account_id(data) if args.include_account_id else None

    if args.refresh:
        if args.auth_type == "api-key":
            raise SystemExit("--refresh only applies to --auth-type chatgpt or auto")
        refresh_token = load_chatgpt_refresh_token(data)
        refreshed = refresh_chatgpt_tokens(
            refresh_token, args.proxy, args.timeout, args.refresh_scope
        )
        token = refreshed["access_token"]
        credential_type = "chatgpt"
        if args.write_back:
            data = merge_refreshed_auth(data, refreshed)
            write_auth_atomic(auth_path, data)
            print(f"refreshed_tokens_written={auth_path}", file=sys.stderr)
        else:
            print("refreshed_tokens_not_written=use --write-back to update auth.json", file=sys.stderr)
    else:
        credential_type, token = select_credential(data, args.auth_type)

    if args.show_token_info and credential_type == "chatgpt":
        print(describe_token(token), file=sys.stderr)
    if credential_type == "chatgpt" and RESPONSES_WRITE_SCOPE not in token_scopes(token):
        print(
            f"warning=token_missing_scope:{RESPONSES_WRITE_SCOPE}; "
            "the /v1/responses test may fail with insufficient permissions",
            file=sys.stderr,
        )

    if args.check_scopes:
        scopes = token_scopes(token) if credential_type == "chatgpt" else []
        print(json.dumps({
            "credential_type": credential_type,
            "scopes": scopes,
            "has_api_responses_write": RESPONSES_WRITE_SCOPE in scopes,
        }, ensure_ascii=False, indent=2))
        return 0

    if args.decode_token:
        original_tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        refreshed_dict: Dict[str, Any] = refreshed if (args.refresh and isinstance(refreshed, dict)) else {}
        output: Dict[str, Any] = {"credential_type": credential_type, "tokens": {}}
        targets = ("access", "id", "refresh") if args.decode_token == "all" else (args.decode_token,)

        def pick(which: str) -> Optional[str]:
            if which == "access":
                if refreshed_dict.get("access_token"):
                    return refreshed_dict["access_token"]
                value = original_tokens.get("access_token")
                return value if isinstance(value, str) else None
            if which == "id":
                value = original_tokens.get("id_token")
                return value if isinstance(value, str) else None
            if which == "refresh":
                value = original_tokens.get("refresh_token")
                return value if isinstance(value, str) else None
            return None

        for which in targets:
            if which == "refresh":
                rt = pick("refresh")
                if not rt:
                    output["tokens"]["refresh_token"] = {"available": False}
                else:
                    output["tokens"]["refresh_token"] = {
                        "available": True,
                        "kind": "opaque",
                        "prefix": rt[:6],
                        "length": len(rt),
                    }
                continue
            raw = pick(which)
            if not raw:
                output["tokens"][which] = {"available": False}
                continue
            decoded = decode_token_full(raw)
            decoded["available"] = True
            output["tokens"][which] = decoded
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    response = request_llm(
        token, args.model, args.prompt, args.proxy, args.timeout, account_id
    )

    if args.raw:
        print(json.dumps(response, ensure_ascii=False, indent=2))
    else:
        print(f"auth_type={credential_type}")
        print(extract_text(response) or json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
