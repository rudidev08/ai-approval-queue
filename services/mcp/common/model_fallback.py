"""model_fallback — the model call for every model call our own code makes:
release_news, finance_jobs, chat_scan, rules_suggest, the hermes audit,
and the voice page's dev mode. One decision, in one place.

  chain(): Claude Opus at high effort through the claude command-line tool,
  on the Claude subscription — the one supported way to use that subscription
  outside Claude Code itself. The assistant's scripts call it. The command takes no
  token cap and no reasoning setting, so a caller's max_tokens is ignored.
  When Claude fails, one try on the backup the caller names:
    LOCAL: the main model's backup, the local model on this Mac.
    OPENROUTER: DeepSeek V4 Flash, only at providers that keep no copy of
    the prompt and take every field of the request, the JSON schema
    included. For callers the local model is too weak for.
  The backup gets the caller's messages, JSON schema and max_tokens, not its
  reasoning setting: on OpenRouter that setting would narrow the providers.
  chat(): a caller's own model, through its
  chat/completions endpoint, one try.
A failure is raised as it is; when both chain() steps fail, with both errors.

Both answer in the chat-completions shape, so a caller reads
choices[0].message.content either way. stdlib only.
"""

import http.client
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request

import hermes_env

CLAUDE = os.path.expanduser("~/.local/bin/claude")
CLAUDE_MODEL = "opus"
CLAUDE_EFFORT = "high"
LOCAL_MODEL = "qwen3.6-35b-a3b"
OPENROUTER_MODEL = "~deepseek/deepseek-v4-flash-latest"
# (chat/completions base, key name in ~/.hermes/.env, model, extra body fields)
LOCAL = ("http://127.0.0.1:1234/v1", "LM_API_TOKEN", LOCAL_MODEL, {})
OPENROUTER = ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", OPENROUTER_MODEL,
              {"provider": {"zdr": True, "require_parameters": True}})


class ProviderError(RuntimeError):
    """A provider's failed answer. code is the HTTP status, or None when the
    provider could not be reached (connection failure, timeout, a failed
    command)."""

    def __init__(self, where, code, detail):
        super().__init__(f"{where}: " + (f"HTTP {code}: {detail}" if code else detail))
        self.code = code


def api_key(name):
    """The named variable out of ~/.hermes/.env (the launchd environment
    carries none of it)."""
    try:
        return hermes_env.read()[name]
    except (OSError, KeyError):
        raise RuntimeError(f"{name} missing in {hermes_env.PATH}") from None


def _post(url, key_env, body, timeout):
    req = urllib.request.Request(
        url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key(key_env)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read())
            if isinstance(detail, dict):
                detail = detail.get("error", "")
            if isinstance(detail, dict):
                detail = detail.get("message", "")
            detail = str(detail or "")
        except (ValueError, OSError):
            detail = ""
        raise ProviderError(url, e.code, detail[:600] or e.reason) from None
    except urllib.error.URLError as e:
        raise ProviderError(url, None, str(e.reason)) from None
    except (OSError, http.client.HTTPException) as e:
        # a timeout, or the connection dropped while the answer was being read
        raise ProviderError(url, None, str(e) or type(e).__name__) from None
    try:
        return json.loads(raw)
    except ValueError:
        raise ProviderError(url, None, "the answer is not JSON") from None


def _claude(body, timeout):
    """The request as one claude print run: system messages become the
    system prompt, the rest the prompt (on stdin — a review prompt can be
    longer than an argument list allows), a JSON schema is passed on, no
    tools, nothing saved. --safe-mode keeps the user's own CLAUDE.md, hooks
    and plugins out of the run; an empty folder keeps a project's out."""
    system = "\n\n".join(m["content"] for m in body["messages"] if m["role"] == "system")
    turns = [m for m in body["messages"] if m["role"] != "system"]
    prompt = (turns[0]["content"] if len(turns) == 1
              else "\n\n".join(f"{m['role']}: {m['content']}" for m in turns))
    argv = [CLAUDE, "-p", "--safe-mode", "--tools", "", "--no-session-persistence",
            "--output-format", "json", "--model", CLAUDE_MODEL, "--effort", CLAUDE_EFFORT]
    if system:
        argv += ["--system-prompt", system]
    schema = (body.get("response_format") or {}).get("json_schema", {}).get("schema")
    if schema:
        argv += ["--json-schema", json.dumps(schema)]
    where = f"claude {CLAUDE_MODEL}"
    cwd = tempfile.mkdtemp(prefix="model-fallback-")
    try:
        p = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise ProviderError(where, None, f"timed out after {timeout}s") from None
    except OSError as e:
        raise ProviderError(where, None, str(e)) from None
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        out = {}
    if p.returncode != 0 or out.get("is_error") or "result" not in out:
        detail = out.get("result") or (p.stderr or p.stdout or "no output").strip()[-300:]
        raise ProviderError(where, None, f"exit {p.returncode}: {detail}")
    content = out["result"]
    if out.get("structured_output") is not None:
        content = json.dumps(out["structured_output"])
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


def chat(url, key_env, body, timeout):
    """POST body to url's chat/completions; returns the parsed answer."""
    return _post(url, key_env, body, timeout)


def chain(body, timeout, backup):
    """Claude, then backup (LOCAL or OPENROUTER) when Claude fails: for
    the assistant's scripts."""
    try:
        return _claude(body, timeout)
    except ProviderError as first:
        url, key_env, model, extra = backup
        keep = {k: body[k] for k in ("messages", "response_format", "max_tokens") if k in body}
        try:
            return _post(url, key_env, {"model": model, **keep, **extra}, timeout)
        except RuntimeError as second:  # a ProviderError, or the backup's key missing
            raise RuntimeError(f"{first}; then {second}") from None
