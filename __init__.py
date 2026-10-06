"""ntfy-notify: push notification at the end of every Hermes WebUI turn.

Why a plugin: Hermes' bundled ntfy platform plugin only answers messages received on an ntfy
topic. WebUI turns go through the gateway API server (platform ``api_server``) and never touch
it. ``post_llm_call`` does fire for those turns (once per turn), without serialising the whole
history the way a shell hook would.

Behaviour:
- ``post_llm_call`` (platform ``api_server`` only: no cron, sub-agent or messaging platform):
  notification with a short preview of the reply; tapping it opens the session in the Hermes
  Android app (``hermes://session/<id>``), with an HTTPS fallback button to the WebUI.
- ``on_session_end`` without a final answer and without interruption: failure notification.
- An interrupted turn (``/stop``) never notifies.
- Everything goes through ntfy's JSON API (clean UTF-8), in a daemon thread with a short
  timeout: an unreachable ntfy never slows down the end of a turn. Errors are logged, then swallowed.

Configuration (profile ``.env``, read with ``get_scoped_secret``: secrets are PER profile):
  NTFY_NOTIFY_URL          ntfy server URL reachable from the container (e.g. http://ntfy:80)
  NTFY_NOTIFY_TOKEN        token of an ntfy user allowed to publish to the topic
  NTFY_NOTIFY_TOPIC        topic (default hermes-turns)
  NTFY_NOTIFY_WEBUI_URL    HTTPS base of the WebUI for the fallback link (e.g. https://hermes.example.com)
  NTFY_NOTIFY_MIN_SECONDS  only notify turns lasting at least N seconds (default 0 = every turn)

"Log only" mode: a file named ``DRYRUN`` next to this module is enough, without a restart,
to log notifications instead of sending them.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_WEBUI_PLATFORM = "api_server"
_PREVIEW_CHARS = 250
_TITLE_CHARS = 60
_TIMEOUT_S = 4.0
_DRYRUN_FLAG = Path(__file__).with_name("DRYRUN")
_MAX_TRACKED = 256

# Workspace tag the WebUI prefixes to the user message: `[Workspace::v1: /path]`.
_WORKSPACE_TAG = re.compile(r"^\s*(?:\[[^\]\n]{0,300}::v\d+:[^\]\n]*\]\s*)+")

_lock = threading.Lock()
_turn_started: Dict[str, float] = {}
_turn_notified: Dict[str, float] = {}


def _setting(name: str, default: str = "") -> str:
    """Setting of the current profile (scoped secrets), never another profile's."""
    try:
        from gateway.platforms._shared import get_scoped_secret
        value = get_scoped_secret(name, default)
    except Exception:
        import os
        value = os.environ.get(name, default)
    return (value or default).strip() if isinstance(value, str) else default


def _config() -> Optional[Dict[str, Any]]:
    base = _setting("NTFY_NOTIFY_URL").rstrip("/")
    topic = _setting("NTFY_NOTIFY_TOPIC", "hermes-turns")
    if not base or not topic:
        return None
    try:
        min_seconds = float(_setting("NTFY_NOTIFY_MIN_SECONDS", "0") or 0)
    except ValueError:
        min_seconds = 0.0
    return {
        "base": base,
        "topic": topic,
        "token": _setting("NTFY_NOTIFY_TOKEN"),
        "webui": _setting("NTFY_NOTIFY_WEBUI_URL").rstrip("/"),
        "min_seconds": min_seconds,
    }


def _profile_label() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = get_active_profile_name() or ""
    except Exception:
        name = ""
    return "Hermes" if name in ("", "default") else name.capitalize()


def _plain(text: Any, limit: int) -> str:
    """Short plain text: markdown stripped, whitespace normalised, cut on a word boundary."""
    s = text if isinstance(text, str) else str(text or "")
    s = re.sub(r"```.*?```", " [code] ", s, flags=re.S)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"(?m)^\s{0,3}(#{1,6}|>|[-*+])\s+", "", s)
    s = re.sub(r"[*`]|__", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        cut = s[:limit]
        s = (cut.rsplit(" ", 1)[0] if " " in cut[limit // 2:] else cut).rstrip(" ,;:.") + "…"
    return s


def _subject(user_message: Any) -> str:
    raw = user_message if isinstance(user_message, str) else ""
    return _plain(_WORKSPACE_TAG.sub("", raw), _TITLE_CHARS)


def _session_links(cfg: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    extra: Dict[str, Any] = {"click": f"hermes://session/{session_id}"}
    if cfg["webui"]:
        extra["actions"] = [{"action": "view", "label": "Open (web)", "url": f"{cfg['webui']}/session/{session_id}"}]
    return extra


def _post(base: str, token: str, payload: Dict[str, Any]) -> None:
    """ntfy JSON publish (POST on the root). Runs in a daemon thread: never raises."""
    try:
        request = urllib.request.Request(
            base + "/", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "hermes-ntfy-notify/1.0",
                     **({"Authorization": f"Bearer {token}"} if token else {})})
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            if response.status >= 300:
                logger.warning("ntfy-notify: HTTP %s", response.status)
    except Exception as exc:
        logger.warning("ntfy-notify: send failed (%s: %s)", type(exc).__name__, exc)


def _dispatch(cfg: Dict[str, Any], payload: Dict[str, Any], label: str) -> None:
    payload = {"topic": cfg["topic"], **payload}
    if _DRYRUN_FLAG.exists():
        logger.info("ntfy-notify DRYRUN [%s] %s", label, json.dumps(payload, ensure_ascii=False))
        return
    threading.Thread(target=_post, args=(cfg["base"], cfg["token"], payload), name="ntfy-notify", daemon=True).start()


def _remember(store: Dict[str, float], key: str) -> None:
    with _lock:
        store[key] = time.monotonic()
        if len(store) > _MAX_TRACKED:
            for stale in sorted(store, key=store.get)[: len(store) - _MAX_TRACKED]:
                store.pop(stale, None)


def _on_pre_llm_call(session_id: str = "", platform: str = "", **_: Any) -> None:
    """Turn start: feeds the duration threshold and spots turns without a final answer."""
    if platform != _WEBUI_PLATFORM or not session_id:
        return None
    _remember(_turn_started, session_id)
    with _lock:
        _turn_notified.pop(session_id, None)
    return None


def _on_post_llm_call(session_id: str = "", assistant_response: Any = None, user_message: Any = None,
                      platform: str = "", **kwargs: Any) -> None:
    if platform != _WEBUI_PLATFORM or not session_id:
        return
    try:
        cfg = _config()
        if cfg is None or not (assistant_response or "").strip():
            return
        _remember(_turn_notified, session_id)
        with _lock:
            started = _turn_started.pop(session_id, None)
        if cfg["min_seconds"] and started is not None and time.monotonic() - started < cfg["min_seconds"]:
            return
        subject = _subject(user_message)
        title = f"{_profile_label()} · {subject}" if subject else f"{_profile_label()} replied"
        _dispatch(cfg, {"title": title, "message": _plain(assistant_response, _PREVIEW_CHARS), "tags": ["robot"],
                        "priority": 3, **_session_links(cfg, session_id)}, "turn")
    except Exception:
        logger.warning("ntfy-notify: post_llm_call ignored", exc_info=True)


def _on_session_end(session_id: str = "", completed: bool = True, interrupted: bool = False,
                    platform: str = "", **kwargs: Any) -> None:
    """Run ended without a final answer or interruption: the turn failed, report it once."""
    if platform != _WEBUI_PLATFORM or not session_id:
        return
    try:
        with _lock:
            already = _turn_notified.pop(session_id, None) is not None
            _turn_started.pop(session_id, None)
        if completed or interrupted or already:
            return
        cfg = _config()
        if cfg is None:
            return
        _dispatch(cfg, {"title": f"{_profile_label()} · turn failed", "tags": ["warning"], "priority": 4,
                        "message": "The turn ended without a final answer. Open the session to see why.",
                        **_session_links(cfg, session_id)}, "failure")
    except Exception:
        logger.warning("ntfy-notify: on_session_end ignored", exc_info=True)


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
