"""Experimental Windows-only OpenAI-compatible gateway for local Ollama.

Prototype for offline study, not a production-safe autonomous-agent fallback.
It offers local-only HTTP binding, conservative *heuristic* context budgeting,
lossy history handoff and allowlist-based tool-call filtering.
All tools are disabled by default. See docs/KNOWN_LIMITATIONS.md.
Do not expose on a network or enable automatic agent failover without review.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_PORT", "11599"))
MAX_BODY_BYTES = int(os.environ.get("BRIDGE_MAX_BODY_BYTES", str(8 * 1024 * 1024)))
UPSTREAM = os.environ.get("BRIDGE_UPSTREAM", "http://127.0.0.1:11434").rstrip("/")
NUM_CTX = int(os.environ.get("BRIDGE_NUM_CTX", "16384"))
RESERVE = int(os.environ.get("BRIDGE_RESERVE_TOKENS", "3000"))
MAX_PROMPT = int(os.environ.get("BRIDGE_MAX_PROMPT_TOKENS", "0")) or (NUM_CTX - RESERVE)

# ─────────────────────────────────────────────────────────────────────────────
# SANDBOX PATCH v1: полный бюджет запроса + строгая политика инструментов.
# Всё, что ниже, работает только в изолированной копии; рабочий мост не изменён.
# ─────────────────────────────────────────────────────────────────────────────
GEN_RESERVE_DEFAULT = int(os.environ.get("BRIDGE_GEN_RESERVE", "4096"))
GEN_RESERVE_MIN = int(os.environ.get("BRIDGE_GEN_RESERVE_MIN", "512"))
TOOLS_CHARS_PER_TOKEN = float(os.environ.get("BRIDGE_TOOLS_CHARS_PER_TOKEN", "1.85"))
MSG_OVERHEAD_TOKENS = int(os.environ.get("BRIDGE_MSG_OVERHEAD", "4"))
TEMPLATE_OVERHEAD_TOKENS = int(os.environ.get("BRIDGE_TEMPLATE_OVERHEAD", "48"))
TOTAL_MARGIN_TOKENS = int(os.environ.get("BRIDGE_TOTAL_MARGIN", "512"))
MIN_HISTORY_TOKENS = int(os.environ.get("BRIDGE_MIN_HISTORY_TOKENS", "1024"))
MAX_BLOCKED_SAME_REQUEST = int(os.environ.get("BRIDGE_MAX_BLOCKED_SAME_REQUEST", "2"))
# Уникальный служебный префикс ответа-блокировки (B6): по нему распознаётся наш след
# в истории, а не бытовая фраза «резервного режима».
BLOCK_MARKER_PREFIX = "[bridge/fallback]"
REQUEST_ID_PREFIX = "brg"
# Политика резервного режима: read-only, двумя уровнями.
# Локальное чтение тоже опасно: результат может позднее попасть в облачную историю.
ALLOWED_LOCAL_TOOLS = frozenset(
    "read_file,search_files,skills_list,skill_view,tool_search,tool_describe".split(",")
)
ALLOWED_NETWORK_TOOLS = frozenset(
    "web_search,web_extract,browser_snapshot,browser_console,browser_get_images".split(",")
)
# Сетевые инструменты по умолчанию ВЫКЛЮЧЕНЫ: read-only не значит «можно передавать данные наружу».
ALLOW_NETWORK = os.environ.get("BRIDGE_ALLOW_NETWORK", "0").strip().lower() in ("1", "true", "yes")
_ALLOW_ENV_RAW = os.environ.get("BRIDGE_ALLOWED_TOOLS")
# Потолок политики: сетевые схемы попадают в него только при явном BRIDGE_ALLOW_NETWORK=1.
_ALLOWED_CEILING = ALLOWED_LOCAL_TOOLS | (ALLOWED_NETWORK_TOOLS if ALLOW_NETWORK else frozenset())
if _ALLOW_ENV_RAW is None:
    # Публичный исследовательский вариант: по умолчанию НОЛЬ инструментов.
    # Определённый вручную список может лишь сузить встроенный потолок.
    ALLOWED_TOOLS = frozenset()
else:
    # Явный список может только сузить набор. Пустой результат — осознанный запрет
    # всех инструментов: подстановки локальных по умолчанию нет.
    _requested = frozenset(t.strip() for t in _ALLOW_ENV_RAW.split(",") if t.strip())
    ALLOWED_TOOLS = frozenset(t for t in _requested if t in _ALLOWED_CEILING)
ALLOWED_TOOLS_EMPTY_EXPLICIT = _ALLOW_ENV_RAW is not None and not ALLOWED_TOOLS
# Оценка схем через len(JSON)/1.85 — гипотеза, поэтому бюджет берёт её с запасом.
TOOLS_SAFETY_FACTOR = float(os.environ.get("BRIDGE_TOOLS_SAFETY_FACTOR", "1.25"))
_MODE_RAW = os.environ.get("BRIDGE_TOOL_POLICY", "enforce").strip().lower()
# Неизвестный режим не должен ослаблять политику: всё, кроме явного observe, — enforce.
TOOL_POLICY_MODE = "observe" if _MODE_RAW == "observe" else "enforce"
POLICY_MODE_FAILSAFE = _MODE_RAW not in ("enforce", "observe")
BLOCK_TTL_SECONDS = int(os.environ.get("BRIDGE_BLOCK_TTL", "600"))

_BLOCK_SEEN: dict[str, list[float]] = {}
_BLOCK_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()
_UNSAFE_LOCK = threading.Lock()


def estimate_tools_tokens(tools: list[dict[str, Any]]) -> int:
    """Гипотеза, НЕ гарантия: len(json)/1.85 + 4 токена на инструмент.

    Проверяется на сохранённых запросах; реальную токенизацию Qwen даёт только
    сервер Ollama (prompt_eval_count) или tokenizer.json.
    """
    if not tools:
        return 0
    blob = json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
    return int(len(blob) / TOOLS_CHARS_PER_TOKEN) + 1 + MSG_OVERHEAD_TOKENS * len(tools)


def filter_tools(tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Оставляет в запросе к Qwen только имена из ALLOWED_TOOLS."""
    if TOOL_POLICY_MODE != "enforce" or not tools:
        return list(tools or []), []
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for spec in tools:
        name = str(((spec or {}).get("function") or {}).get("name") or "")
        if name in ALLOWED_TOOLS:
            kept.append(spec)
        else:
            dropped.append(name or "<без имени>")
    return kept, dropped


def full_request_tokens(messages: list[dict[str, Any]], tools_tokens: int) -> int:
    """Полная оценка: сообщения + каркас сообщений + chat-шаблон + схемы инструментов."""
    return (conversation_tokens(messages) + MSG_OVERHEAD_TOKENS * (len(messages) + 1)
            + TEMPLATE_OVERHEAD_TOKENS + tools_tokens)


MAX_OUTPUT_TOKENS = int(os.environ.get("BRIDGE_MAX_OUTPUT_TOKENS", "32768"))


def validated_max_tokens(payload: dict[str, Any]) -> tuple[int, str | None]:
    """Возвращает (значение, ошибка). Одно и то же число идёт в бюджет и в num_predict.

    None → значение по умолчанию GEN_RESERVE_DEFAULT. Строки проверяются на целое число,
    дробные float и bool отклоняются, 0 и отрицательные — отклоняются, слишком большие —
    отклоняются до обращения к Ollama. Значение не подгоняется молча.
    """
    raw = payload.get("max_tokens")
    if raw is None:
        raw = payload.get("max_completion_tokens")
    if raw is None:
        return GEN_RESERVE_DEFAULT, None
    if isinstance(raw, bool):
        return 0, "логическое значение недопустимо"
    if isinstance(raw, str):
        text = raw.strip()
        if not text or not text.lstrip("+").isdigit():
            return 0, "строка должна содержать целое число"
        value = int(text)
    elif isinstance(raw, float):
        if not raw.is_integer():
            return 0, "дробное значение недопустимо"
        value = int(raw)
    elif isinstance(raw, int):
        value = raw
    else:
        return 0, "ожидается целое число"
    if value <= 0:
        return 0, "значение должно быть больше нуля"
    if value > MAX_OUTPUT_TOKENS:
        return 0, f"значение больше предела {MAX_OUTPUT_TOKENS}"
    if value >= NUM_CTX - MIN_HISTORY_TOKENS:
        return 0, "значение оставляет меньше минимального бюджета истории"
    return value, None


_UNSAFE_LOGGED = False


def note_unsafe_mode() -> None:
    """observe и сетевой доступ небезопасны для резервной модели: пишем это в лог."""
    global _UNSAFE_LOGGED
    if TOOL_POLICY_MODE == "enforce" and not ALLOW_NETWORK:
        return
    with _UNSAFE_LOCK:
        if _UNSAFE_LOGGED:
            return
        _UNSAFE_LOGGED = True
    log_event("unsafe_mode", {
        "policy_mode": TOOL_POLICY_MODE,
        "network_tools": ALLOW_NETWORK,
        "note": "unsafe diagnostic configuration; do not use for agent automation",
    })


def non_compressible_tokens(messages: list[dict[str, Any]], tools_tokens: int,
                            gen_reserve: int) -> dict[str, int]:
    """Системные сообщения + последний пользовательский ввод + схемы: это не сжать."""
    system_tokens = 0
    last_user_tokens = 0
    for m in messages or []:
        role = (m or {}).get("role")
        content = (m or {}).get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False) \
            if content is not None else ""
        if role == "system":
            system_tokens += conservative_text_tokens(text)
        elif role == "user":
            last_user_tokens = conservative_text_tokens(text) or last_user_tokens
    system_tokens = int(system_tokens * MESSAGE_SAFETY_FACTOR)
    last_user_tokens = int(last_user_tokens * MESSAGE_SAFETY_FACTOR)
    overhead = MSG_OVERHEAD_TOKENS * 2
    total = (system_tokens + last_user_tokens + tools_tokens + TEMPLATE_OVERHEAD_TOKENS
             + gen_reserve + overhead + TOTAL_MARGIN_TOKENS)
    return {"system_tokens": system_tokens, "last_user_tokens": last_user_tokens,
            "tools_tokens": tools_tokens, "output_reserve": gen_reserve,
            "overhead": overhead, "margin": TOTAL_MARGIN_TOKENS, "total": total,
            "num_ctx": NUM_CTX}


def repair_tool_pairs(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Сжатие не должно оставлять tool_call без результата (и наоборот).

    Осиротевший вызов Hermes воспринимает как незавершённое действие, поэтому
    такие пары удаляются целиком; system не трогается.
    """
    call_ids: set[str] = set()
    result_ids: set[str] = set()
    for m in messages:
        for tc in (m or {}).get("tool_calls") or []:
            cid = (tc or {}).get("id") or ((tc or {}).get("function") or {}).get("id")
            if cid:
                call_ids.add(str(cid))
        if (m or {}).get("role") == "tool":
            rid = (m or {}).get("tool_call_id")
            if rid:
                result_ids.add(str(rid))
    orphan_calls = call_ids - result_ids
    orphan_results = result_ids - call_ids
    if not orphan_calls and not orphan_results:
        return messages, {"tool_pairs_dropped_calls": 0, "tool_pairs_dropped_results": 0}
    kept: list[dict[str, Any]] = []
    for m in messages:
        role = (m or {}).get("role")
        if role == "tool" and str((m or {}).get("tool_call_id") or "") in orphan_results:
            continue
        calls = (m or {}).get("tool_calls") or []
        if calls:
            rest = [tc for tc in calls
                    if str((tc or {}).get("id") or ((tc or {}).get("function") or {}).get("id") or "") not in orphan_calls]
            if not rest and role == "assistant":
                text = _content_text((m or {}).get("content"))
                if not text.strip():
                    continue  # пустой assistant-call без результата бессмысленен
                m = {k: v for k, v in m.items() if k != "tool_calls"}
            elif len(rest) != len(calls):
                m = dict(m)
                m["tool_calls"] = rest
        kept.append(m)
    return kept, {"tool_pairs_dropped_calls": len(orphan_calls),
                  "tool_pairs_dropped_results": len(orphan_results)}


def plan_budget(payload: dict[str, Any]) -> dict[str, Any]:
    """Решает, влезает ли запрос целиком, и отдаёт бюджет истории под condense."""
    messages = payload.get("messages") or []
    raw_tools = payload.get("tools") or []
    tools_kept, dropped = filter_tools(raw_tools)
    tools_tokens = int(estimate_tools_tokens(tools_kept) * TOOLS_SAFETY_FACTOR)
    # /1.85 — гипотеза; запас 1.25 страхует недооценку схем инструментов.
    raw_mt = payload.get("max_tokens")
    if raw_mt is None:
        raw_mt = payload.get("max_completion_tokens")
    gen_reserve, mt_error = validated_max_tokens(payload)
    msg_tokens_in = messages_tokens_conservative(messages)
    raw_budget = (NUM_CTX - gen_reserve - tools_tokens - TEMPLATE_OVERHEAD_TOKENS
                  - TOTAL_MARGIN_TOKENS - MSG_OVERHEAD_TOKENS * (KEEP_RECENT + 1))
    # condense меряет историю оптимистичной оценкой, поэтому режем бюджет заранее —
    # по измеренному отношению консервативной оценки к оптимистичной (не меньше 1.30).
    _opt_in = max(1, conversation_tokens(messages))
    msg_ratio = max(MESSAGE_SAFETY_FACTOR, min(3.0, msg_tokens_in / _opt_in))
    history_budget = int(max(raw_budget, 0) / msg_ratio)
    log = {
        "num_ctx": NUM_CTX,
        "gen_reserve": gen_reserve,
        "tools_kept": len(tools_kept),
        "tools_dropped": len(dropped),
        "tools_dropped_names": dropped[:12],
        "tools_tokens_est": tools_tokens,
        "history_budget": history_budget,
        "msg_count_in": len(messages),
        "max_tokens_in": raw_mt,
        "tokens_messages_in": msg_tokens_in,
        "tokens_tools": tools_tokens,
        "tokens_template": TEMPLATE_OVERHEAD_TOKENS,
        "tokens_output_reserve": gen_reserve,
        "tokens_margin": TOTAL_MARGIN_TOKENS,
        "message_safety_factor": MESSAGE_SAFETY_FACTOR,
        "history_budget_raw": raw_budget,
        "policy_mode": TOOL_POLICY_MODE,
        "policy_mode_failsafe": POLICY_MODE_FAILSAFE,
        "native": NATIVE,
        "native_forced": NATIVE_FORCED_BY_POLICY,
    }
    last_user_tokens = 0
    for m in reversed(messages):
        if (m or {}).get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                last_user_tokens = conservative_text_tokens(content)
            elif content is not None:
                last_user_tokens = conservative_text_tokens(json.dumps(content, ensure_ascii=False))
            break
    last_user_tokens = int(last_user_tokens * MESSAGE_SAFETY_FACTOR)
    log["last_user_tokens"] = last_user_tokens
    if mt_error:
        return {"verdict": "invalid_max_tokens", "log": log,
                "error": {"code": "invalid_max_tokens", "message": "некорректный max_tokens: " + mt_error,
                          "received": repr(raw_mt)[:64], "num_ctx": NUM_CTX}}
    body = dict(payload)
    body["tools"] = tools_kept
    body["max_tokens"] = gen_reserve
    if last_user_tokens + gen_reserve + tools_tokens + TEMPLATE_OVERHEAD_TOKENS + TOTAL_MARGIN_TOKENS > NUM_CTX:
        return {"verdict": "last_user_message_too_large", "log": log,
                "error": {"last_user_tokens": last_user_tokens, "generation_reserve": gen_reserve,
                          "tools_tokens": tools_tokens, "num_ctx": NUM_CTX,
                          "message": "последнее сообщение пользователя не помещается целиком; "
                                     "в Ollama не отправлялось (усечение запрещено)"}}
    if gen_reserve >= NUM_CTX - MIN_HISTORY_TOKENS:
        return {"verdict": "generation_reserve_too_large", "log": log,
                "error": {"generation_reserve": gen_reserve, "num_ctx": NUM_CTX,
                          "message": "резерв генерации не оставляет места истории"}}
    if history_budget < MIN_HISTORY_TOKENS:
        return {"verdict": "no_room_for_history", "log": log,
                "error": {"tools_tokens": tools_tokens, "generation_reserve": gen_reserve,
                          "num_ctx": NUM_CTX, "message": "несжимаемые схемы инструментов не оставляют места истории"}}
    log["num_predict"] = gen_reserve
    return {"verdict": "ok", "payload": body, "log": log, "tools_tokens": tools_tokens,
            "num_predict": gen_reserve,
            "gen_reserve": gen_reserve, "history_budget": history_budget, "dropped_tools": dropped}


def tool_call_name(call: dict[str, Any]) -> str:
    return str(((call or {}).get("function") or {}).get("name") or "")


def policy_block_message(names: list[str]) -> str:
    return ("[bridge/fallback] Запрос инструмента(ов) отклонён политикой резервного режима: "
            + ", ".join(sorted(set(names))) + ". В резервном режиме разрешено только чтение: "
            + ", ".join(sorted(ALLOWED_TOOLS))
            + ". Сообщи пользователю, что для этого действия нужна основная модель, и не повторяй вызов.")


def apply_tool_policy(native: dict[str, Any]) -> list[str]:
    """Мутирует нативный ответ: запрещённые tool_calls заменяются текстом."""
    msg = native.get("message") or {}
    calls = msg.get("tool_calls") or []
    if not calls or TOOL_POLICY_MODE != "enforce":
        return []
    blocked = [tool_call_name(c) for c in calls if tool_call_name(c) not in ALLOWED_TOOLS]
    if not blocked:
        return []
    msg["tool_calls"] = []
    text = policy_block_message(blocked)
    msg["content"] = ((msg.get("content") or "").strip() + "\n\n" + text).strip()
    return blocked


def stream_filter_calls(calls: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    if TOOL_POLICY_MODE != "enforce":
        return calls, []
    blocked = [tool_call_name(c) for c in calls if tool_call_name(c) not in ALLOWED_TOOLS]
    if blocked:
        return [], blocked
    return calls, []


def request_fingerprint(payload: dict[str, Any], session_hint: str = "") -> str:
    """Идентичность диалога: сессия + модель + схемы + system + первое сообщение пользователя.

    Побайтовый отпечаток всего запроса менялся после каждой нашей блокировки, поэтому
    анти-повтор не срабатывал на реальный цикл Hermes. Здесь ключ стабилен в рамках
    диалога, а независимые сессии отличаются system/первым сообщением или заголовком
    сессии. Предварительная блокировка всё равно требует следа блокировки в истории.
    """
    msgs = payload.get("messages") or []
    system = ""
    first_user = ""
    for m in msgs:
        role = (m or {}).get("role")
        text = _content_text((m or {}).get("content"))
        if role == "system" and not system:
            system = text
        elif role == "user" and not first_user:
            first_user = text
        if system and first_user:
            break
    canon = {
        "session": session_hint or "",
        "model": payload.get("model") or "",
        "system": system,
        "first_user": first_user,
        "tools": [str(((t or {}).get("function") or {}).get("name") or "")
                  for t in (payload.get("tools") or [])],
    }
    blob = json.dumps(canon, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:32]


BLOCK_SEEN_MAX = int(os.environ.get("BRIDGE_BLOCK_SEEN_MAX", "256"))


def _prune_blocks(now: float) -> None:
    """Удаляет просроченные записи и ограничивает словарь по размеру (без роста памяти)."""
    for key in [k for k, ts in _BLOCK_SEEN.items() if not ts or now - ts[-1] >= BLOCK_TTL_SECONDS]:
        _BLOCK_SEEN.pop(key, None)
    if len(_BLOCK_SEEN) > BLOCK_SEEN_MAX:
        for key, _ts in sorted(_BLOCK_SEEN.items(), key=lambda kv: kv[1][-1])[:len(_BLOCK_SEEN) - BLOCK_SEEN_MAX]:
            _BLOCK_SEEN.pop(key, None)


def blocked_recently(fp: str) -> bool:
    now = time.time()
    with _BLOCK_LOCK:
        _prune_blocks(now)
        hits = [t for t in _BLOCK_SEEN.get(fp, []) if now - t < BLOCK_TTL_SECONDS]
        _BLOCK_SEEN[fp] = hits
        return len(hits) >= MAX_BLOCKED_SAME_REQUEST


def history_has_block_marker(messages: list[dict[str, Any]]) -> bool:
    """След нашей блокировки в истории: значит клиент действительно повторяет цикл,
    а не другая сессия случайно прислала такой же запрос."""
    for m in messages:
        if (m or {}).get("role") != "assistant":
            continue
        text = _content_text((m or {}).get("content"))
        # Только наш точный служебный префикс: обычная фраза «резервного режима»
        # в переписке не должна включать предварительную блокировку.
        if text.lstrip().startswith(BLOCK_MARKER_PREFIX):
            return True
    return False


def note_blocked(fp: str) -> None:
    now = time.time()
    with _BLOCK_LOCK:
        _prune_blocks(now)
        _BLOCK_SEEN.setdefault(fp, []).append(now)
        if len(_BLOCK_SEEN) > BLOCK_SEEN_MAX:
            for key, _ts in sorted(_BLOCK_SEEN.items(), key=lambda kv: kv[1][-1])[:len(_BLOCK_SEEN) - BLOCK_SEEN_MAX]:
                _BLOCK_SEEN.pop(key, None)


def text_only_response(model: str, text: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-bridge-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        # Synthetic local policy response; no upstream model was called.
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
KEEP_RECENT = int(os.environ.get("BRIDGE_KEEP_RECENT", "6"))
TOOL_RESULT_CHARS = int(os.environ.get("BRIDGE_TOOL_RESULT_CHARS", "1200"))
SUMMARY_CHARS = int(os.environ.get("BRIDGE_SUMMARY_CHARS", "4000"))
LOG_DIR = Path(os.environ.get("BRIDGE_LOG_DIR", str(Path(__file__).with_name("logs"))))

MARKER = "[HANDOFF-LOCAL-MODEL]"
THINK = os.environ.get("BRIDGE_THINK", "0") in ("1", "true", "True")
_NATIVE_RAW = os.environ.get("BRIDGE_NATIVE_API", "1").strip().lower()
_NATIVE_SETTING = _NATIVE_RAW not in ("0", "false", "no")
# В enforce-режиме нативный путь обязателен: иначе ответ уходит клиенту без разбора
# tool_calls. Неизвестное значение тоже трактуем как «включено» (fail-closed).
NATIVE = True if TOOL_POLICY_MODE == "enforce" else _NATIVE_SETTING
NATIVE_FORCED_BY_POLICY = bool(NATIVE and not _NATIVE_SETTING)
RETENTION_HOURS = int(os.environ.get("BRIDGE_RETENTION_HOURS", "24"))
# Политика spill-файлов: off (по умолчанию) | encrypted (DPAPI) | plain.
# Полная исходная история на диск по умолчанию НЕ пишется: модель и без неё получает handoff,
# а полный вывод инструментов остаётся в сессии Hermes.
SPILL_MODE = os.environ.get("BRIDGE_SPILL_MODE", "off").strip().lower()
if SPILL_MODE not in {"off", "encrypted", "plain"}:
    SPILL_MODE = "off"
if SPILL_MODE == "plain" and os.environ.get("BRIDGE_SPILL_ALLOW_PLAIN", "0") not in ("1", "true", "True"):
    SPILL_MODE = "encrypted"

_SECRET_PATTERNS = [
    (re.compile(r"(sk-[A-Za-z0-9_\-]{12,})"), "[REDACTED_API_KEY]"),
    (re.compile(r"(ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"(xox[baprs]-[A-Za-z0-9\-]{10,})"), "[REDACTED_SLACK]"),
    (re.compile(r"(AKIA[0-9A-Z]{16})"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[=:]\s*[\"']?([^\s\"',;}]{6,})"),
     r"\1=[REDACTED]"),
    (re.compile(r"(?i)(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]{0,4000}?(-----END [A-Z ]*PRIVATE KEY-----)"),
     "[REDACTED_PRIVATE_KEY]"),
]


def redact(text: str) -> str:
    """Вырезает секреты из того, что уходит в журналы и spill-файлы."""
    if not text:
        return text
    out = text
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        clone = dict(m)
        content = clone.get("content")
        if isinstance(content, str):
            clone["content"] = redact(content)
        elif isinstance(content, list):
            clone["content"] = [redact(p) if isinstance(p, str) else p for p in content]
        if clone.get("tool_calls"):
            clone["tool_calls"] = json.loads(redact(json.dumps(clone["tool_calls"], ensure_ascii=False)))
        out.append(clone)
    return out


def restrict_acl(path: Path) -> None:
    """Ограничивает доступ к файлу текущим пользователем (Windows ACL)."""
    try:
        user = os.environ.get("USERNAME") or ""
        if os.name == "nt" and user:
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                           check=False, capture_output=True, timeout=20)
        else:
            os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001
        pass


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _data_blob(data: bytes) -> tuple[_DATA_BLOB, Any]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def dpapi_encrypt(data: bytes, entropy: bytes = b"hermes-bridge-spill") -> bytes:
    """Шифрует данные ключом текущего пользователя Windows (DPAPI, user scope)."""
    if os.name != "nt":
        raise RuntimeError("DPAPI доступен только в Windows")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    blob_in, _keep_in = _data_blob(data)
    blob_ent, _keep_ent = _data_blob(entropy)
    blob_out = _DATA_BLOB()
    ok = crypt32.CryptProtectData(ctypes.byref(blob_in), "hermes-bridge-spill",
                                  ctypes.byref(blob_ent), None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def dpapi_decrypt(data: bytes, entropy: bytes = b"hermes-bridge-spill") -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI доступен только в Windows")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    blob_in, _keep_in = _data_blob(data)
    blob_ent, _keep_ent = _data_blob(entropy)
    blob_out = _DATA_BLOB()
    ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, ctypes.byref(blob_ent),
                                    None, None, 0x1, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def write_spill(messages: list[dict[str, Any]], stem: str) -> Path | None:
    """Пишет полный исходный промпт по политике SPILL_MODE. Возвращает путь или None."""
    if SPILL_MODE == "off":
        return None
    payload = json.dumps(redact_messages(messages), ensure_ascii=False, indent=1).encode("utf-8")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if SPILL_MODE == "encrypted":
            path = LOG_DIR / f"{stem}.dpapi"
            path.write_bytes(b"BRIDGE-DPAPI-1\n" + dpapi_encrypt(payload))
        else:
            path = LOG_DIR / f"{stem}.json"
            path.write_text(payload.decode("utf-8"), encoding="utf-8")
        restrict_acl(path)
        return path
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"bridge: spill не записан ({SPILL_MODE}): {exc}\n")
        return None


def cleanup_old() -> int:
    """Удаляет журналы и spill-файлы старше политики хранения."""
    if RETENTION_HOURS <= 0 or not LOG_DIR.exists():
        return 0
    cutoff = time.time() - RETENTION_HOURS * 3600
    removed = 0
    for f in LOG_DIR.glob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed



def _args_to_obj(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ollama (qwen3.5) в /api/chat ждёт arguments объектом, OpenAI-клиент — строкой."""
    out = []
    for m in messages:
        calls = m.get("tool_calls")
        if not calls:
            out.append(m)
            continue
        fixed = []
        for tc in calls:
            tc = dict(tc or {})
            fn = dict(tc.get("function") or {})
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    fn["arguments"] = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    # ничего не выдумываем: помечаем аргументы как нераспознанные,
                    # модель увидит исходный текст и сама решит, повторять ли вызов
                    fn["arguments"] = {"__unparsed_arguments__": args}
                    print(json.dumps({"warn": "unparsed_tool_arguments", "len": len(args)}), file=sys.stderr, flush=True)
            elif args is None:
                fn["arguments"] = {}
            tc["function"] = fn
            fixed.append(tc)
        out.append({**m, "tool_calls": fixed})
    return out


def _args_to_str(message: dict[str, Any]) -> dict[str, Any]:
    calls = message.get("tool_calls")
    if not calls:
        return message
    fixed = []
    for tc in calls:
        tc = dict(tc or {})
        fn = dict(tc.get("function") or {})
        args = fn.get("arguments")
        if isinstance(args, (dict, list)):
            fn["arguments"] = json.dumps(args, ensure_ascii=False)
        tc["function"] = fn
        tc.setdefault("type", "function")
        fixed.append(tc)
    return {**message, "tool_calls": fixed}


def to_ollama_chat(payload: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    """OpenAI-совместимый запрос → нативный /api/chat Ollama (там работает think/num_ctx)."""
    opts: dict[str, Any] = {"num_ctx": NUM_CTX}
    if payload.get("temperature") is not None:
        opts["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        opts["top_p"] = payload["top_p"]
    if payload.get("max_tokens"):
        opts["num_predict"] = payload["max_tokens"]
    body: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": _args_to_obj(messages),
        "stream": False,
        "think": THINK,
        "options": {**(payload.get("options") or {}), **opts},
    }
    if payload.get("tools"):
        body["tools"] = payload["tools"]
    return body


def to_openai_response(data: dict[str, Any], model: str) -> dict[str, Any]:
    """Ответ /api/chat → OpenAI-совместимый вид, чтобы клиент Hermes не заметил подмены."""
    msg = data.get("message") or {}
    openai_msg: dict[str, Any] = {"role": msg.get("role", "assistant"), "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        openai_msg["tool_calls"] = _args_to_str({"tool_calls": msg["tool_calls"]})["tool_calls"]
    pt = data.get("prompt_eval_count")
    ct = data.get("eval_count")
    return {
        "id": f"chatcmpl-bridge-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": data.get("model") or model,
        "choices": [{
            "index": 0,
            "message": openai_msg,
            "finish_reason": "tool_calls" if msg.get("tool_calls") else
                             ("length" if data.get("done_reason") == "length" else "stop"),
        }],
        "usage": {
            "prompt_tokens": pt or 0,
            "completion_tokens": ct or 0,
            "total_tokens": (pt or 0) + (ct or 0),
        },
    }



def estimate_tokens(text: str) -> int:
    """Грубая, но устойчивая оценка: кириллица ~1.9 симв./токен, латиница ~3.8."""
    if not text:
        return 0
    cyr = sum(1 for ch in text if "\u0400" <= ch <= "\u04ff")
    other = len(text) - cyr
    return int(cyr / 1.9 + other / 3.8) + 8


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif part.get("type") == "image_url":
                    parts.append("[image]")
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return "" if content is None else str(content)


def message_tokens(msg: dict[str, Any]) -> int:
    total = estimate_tokens(_content_text(msg.get("content")))
    for tc in msg.get("tool_calls") or []:
        total += estimate_tokens(json.dumps(tc, ensure_ascii=False))
    return total


def conversation_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(message_tokens(m) for m in messages)


# Плотный JSON/код/пути жмутся хуже прозы: для ASCII берём 2.7 симв./токен вместо 3.8.
JSON_DENSE_CHARS_PER_TOKEN = float(os.environ.get("BRIDGE_DENSE_CHARS_PER_TOKEN", "2.7"))
MESSAGE_SAFETY_FACTOR = float(os.environ.get("BRIDGE_MESSAGE_SAFETY_FACTOR", "1.30"))


ESTIMATE_KIND = "heuristic_v5"
ESTIMATE_NOTE = ("эвристика символов; не гарантирует фактическое окно chat-template, "
                 "итог проверяется по prompt_eval_count живой модели")
CJK_PER_TOKEN = float(os.environ.get("BRIDGE_CJK_CHARS_PER_TOKEN", "1.0"))
B64_PER_TOKEN = float(os.environ.get("BRIDGE_B64_CHARS_PER_TOKEN", "1.5"))
_B64_RUN = re.compile(r"[A-Za-z0-9+/]{64,}={0,2}")


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF
            or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF or 0xAC00 <= o <= 0xD7AF)


def conservative_text_tokens(text: str) -> int:
    """Оценка «сверху»: кириллица 1.9, CJK и эмодзи 1.0, base64 1.5, прочее 2.7 симв./токен.

    Это эвристика, а не доказательство: chat-template, BPE-мердж и язык модели могут
    дать больше токенов. Поэтому итог всегда сверяется с prompt_eval_count живой Qwen.
    """
    if not text:
        return 0
    cyr = cjk = nonbmp = 0
    for ch in text:
        o = ord(ch)
        if 0x0400 <= o <= 0x04FF:
            cyr += 1
        elif o > 0xFFFF:
            nonbmp += 1
        elif _is_cjk(ch):
            cjk += 1
    b64 = 0
    for m in _B64_RUN.finditer(text):
        b64 += len(m.group(0))
    rest = max(0, len(text) - cyr - cjk - nonbmp - b64)
    total = (cyr / 1.9 + cjk / CJK_PER_TOKEN + nonbmp / 1.0
             + b64 / B64_PER_TOKEN + rest / JSON_DENSE_CHARS_PER_TOKEN)
    return int(total) + 8


def messages_tokens_conservative(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        content = (m or {}).get("content")
        if isinstance(content, str):
            total += conservative_text_tokens(content)
        elif content is not None:
            total += conservative_text_tokens(json.dumps(content, ensure_ascii=False))
        for tc in (m or {}).get("tool_calls") or []:
            total += conservative_text_tokens(json.dumps(tc, ensure_ascii=False))
        for field in ("role", "name", "tool_call_id", "reasoning_content", "function_call"):
            val = (m or {}).get(field)
            if val is not None:
                total += conservative_text_tokens(val if isinstance(val, str) else
                                                  json.dumps(val, ensure_ascii=False))
    return int(total * MESSAGE_SAFETY_FACTOR)


def full_request_tokens_conservative(messages: list[dict[str, Any]], tools_tokens: int,
                                     gen_reserve: int) -> dict[str, int]:
    """Разложенная по компонентам оценка полного запроса (не гарантия, а верхняя граница эвристики)."""
    msg = messages_tokens_conservative(messages)
    overhead = MSG_OVERHEAD_TOKENS * (len(messages) + 1)
    total = msg + overhead + TEMPLATE_OVERHEAD_TOKENS + tools_tokens + gen_reserve
    return {"messages": msg, "overhead": overhead, "template": TEMPLATE_OVERHEAD_TOKENS,
            "tools": tools_tokens, "output_reserve": gen_reserve, "margin": TOTAL_MARGIN_TOKENS,
            "total": total, "num_ctx": NUM_CTX}


def _balance(text: str) -> str:
    """Дополняет незакрытые { } и [ ], чтобы Ollama не падала на парсинге tool-вывода."""
    opens = {"{": "}", "[": "]"}
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in opens:
            stack.append(opens[ch])
        elif ch in ("}", "]") and stack and stack[-1] == ch:
            stack.pop()
    if in_str:
        text += '"'
    return text + "".join(reversed(stack))


def _shrink_message(msg: dict[str, Any], limit: int) -> dict[str, Any]:
    """Обрезает длинный tool-результат, сохраняя ссылку на полный вывод и маркер."""
    text = _content_text(msg.get("content"))
    if len(text) <= limit:
        return msg
    cut = text[: limit // 2]
    nl = cut.rfind("\n")
    if nl > limit // 4:
        cut = cut[: nl + 1]
    head, tail = cut, text[-(limit // 2) :]
    nl2 = tail.find("\n")
    if 0 <= nl2 < limit // 4:
        tail = tail[nl2 + 1 :]
    paths = []
    for token in text.split():
        if ("\\" in token or "/" in token) and ("." in token or ":" in token) and len(token) < 260:
            paths.append(token.strip(",;:()[]"))
    note = (
        f"\n...[{len(text) - limit} символов вырезано при передаче локальной модели; "
        f"это НЕПОЛНЫЙ вывод инструмента, не считай его полным результатом; "
        f"исходные файлы доступны через read_file/search_files]...\n"
    )
    if paths:
        note += "пути из вывода: " + ", ".join(dict.fromkeys(paths[-8:])) + "\n"
    body = head + note + tail
    balanced = _balance(body)
    out = dict(msg)
    if balanced != body:
        # Структура была битой (обрезанный JSON). Восстановление формальное — помечаем явно,
        # чтобы модель не приняла восстановленный фрагмент за целые данные.
        warn = ("[BRIDGE: JSON обрезан, скобки восстановлены формально. "
                "Данные НЕПОЛНЫЕ — это фрагмент, а не полный результат.]\n")
        out["content"] = warn + balanced + "\n" + warn
    else:
        out["content"] = balanced
    return out


_SUMMARY_ANCHOR_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9]{2,}(?:-[A-Z0-9]{2,}){1,5}(?![A-Za-z0-9_])")


def build_summary(messages: list[dict[str, Any]], budget_chars: int) -> str:
    """Lossy middle-history summary; preserve a bounded set of structured IDs.

    This is NOT semantic memory. Unrecognized, excessive or long identifiers can
    still disappear, and excerpts are truncated. Do not use as a tool receipt.
    """
    lines: list[str] = []
    anchors: list[str] = []
    seen_anchors: set[str] = set()
    for m in messages:
        role = m.get("role", "?")
        text = _content_text(m.get("content")).strip()
        calls = m.get("tool_calls") or []
        names = []
        for tc in calls:
            fn = (tc or {}).get("function") or {}
            name = fn.get("name") or "tool"
            args = fn.get("arguments") or ""
            paths = [t.strip('",') for t in str(args).split() if ("\\" in t or "/" in t)][:3]
            names.append(f"{name}({' '.join(paths)})" if paths else name)
        # Extract a few recognizable identifiers even when they appear beyond
        # the 220-character per-message excerpt. Cap global storage below.
        for anchor in _SUMMARY_ANCHOR_RE.findall(text)[:4]:
            if anchor not in seen_anchors:
                seen_anchors.add(anchor)
                anchors.append(anchor)
        snippet = " ".join(text.split())[:220]
        line = f"- {role}"
        if names:
            line += " → " + ", ".join(names)
        if snippet:
            line += ": " + snippet
        lines.append(line)
    if budget_chars <= 0:
        return ""
    # Reserve up to one quarter of the summary for identifiers; deliberately
    # do not claim completeness or accuracy of semantic relationships.
    anchor_budget = max(0, min(budget_chars // 4, 1200))
    anchor_line = "IDs present in omitted messages: " + ", ".join(anchors)
    if not anchors:
        anchor_line = ""
    elif len(anchor_line) > anchor_budget:
        anchor_line = anchor_line[:max(0, anchor_budget - 18)] + " ...[IDs omitted]"
    remainder = max(0, budget_chars - len(anchor_line) - (1 if anchor_line else 0))
    summary = "\n".join(lines)
    if len(summary) > remainder:
        # preserve both ends while respecting the exact character budget.
        ellipsis = "\n...[middle omitted]...\n"
        if remainder <= len(ellipsis):
            summary = summary[:remainder]
        else:
            half = (remainder - len(ellipsis)) // 2
            summary = summary[:half] + ellipsis + summary[-(remainder-len(ellipsis)-half):]
    return (anchor_line + "\n" + summary if anchor_line else summary)[:budget_chars]


def sanitize(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Чинит разорванные пары tool_call ↔ tool после сокращения (Ollama иначе отвечает 400)."""
    out: list[dict[str, Any]] = []
    pending: set[str] = set()
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            calls = msg.get("tool_calls") or []
            ids = {str((tc or {}).get("id")) for tc in calls if (tc or {}).get("id")}
            text = _content_text(msg.get("content")).strip()
            if not calls and not text:
                continue
            pending = ids
            out.append(msg)
            continue
        if role == "tool":
            cid = str(msg.get("tool_call_id"))
            if cid in pending:
                pending.discard(cid)
                out.append(msg)
            continue
        pending = set()
        out.append(msg)
    # если результат инструмента потерялся при сокращении — убираем и сам вызов
    kept_ids = {str(m.get("tool_call_id")) for m in out if m.get("role") == "tool"}
    for i, msg in enumerate(out):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            calls = [tc for tc in msg["tool_calls"] if str((tc or {}).get("id")) in kept_ids]
            if calls:
                out[i] = {**msg, "tool_calls": calls}
            else:
                out[i] = {k: v for k, v in msg.items() if k != "tool_calls"}
    return [m for m in out if not (m.get("role") == "assistant" and not _content_text(m.get("content")).strip()
                                   and not m.get("tool_calls"))]


def condense(messages: list[dict[str, Any]], budget_tokens: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Возвращает (сокращённые сообщения, отчёт). Никогда не теряет system и хвост."""
    before = conversation_tokens(messages)
    report: dict[str, Any] = {"tokens_before_est": before, "dropped_messages": 0, "condensed": False}
    if before <= budget_tokens:
        report["tokens_after_est"] = before
        return messages, report

    systems = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    tail = rest[-KEEP_RECENT:] if KEEP_RECENT > 0 else []
    # Лимит на одно сообщение считаем от бюджета окна, а не от TOOL_RESULT_CHARS:
    # иначе один длинный пользовательский промпт сжимается до 1200 символов (потеря контекста).
    budget_chars = int(budget_tokens * 3.4)
    per_msg = max(TOOL_RESULT_CHARS, int(budget_chars * 0.85) // max(1, len(tail))) if tail else TOOL_RESULT_CHARS
    tail = [_shrink_message(m, per_msg) for m in tail]
    middle = rest[: len(rest) - len(tail)] if tail else rest

    summary = build_summary(middle, SUMMARY_CHARS)
    notice = {
        "role": "system",
        "content": (
            f"{MARKER} История сокращена для локальной модели (окно {NUM_CTX} токенов). "
            f"Свёрнуто сообщений: {len(middle)} из {len(rest)}; последние {len(tail)} — дословно. "
            "Опирайся только на видимый текст и резюме ниже. "
            "Внутри длинных сообщений часть содержимого вырезана (в местах вырезания стоит пометка "
            "о пропущенных символах) — этих данных у тебя НЕТ. "
            "Резюме неполное; сохранённые идентификаторы не доказывают контекст или результат действий. "
            "Если факта не хватает, сообщи о неопределённости и не выдумывай детали. "
            "Полные выводы инструментов лежат в файлах по указанным путям — при необходимости "
            "попроси их прочитать.\n\nРезюме середины диалога:\n" + summary
        ),
    }
    out = sanitize(systems + [notice] + tail)

    # Если после свёртки середины всё ещё не влезает — ужимаем содержимое последних
    # сообщений пропорционально бюджету (а не выбрасываем их целиком).
    if conversation_tokens(out) > budget_tokens and tail:
        _cur = _content_text(tail[-1].get("content"))
        _est = conversation_tokens(tail[-1:]) or 1
        _ratio = (len(_cur) / _est) if _cur else 2.0
        lim = max(2000, int(budget_tokens * 0.75 * _ratio) // max(1, len(tail)))
        tail = [_shrink_message(m, lim) for m in tail]
        out = sanitize(systems + [notice] + tail)

    # Страховка: если всё равно не влезает — режем summary, затем самые старые из хвоста
    while conversation_tokens(out) > budget_tokens and len(out) > (len(systems) + 2):
        idx = len(systems) + 1 if len(out) > len(systems) + 2 else None
        if idx is None:
            break
        out.pop(idx)
        report["dropped_messages"] += 1

    report["condensed"] = True
    report["dropped_messages"] = len(middle) + report["dropped_messages"]
    report["kept_recent"] = len(tail)
    report["tokens_after_est"] = conversation_tokens(out)
    return out, report


def log_event(kind: str, payload: dict[str, Any], full_messages: list[dict[str, Any]] | None = None) -> str:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    record = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, **payload}
    if full_messages is not None and payload.get("condensed"):
        spill = write_spill(full_messages, f"handoff_{stamp}")
        record["full_prompt_path"] = str(spill) if spill else None
        record["spill_mode"] = SPILL_MODE
    with _LOG_LOCK:
        with open(LOG_DIR / "bridge.jsonl", "a", encoding="utf-8") as fh:
            fh.write(redact(json.dumps(record, ensure_ascii=False)) + "\n")
    return json.dumps(record, ensure_ascii=False)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # тише в консоли
        if os.environ.get("BRIDGE_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._rid_header()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/v1/models", "/api/tags"):
            self._proxy_json("GET", self.path, None)
            return
        if self.path.rstrip("/") in ("/health", "/"):
            self._json(200, {"ok": True, "num_ctx": NUM_CTX, "max_prompt_tokens": MAX_PROMPT})
            return
        self._json(404, {"error": {"message": "not found"}})

    def _rid_header(self) -> None:
        """Заголовок для точной сверки запроса с его usage-событием в логе."""
        rid = getattr(self, "request_id", "")
        if rid:
            self.send_header("X-Bridge-Request-Id", rid)

    def _sse(self, obj: dict[str, Any]) -> None:
        self.wfile.write(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n")
        self.wfile.flush()

    def _sse_text(self, model: str, text: str) -> None:
        """Локально сформированный ответ в формате SSE (для stream=true)."""
        cid = f"chatcmpl-bridge-{int(time.time() * 1000)}"
        created = int(time.time())
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Bridge-Policy", "blocked-repeat")
        self._rid_header()
        self.end_headers()

        def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
            return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        self._sse(chunk({"role": "assistant"}))
        self._sse(chunk({"content": text}))
        self._sse(chunk({}, "stop"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _stream_native(self, payload: dict[str, Any], out_messages: list[dict[str, Any]],
                       report: dict[str, Any], fp: str = "") -> None:
        """Отдаёт ответ Ollama клиенту как OpenAI SSE-поток (Hermes по умолчанию стримит)."""
        model = str(payload.get("model") or "")
        body = to_ollama_chat(payload, out_messages)
        body["stream"] = True
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(UPSTREAM + "/api/chat", data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        cid = f"chatcmpl-bridge-{int(time.time() * 1000)}"
        created = int(time.time())
        self.close_connection = True

        def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
            return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        try:
            resp = urllib.request.urlopen(req, timeout=900)
        except urllib.error.HTTPError as exc:
            self._json(exc.code if 400 <= exc.code < 600 else 502,
                       {"error": {"message": f"upstream error: {exc.read().decode('utf-8', 'replace')[:400]}"}})
            return
        except Exception as exc:  # noqa: BLE001
            self._json(502, {"error": {"message": f"upstream unavailable: {exc}"}})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Bridge-Condensed", "1" if report.get("condensed") else "0")
            self._rid_header()
            self.end_headers()
            self._sse(chunk({"role": "assistant"}))
            # tool_calls копим до done: фрагментированные дельты не должны уходить
            # клиенту по частям, иначе запрещённый вызов виден до проверки политики.
            pending_calls: dict[int, dict[str, Any]] = {}
            saw_done = False
            truncation: str | None = None
            usage_native: dict[str, Any] = {}
            for line in resp:
                if not line.strip():
                    continue
                try:
                    evt = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                msg = evt.get("message") or {}
                if msg.get("content"):
                    self._sse(chunk({"content": msg["content"]}))
                for pos, tc in enumerate(msg.get("tool_calls") or []):
                    key = tc.get("index") if isinstance(tc.get("index"), int) else pos
                    bucket = pending_calls.setdefault(key, {"function": {"name": "", "arguments": ""}})
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        frag = str(fn["name"])
                        cur = bucket["function"]["name"]
                        # Имя тоже может прийти частями; повтор целого имени не дублируем.
                        bucket["function"]["name"] = frag if (not cur or cur == frag) else cur + frag
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        bucket["function"]["arguments"] += args
                    elif args is not None:
                        bucket["function"]["arguments"] = args
                    if tc.get("id"):
                        bucket["id"] = tc["id"]
                if evt.get("error"):
                    truncation = "upstream_error"
                    break
                if evt.get("done"):
                    saw_done = True
                    usage_native = {"prompt_eval_count": evt.get("prompt_eval_count"),
                                    "eval_count": evt.get("eval_count")}
                    merged = [pending_calls[k] for k in sorted(pending_calls)]
                    allowed, blocked = stream_filter_calls(merged)
                    if blocked:
                        # запрещённый вызов не отдаём даже частично: вместо него текст
                        note_blocked(fp)
                        log_event("tool_policy_block", {"blocked": blocked, "mode": "stream",
                                                        "calls_in_response": len(merged),
                                                        "request_id": getattr(self, "request_id", "")})
                        self._sse(chunk({"content": "\n\n" + policy_block_message(blocked)}))
                        self._sse(chunk({}, "stop"))
                    elif allowed:
                        fixed = _args_to_str({"tool_calls": allowed})["tool_calls"]
                        for idx, tc in enumerate(fixed):
                            tc["index"] = idx
                        self._sse(chunk({"tool_calls": fixed}))
                        self._sse(chunk({}, "tool_calls"))
                    else:
                        self._sse(chunk({}, "stop"))
                    break
            if not saw_done and truncation is None:
                truncation = "eof_before_done"
            if truncation:
                # HTTP 200 уже отправлен: задним числом 502 не вернуть. Отдаём
                # документированный SSE error и закрываем поток БЕЗ [DONE] и без
                # finish_reason — клиент не должен принять ответ за полный.
                log_event("stream_truncated",
                          {"reason": truncation, "request_id": getattr(self, "request_id", ""),
                           "bytes_partial": True, "finish_reason_sent": False})
                self._sse({"error": {"code": "stream_truncated",
                                     "message": "upstream оборвал поток до done:true; ответ неполный"}})
                self.wfile.flush()
                return
            log_event("usage", {
                "stream": True,
                "prompt_eval_count": usage_native.get("prompt_eval_count"),
                "eval_count": usage_native.get("eval_count"),
                "tokens_messages_in": report.get("split_messages"),
                "tokens_tools": report.get("split_tools"),
                "tokens_template": TEMPLATE_OVERHEAD_TOKENS,
                "tokens_output_reserve": report.get("gen_reserve"),
                "tokens_total": report.get("split_total"),
                "num_ctx": NUM_CTX,
                "num_predict": report.get("gen_reserve"),
                "condensed": bool(report.get("condensed")),
                "history_budget_final": report.get("history_budget_final"),
                "tools_count": report.get("tools_count"),
                "policy_mode": TOOL_POLICY_MODE,
                "estimate_kind": ESTIMATE_KIND,
                "estimate_note": ESTIMATE_NOTE,
                "request_id": getattr(self, "request_id", ""),
                "unsafe_mode": TOOL_POLICY_MODE != "enforce" or ALLOW_NETWORK,
            })
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._json(400, {"error": {"code": "invalid_content_length"}})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(413, {"error": {"code": "request_too_large",
                                       "max_body_bytes": MAX_BODY_BYTES}})
            return
        raw = self.rfile.read(length) if length else b"{}"
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/api/chat"):
            self._json(404, {"error": {"message": "only /v1/chat/completions is bridged"}})
            return
        if self.path.rstrip("/") == "/api/chat" and TOOL_POLICY_MODE == "enforce":
            # Нативный маршрут не разбирает tool_calls в ответе, поэтому в enforce-режиме
            # он закрыт: иначе политику инструментов можно обойти одним запросом.
            log_event("endpoint_rejected", {"path": "/api/chat", "mode": TOOL_POLICY_MODE})
            self._json(403, {"error": {"code": "endpoint_not_supported",
                                       "message": "/api/chat не поддерживается в резервном режиме; "
                                                  "используйте /v1/chat/completions"}})
            return
        try:
            payload = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": {"message": "invalid json"}})
            return
        if (not isinstance(payload, dict)
                or not isinstance(payload.get("messages"), list)
                or any(not isinstance(m, dict) for m in payload["messages"])
                or not isinstance(payload.get("tools", []), list)
                or any(not isinstance(t, dict) for t in payload.get("tools", []))):
            self._json(400, {"error": {"code": "invalid_request_shape"}})
            return
        fp = request_fingerprint(
            payload,
            session_hint=(self.headers.get("X-Session-Id")
                          or self.headers.get("X-Hermes-Session") or ""),
        )
        self.request_id = f"{REQUEST_ID_PREFIX}-{uuid.uuid4().hex[:16]}"
        note_unsafe_mode()
        if blocked_recently(fp) and history_has_block_marker(payload.get("messages") or []):
            log_event("policy_stop", {"reason": "repeated_blocked_tool_call",
                                      "stream": bool(payload.get("stream"))})
            model_name = str(payload.get("model") or "")
            repeat_text = policy_block_message(["<повтор запрещённого вызова>"])
            if payload.get("stream"):
                # Клиент ждёт SSE; JSON здесь сломал бы протокол.
                self._sse_text(model_name, repeat_text)
            else:
                self._json(200, text_only_response(model_name, repeat_text))
            return

        plan = plan_budget(payload)
        if plan["verdict"] != "ok":
            invalid_mt = plan["verdict"] == "invalid_max_tokens"
            event = log_event("max_tokens_invalid" if invalid_mt else "context_length_exceeded", plan["log"])
            self._json(400, {"error": {"code": "invalid_max_tokens" if invalid_mt else "context_length_exceeded",
                                       "message": ("некорректный max_tokens; в Ollama не отправлялся"
                                                   if invalid_mt else
                                                   "запрос не помещается в контекст; в Ollama не отправлялся"),
                                       **plan["error"], "bridge_event": json.loads(event)}})
            return

        payload = plan["payload"]
        history_budget = plan["history_budget"]
        # B4: несжимаемая часть (system + схемы + последний пользовательский ввод +
        # резерв генерации) проверяется ДО проходов condense: если она не влезает,
        # сжатие истории задачу всё равно не спасёт.
        noncomp = non_compressible_tokens(payload["messages"], plan["tools_tokens"], plan["gen_reserve"])
        if noncomp["total"] > NUM_CTX - TOTAL_MARGIN_TOKENS:
            event = log_event("context_not_compressible", {**noncomp, **plan["log"]})
            self._json(400, {"error": {"code": "context_not_compressible",
                                       "message": "system, схемы инструментов и последний ввод не помещаются "
                                                  "в окно; сжатие истории не поможет",
                                       **noncomp, "bridge_event": json.loads(event)}})
            return
        out_messages, report = condense(payload.get("messages") or [], history_budget)
        split = full_request_tokens_conservative(out_messages, plan["tools_tokens"], plan["gen_reserve"])
        # Одна попытка сжатия считается по оптимистичной оценке, а проверка — по
        # консервативной: если не прошло, ужимаем историю сильнее (не больше двух проходов).
        passes = 0
        while split["total"] > NUM_CTX - TOTAL_MARGIN_TOKENS and passes < 2:
            passes += 1
            opt_now = max(1, conversation_tokens(out_messages))
            ratio = max(MESSAGE_SAFETY_FACTOR, min(3.0, split["messages"] / opt_now))
            room = (NUM_CTX - plan["gen_reserve"] - plan["tools_tokens"] - TEMPLATE_OVERHEAD_TOKENS
                    - TOTAL_MARGIN_TOKENS - MSG_OVERHEAD_TOKENS * (len(out_messages) + 1))
            tighter = max(MIN_HISTORY_TOKENS, int(max(room, 0) / ratio))
            if tighter < MIN_HISTORY_TOKENS or tighter >= history_budget:
                break
            history_budget = tighter
            out_messages, report = condense(payload.get("messages") or [], history_budget)
            split = full_request_tokens_conservative(out_messages, plan["tools_tokens"], plan["gen_reserve"])
        out_messages, pairs_report = repair_tool_pairs(out_messages)
        report.update(pairs_report)
        split = full_request_tokens_conservative(out_messages, plan["tools_tokens"], plan["gen_reserve"])
        report["condense_passes"] = 1 + passes
        report["history_budget_final"] = history_budget
        total_est = split["total"]
        if total_est > NUM_CTX - TOTAL_MARGIN_TOKENS:
            event = log_event("condense_exhausted",
                              {**plan["log"], **split, "tokens_after_condense_est": total_est,
                               **report, "removable": len(out_messages) > 2})
            self._json(400, {"error": {"code": "context_length_exceeded",
                                       "message": "после сжатия запрос всё равно не помещается; в Ollama не отправлялся",
                                       "estimated_tokens": total_est, "num_ctx": NUM_CTX,
                                       "generation_reserve": plan["gen_reserve"],
                                       "tools_tokens": plan["tools_tokens"],
                                       "estimated_split": split,
                                       "bridge_event": json.loads(event)}})
            return

        report["split_messages"] = split["messages"]
        report["split_tools"] = split["tools"]
        report["split_total"] = split["total"]
        report["gen_reserve"] = plan["gen_reserve"]
        report["tools_count"] = len(payload.get("tools") or [])
        log_event("handoff" if report["condensed"] else "passthrough",
                  {**report, "budget": history_budget,
                   "tokens_after_est": conversation_tokens(out_messages),
                   "tools_tokens_est": plan["tools_tokens"], "tools_kept": plan["log"]["tools_kept"],
                   "tools_dropped": plan["log"]["tools_dropped"],
                   "gen_reserve": plan["gen_reserve"],
                   "stream": bool(payload.get("stream"))}, out_messages)

        if payload.get("stream"):
            self._stream_native(payload, out_messages, report, fp)
            return

        body = dict(payload)
        body["messages"] = out_messages
        opts = dict(body.get("options") or {})
        opts.setdefault("num_ctx", NUM_CTX)
        if "options" in body or self.path.rstrip("/") == "/api/chat":
            body["options"] = opts
        try:
            if NATIVE and self.path.rstrip("/") == "/v1/chat/completions":
                body = to_ollama_chat(payload, out_messages)
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(UPSTREAM + "/api/chat", data=data, method="POST",
                                             headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=900) as resp:
                        native = json.loads(resp.read().decode("utf-8"))
                except urllib.error.HTTPError as exc:
                    # Privacy: never dump the request body or full conversation on
                    # upstream errors; SPILL_MODE=off must really mean no prompt dump.
                    log_event("upstream_http_error", {"status": exc.code,
                                                       "request_id": getattr(self, "request_id", "")})
                    self._json(502, {"error": {"code": "upstream_error", "message": "upstream отклонил запрос", "status": exc.code}})
                    return
                except Exception as exc:  # noqa: BLE001
                    log_event("upstream_unavailable", {"error": str(exc)[:300]})
                    self._json(502, {"error": {"code": "upstream_unavailable", "message": "upstream недоступен; запрос не выполнен"}})
                    return
                blocked = apply_tool_policy(native)
                if blocked:
                    note_blocked(fp)
                    log_event("tool_policy_block", {"blocked": blocked, "mode": "non_stream"})
                try:
                    log_event("usage", {
                        "tokens_before_est": report["tokens_before_est"],
                        "tokens_after_est": report["tokens_after_est"],
                        "prompt_tokens_actual": native.get("prompt_eval_count"),
                        "completion_tokens_actual": native.get("eval_count"),
                        "condensed": report["condensed"],
                        "budget": history_budget,
                        "history_budget_final": report.get("history_budget_final"),
                        "num_predict": plan["gen_reserve"],
                        "estimate_kind": ESTIMATE_KIND,
                        "estimate_note": ESTIMATE_NOTE,
                        "request_id": getattr(self, "request_id", ""),
                        "unsafe_mode": TOOL_POLICY_MODE != "enforce" or ALLOW_NETWORK,
                        "tokens_messages_in": split["messages"],
                        "tokens_tools": split["tools"],
                        "tokens_template": split["template"],
                        "tokens_output_reserve": split["output_reserve"],
                        "tokens_total": split["total"],
                        "num_ctx": NUM_CTX,
                        "policy_mode": TOOL_POLICY_MODE,
                        "prompt_eval_count": native.get("prompt_eval_count"),
                        "tools_count": len(plan["payload"].get("tools") or []),
                    })
                except Exception:
                    pass
                answer = json.dumps(to_openai_response(native, str(payload.get("model") or "")),
                                    ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(answer)))
                self.send_header("X-Bridge-Condensed", "1" if report["condensed"] else "0")
                self.send_header("X-Bridge-Prompt-Tokens", str(native.get("prompt_eval_count") or 0))
                self._rid_header()
                self.end_headers()
                self.wfile.write(answer)
                return
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(UPSTREAM + self.path, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=900) as resp:
                resp_raw = resp.read()
                code = resp.status
                ctype = resp.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as exc:
            resp_raw, code, ctype = exc.read(), exc.code, exc.headers.get("Content-Type", "application/json")
        except Exception as exc:  # noqa: BLE001
            log_event("upstream_unavailable", {"error_type": type(exc).__name__,
                                                "request_id": getattr(self, "request_id", "")})
            self._json(502, {"error": {"code": "upstream_unavailable", "message": "upstream недоступен"}})
            return
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(resp_raw)))
        self.send_header("X-Bridge-Condensed", "1" if report["condensed"] else "0")
        self.end_headers()
        self.wfile.write(resp_raw)

    def _proxy_json(self, method: str, path: str, data: bytes | None) -> None:
        try:
            req = urllib.request.Request(UPSTREAM + path, data=data, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw, code = resp.read(), resp.status
        except Exception as exc:  # noqa: BLE001
            self._json(502, {"error": {"message": str(exc)}})
            return
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


_MUTEX_HANDLE = None
MUTEX_NAME = "HermesOllamaBridgeExperimental_v1_" + str(PORT) + os.environ.get("BRIDGE_MUTEX_SUFFIX", "")


def acquire_single_instance_lock() -> None:
    """Единственный экземпляр моста через именованный mutex Windows.

    Блокировка снимается ОС при любом завершении процесса (включая аварийное),
    поэтому ложных блокировок не остаётся. Работает независимо от способа запуска.
    """
    global _MUTEX_HANDLE
    import ctypes
    from ctypes import wintypes

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    err = ctypes.get_last_error()
    if not handle:
        raise SystemExit("bridge: не удалось создать mutex единственного экземпляра")
    if err == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        raise SystemExit("bridge: уже запущен другой экземпляр (mutex " + MUTEX_NAME + ")")
    _MUTEX_HANDLE = handle


def _assert_local_upstream(url: str) -> None:
    """Prevent accidentally forwarding conversation content to a remote upstream."""
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise SystemExit("BRIDGE_UPSTREAM must be a plain loopback HTTP base URL")


def _assert_loopback(host: str) -> None:
    """Мост держит только локальный адрес: иначе через него можно дёргать Ollama из сети."""
    allowed = {"127.0.0.1", "localhost", "::1"}
    if host not in allowed:
        raise SystemExit(f"BRIDGE_HOST={host!r} запрещён: разрешены только {sorted(allowed)}")


class _SingleInstanceServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "decrypt":
        raw = Path(sys.argv[2]).read_bytes()
        if raw.startswith(b"BRIDGE-DPAPI-1\n"):
            raw = raw[len(b"BRIDGE-DPAPI-1\n"):]
        sys.stdout.buffer.write(dpapi_decrypt(raw))
        return
    _assert_loopback(HOST)
    _assert_local_upstream(UPSTREAM)
    try:
        socket.create_connection((HOST, PORT), timeout=1).close()
        raise SystemExit(f"bridge: порт {PORT} уже занят — второй экземпляр не запускаю")
    except OSError:
        pass
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    restrict_acl(LOG_DIR)
    purged = cleanup_old()
    server = _SingleInstanceServer((HOST, PORT), Handler)
    print(json.dumps({"bridge": "up", "addr": f"http://{HOST}:{PORT}", "num_ctx": NUM_CTX,
                      "max_prompt_tokens": MAX_PROMPT, "log_dir": str(LOG_DIR),
                      "retention_hours": RETENTION_HOURS, "purged": purged,
                      "reserve_tokens": RESERVE, "spill_mode": SPILL_MODE}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    # Подкоманды (decrypt, health и т.п.) не должны занимать mutex сервера:
    # иначе утилита расшифровки падает с «уже запущен другой экземпляр».
    _argv = [a for a in sys.argv[1:]]
    _sub = _argv[0] if _argv and not _argv[0].startswith("-") else None
    if _sub not in {"decrypt", "encrypt", "health", "check", "selftest"}:
        acquire_single_instance_lock()
    main()
