"""Channel 2 adapter: notebooklm-py (>=0.8.6) multi-profile async wrapper."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from notebooklm import NotebookLMClient
from notebooklm._env import ENTERPRISE_APP_HOSTS, PERSONAL_APP_HOSTS, get_base_host
from notebooklm.auth import (
    MINIMUM_REQUIRED_COOKIES,
    _has_valid_secondary_binding,
    _is_allowed_auth_domain,
    replace_profile_from_login,
)
from notebooklm.exceptions import AuthError, NotebookLMError
from notebooklm.paths import get_storage_path, list_profiles
from notebooklm.types import (
    DriveMimeType,
    ReportFormat,
    SharePermission,
    UsageSummaryStatus,
    UsageWindowKind,
)

logger = logging.getLogger(__name__)

_TIER_CODE_MAP: dict[int, str] = {
    1: "free",
    2: "pro",
    3: "ultra",
    4: "plus",
    6: "ultra",
}

_DRIVE_MIME_ALIASES: dict[str, str] = {
    "google-doc": DriveMimeType.GOOGLE_DOC.value,
    "google_doc": DriveMimeType.GOOGLE_DOC.value,
    "doc": DriveMimeType.GOOGLE_DOC.value,
    "google-slides": DriveMimeType.GOOGLE_SLIDES.value,
    "google_slides": DriveMimeType.GOOGLE_SLIDES.value,
    "slides": DriveMimeType.GOOGLE_SLIDES.value,
    "google-sheets": DriveMimeType.GOOGLE_SHEETS.value,
    "google_sheets": DriveMimeType.GOOGLE_SHEETS.value,
    "sheets": DriveMimeType.GOOGLE_SHEETS.value,
    "pdf": DriveMimeType.PDF.value,
}

# Key locks by (loop_id, profile_name) so tests across event loops never collide.
_profile_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _get_lock(profile_name: str) -> asyncio.Lock:
    loop_id = id(asyncio.get_running_loop())
    key = (loop_id, profile_name)
    lock = _profile_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _profile_locks[key] = lock
    return lock


def discover_existing_profiles() -> list[dict[str, Any]]:
    """Return existing ~/.notebooklm/profiles/* entries with local cookie health."""
    profiles = []
    for name in list_profiles():
        auth = check_local_auth(name)
        profiles.append({"profile": name, **auth})
    return profiles


def check_local_auth(profile_name: str) -> dict[str, Any]:
    """Inspect on-disk storage_state.json without making network requests."""
    storage_path = get_storage_path(profile=profile_name)
    if not storage_path.exists():
        return {
            "exists": False,
            "valid_json": False,
            "cookies_ok": False,
            "status": "unauthenticated",
            "storage_path": str(storage_path),
        }
    try:
        data = json.loads(storage_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "exists": True,
            "valid_json": False,
            "cookies_ok": False,
            "status": "error",
            "storage_path": str(storage_path),
        }

    cookies = data.get("cookies") if isinstance(data, dict) else None
    names: set[str] = set()
    if isinstance(cookies, list):
        for item in cookies:
            if not isinstance(item, dict):
                continue
            cname = item.get("name")
            cval = item.get("value")
            if isinstance(cname, str) and cname.strip() and isinstance(cval, str) and cval.strip():
                names.add(cname.strip())
    has_session_cookie = ("SID" in names) and bool(names & {"__Secure-1PSIDTS", "__Secure-1PSID"})
    return {
        "exists": True,
        "valid_json": True,
        "cookies_ok": has_session_cookie,
        "status": "ok" if has_session_cookie else "expired",
        "storage_path": str(storage_path),
    }


async def poll_account_usage(profile_name: str) -> dict[str, Any]:
    """Fetch live 5-hour & weekly compute usage plus account limits."""
    async with _get_lock(profile_name):
        try:
            async with NotebookLMClient.from_storage(profile=profile_name, keepalive=600.0) as client:
                summary = await client.settings.get_usage()
                user_settings = await client.settings.get_user_settings()
                email = await client.get_account_email(live_fallback=True)
        except AuthError as exc:
            return {
                "auth_status": "expired",
                "status": "error",
                "error_msg": f"Authentication expired: {exc}",
                "actions": [],
            }
        except NotebookLMError as exc:
            return {
                "auth_status": "error",
                "status": "error",
                "error_msg": str(exc),
                "actions": [],
            }

        five_hour = summary.window(UsageWindowKind.FIVE_HOUR)
        weekly = summary.window(UsageWindowKind.WEEKLY)
        active = summary.active_window
        actions = [
            {
                "code": action.code,
                "kind": action.kind.name.lower() if action.kind is not None else None,
                "has_sufficient_quota": bool(action.has_sufficient_quota),
                "cost_tier": action.cost_tier.name.lower() if action.cost_tier is not None else None,
                "remaining_deferred_artifact_generations": action.remaining_deferred_artifact_generations,
                "estimated_cost_percent": action.estimated_cost_percent,
            }
            for action in summary.actions
        ]
        limits = user_settings.limits
        detected_tier = _TIER_CODE_MAP.get(limits.tier) if limits.tier is not None else None
        status_str = summary.status.value if isinstance(summary.status, UsageSummaryStatus) else str(summary.status)

        return {
            "auth_status": "ok",
            "status": status_str,
            "five_hour_used_pct": five_hour.used_percent if five_hour else None,
            "five_hour_remaining_pct": five_hour.remaining_percent if five_hour else None,
            "five_hour_resets_at": five_hour.resets_at.isoformat() if five_hour else None,
            "weekly_used_pct": weekly.used_percent if weekly else None,
            "weekly_remaining_pct": weekly.remaining_percent if weekly else None,
            "weekly_resets_at": weekly.resets_at.isoformat() if weekly else None,
            "is_exhausted": bool(summary.is_exhausted),
            "active_window": active.kind.name.lower() if active else None,
            "actions": actions,
            "email": email,
            "notebook_limit": limits.notebook_limit,
            "source_limit": limits.source_limit,
            "detected_tier": detected_tier,
            "output_language": user_settings.output_language,
            "error_msg": None,
        }


async def list_notebooks(profile_name: str) -> list[dict[str, Any]]:
    async with _get_lock(profile_name):
        async with NotebookLMClient.from_storage(profile=profile_name, keepalive=600.0) as client:
            nbs = await client.notebooks.list()
            return [
                {
                    "id": nb.id,
                    "title": nb.title,
                    "sources_count": int(getattr(nb, "sources_count", 0) or 0),
                    "is_owner": bool(getattr(nb, "is_owner", True)),
                    "role": nb.role.name.lower() if getattr(nb, "role", None) is not None else None,
                }
                for nb in nbs
            ]


async def create_notebook(profile_name: str, title: str) -> dict[str, Any]:
    async with _get_lock(profile_name):
        async with NotebookLMClient.from_storage(profile=profile_name, keepalive=600.0) as client:
            nb = await client.notebooks.create(title)
            return {"id": nb.id, "title": nb.title, "sources_count": int(getattr(nb, "sources_count", 0) or 0)}


async def list_sources(profile_name: str, notebook_id: str) -> list[dict[str, Any]]:
    async with _get_lock(profile_name):
        async with NotebookLMClient.from_storage(profile=profile_name, keepalive=600.0) as client:
            sources = await client.sources.list(notebook_id)
            return [
                {
                    "id": src.id,
                    "title": getattr(src, "title", ""),
                    "kind": str(getattr(src, "kind", "")),
                    "status": str(getattr(src, "status", "")),
                }
                for src in sources
            ]


async def add_url(
    profile_name: str, notebook_id: str, url: str, *, title: str | None = None, wait: bool = True
) -> dict[str, Any]:
    async with _get_lock(profile_name):
        async with NotebookLMClient.from_storage(profile=profile_name, keepalive=600.0) as client:
            src = await client.sources.add_url(notebook_id, url, wait=wait, title=title)
            return {"id": src.id, "title": getattr(src, "title", title or url)}


async def add_file(
    profile_name: str, notebook_id: str, file_path: str | Path, *, title: str | None = None, wait: bool = True
) -> dict[str, Any]:
    async with _get_lock(profile_name):
        async with NotebookLMClient.from_storage(profile=profile_name, keepalive=600.0) as client:
            src = await client.sources.add_file(notebook_id, str(file_path), wait=wait, title=title)
            return {"id": src.id, "title": getattr(src, "title", Path(file_path).name)}


async def add_text(
    profile_name: str, notebook_id: str, title: str, content: str, *, wait: bool = True
) -> dict[str, Any]:
    async with _get_lock(profile_name):
        async with NotebookLMClient.from_storage(profile=profile_name, keepalive=600.0) as client:
            src = await client.sources.add_text(notebook_id, title, content, wait=wait)
            return {"id": src.id, "title": getattr(src, "title", title)}


async def add_drive(
    profile_name: str,
    notebook_id: str,
    file_id: str,
    title: str,
    *,
    mime_type: str = "google-doc",
    wait: bool = True,
) -> dict[str, Any]:
    resolved_mime = _DRIVE_MIME_ALIASES.get(mime_type, mime_type)
    async with _get_lock(profile_name):
        async with NotebookLMClient.from_storage(profile=profile_name, keepalive=600.0) as client:
            src = await client.sources.add_drive(notebook_id, file_id, title, mime_type=resolved_mime, wait=wait)
            return {"id": src.id, "title": getattr(src, "title", title)}


async def add_drive_file(
    profile_name: str, notebook_id: str, document_id: str, *, title: str | None = None, wait: bool = True
) -> dict[str, Any]:
    async with _get_lock(profile_name):
        async with NotebookLMClient.from_storage(profile=profile_name, keepalive=600.0) as client:
            src = await client.sources.add_drive_file(notebook_id, document_id, title=title, wait=wait)
            return {"id": src.id, "title": getattr(src, "title", title or document_id)}


async def share_editor(
    profile_name: str, notebook_id: str, email: str, *, notify: bool = False
) -> dict[str, Any]:
    async with _get_lock(profile_name):
        async with NotebookLMClient.from_storage(profile=profile_name, keepalive=600.0) as client:
            status = await client.sharing.add_user(
                notebook_id,
                email,
                permission=SharePermission.EDITOR,
                notify=notify,
            )
            return {
                "shared_email": email,
                "permission": "editor",
                "users_count": len(getattr(status, "shared_users", []) or []),
            }


async def ask_question(
    profile_name: str,
    notebook_id: str,
    question: str,
    *,
    source_ids: list[str] | None = None,
    save_as_note: bool = False,
    note_title: str | None = None,
) -> dict[str, Any]:
    async with _get_lock(profile_name):
        async with NotebookLMClient.from_storage(profile=profile_name, keepalive=600.0) as client:
            res = await client.chat.ask(notebook_id, question, source_ids=source_ids)
            saved_note_id = None
            if save_as_note:
                note = await client.chat.save_answer_as_note(notebook_id, res, title=note_title)
                saved_note_id = getattr(note, "id", None)
            return {
                "answer": res.answer,
                "conversation_id": res.conversation_id,
                "turn_number": res.turn_number,
                "references_count": len(res.references or []),
                "saved_note_id": saved_note_id,
            }


def _safe_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


async def generate_and_download(
    profile_name: str,
    notebook_id: str,
    kind: str,
    output_dir: str | Path,
    *,
    instructions: str | None = None,
    language: str = "zh-Hans",
    later: bool = False,
    source_ids: list[str] | None = None,
    title_prefix: str = "",
) -> dict[str, Any]:
    """Generate a Studio artifact and download it locally (or defer via later=True)."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _safe_stamp()
    safe_prefix = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", (title_prefix or "").strip()).strip(". ")[:60]
    prefix = f"{safe_prefix}_" if safe_prefix else ""

    async with _get_lock(profile_name):
        async with NotebookLMClient.from_storage(profile=profile_name, keepalive=600.0) as client:
            if kind == "mind_map":
                mm = await client.artifacts.generate_mind_map(
                    notebook_id, source_ids=source_ids, language=language, instructions=instructions
                )
                target = out_dir / f"{prefix}mind_map_{stamp}.json"
                if mm.mind_map is not None:
                    target.write_text(json.dumps(mm.mind_map, ensure_ascii=False, indent=2), encoding="utf-8")
                else:
                    await client.artifacts.download_mind_map(notebook_id, str(target))
                return {
                    "status": "done",
                    "remote_task_id": mm.note_id or "",
                    "local_drive_path": str(target),
                    "title": f"{prefix}mind_map_{stamp}",
                }

            if kind == "audio":
                gen = await client.artifacts.generate_audio(
                    notebook_id, source_ids=source_ids, language=language, instructions=instructions, later=later
                )
                ext = ".m4a"
            elif kind == "video":
                gen = await client.artifacts.generate_video(
                    notebook_id, source_ids=source_ids, language=language, instructions=instructions, later=later
                )
                ext = ".mp4"
            elif kind == "cinematic_video":
                gen = await client.artifacts.generate_cinematic_video(
                    notebook_id, source_ids=source_ids, language=language, instructions=instructions
                )
                ext = ".mp4"
            elif kind == "slide_deck":
                gen = await client.artifacts.generate_slide_deck(
                    notebook_id, source_ids=source_ids, language=language, instructions=instructions, later=later
                )
                ext = ".pdf"
            elif kind == "quiz":
                gen = await client.artifacts.generate_quiz(
                    notebook_id, source_ids=source_ids, instructions=instructions, later=later
                )
                ext = ".md"
            elif kind == "flashcards":
                gen = await client.artifacts.generate_flashcards(
                    notebook_id, source_ids=source_ids, instructions=instructions, later=later
                )
                ext = ".md"
            elif kind == "infographic":
                gen = await client.artifacts.generate_infographic(
                    notebook_id, source_ids=source_ids, language=language, instructions=instructions, later=later
                )
                ext = ".png"
            elif kind == "report":
                gen = await client.artifacts.generate_report(
                    notebook_id,
                    report_format=ReportFormat.BRIEFING_DOC,
                    source_ids=source_ids,
                    language=language,
                    extra_instructions=instructions,
                    later=later,
                )
                ext = ".md"
            elif kind == "study_guide":
                gen = await client.artifacts.generate_study_guide(
                    notebook_id,
                    source_ids=source_ids,
                    language=language,
                    extra_instructions=instructions,
                    later=later,
                )
                ext = ".md"
            elif kind == "data_table":
                gen = await client.artifacts.generate_data_table(
                    notebook_id, source_ids=source_ids, language=language, instructions=instructions
                )
                ext = ".csv"
            else:
                raise ValueError(f"Unsupported Studio artifact kind: {kind}")

            if later or gen.is_scheduled:
                return {
                    "status": "deferred_later",
                    "remote_task_id": gen.task_id,
                    "local_drive_path": None,
                    "title": f"{prefix}{kind}_{stamp}",
                }

            final_status = await client.artifacts.wait_for_completion(notebook_id, gen.task_id, timeout=600.0)
            if final_status.is_failed:
                raise RuntimeError(
                    f"Generation failed ({final_status.error_code or 'ERROR'}): {final_status.error or 'unknown'}"
                )
            if not final_status.is_complete:
                raise RuntimeError(f"Generation ended in non-complete state: {final_status.status}")

            target = out_dir / f"{prefix}{kind}_{stamp}{ext}"
            artifact_id = final_status.task_id

            if kind == "audio":
                await client.artifacts.download_audio(notebook_id, str(target), artifact_id=artifact_id)
            elif kind in ("video", "cinematic_video"):
                await client.artifacts.download_video(notebook_id, str(target), artifact_id=artifact_id)
            elif kind == "slide_deck":
                await client.artifacts.download_slide_deck(
                    notebook_id, str(target), artifact_id=artifact_id, output_format="pdf"
                )
            elif kind == "quiz":
                await client.artifacts.download_quiz(
                    notebook_id, str(target), artifact_id=artifact_id, output_format="markdown"
                )
            elif kind == "flashcards":
                await client.artifacts.download_flashcards(
                    notebook_id, str(target), artifact_id=artifact_id, output_format="markdown"
                )
            elif kind == "infographic":
                await client.artifacts.download_infographic(notebook_id, str(target), artifact_id=artifact_id)
            elif kind in ("report", "study_guide"):
                await client.artifacts.download_report(notebook_id, str(target), artifact_id=artifact_id)
            elif kind == "data_table":
                await client.artifacts.download_data_table(notebook_id, str(target), artifact_id=artifact_id)

            return {
                "status": "done",
                "remote_task_id": artifact_id,
                "local_drive_path": str(target),
                "title": target.stem,
            }


_KNOWN_APP_HOSTS = frozenset(PERSONAL_APP_HOSTS | ENTERPRISE_APP_HOSTS)


def _extract_curl_context(unwrapped: str) -> tuple[str | None, int | None]:
    """Extract target NotebookLM host and ?authuser=N index from a pasted cURL command."""
    url_match = re.search(r"""(?:^|\s)--url\s+['"]?(https://[^\s'"]+)""", unwrapped, flags=re.IGNORECASE)
    candidates = [url_match.group(1)] if url_match else [m.group(0) for m in re.finditer(r"https://[^\s'\"]+", unwrapped, flags=re.IGNORECASE)]
    for candidate in candidates:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if host in _KNOWN_APP_HOSTS:
            raw_au = (parse_qs(parsed.query).get("authuser") or [None])[0]
            parsed_authuser = int(raw_au) if isinstance(raw_au, str) and raw_au.isdigit() else None
            return host, parsed_authuser
    return None, None


def _normalize_same_site(val: Any) -> str:
    s = str(val or "").strip().lower()
    if s in ("lax",):
        return "Lax"
    if s in ("strict",):
        return "Strict"
    return "None"


def _infer_cookie_defaults(name: str, value: str, *, app_host: str | None = None) -> dict[str, Any]:
    if name in ("OSID", "__Secure-OSID", "__Host-OSID"):
        domain = app_host or get_base_host()
        http_only = True
        secure = True
    elif name == "LSID" or name.startswith("__Host-"):
        domain = "accounts.google.com"
        http_only = True
        secure = True
    else:
        domain = ".google.com"
        http_only = name.startswith("__Secure-") or name in ("SID", "HSID", "SSID", "NID")
        secure = name.startswith("__Secure-") or name in ("SID", "SAPISID", "SSID")
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": -1,
        "httpOnly": http_only,
        "secure": secure,
        "sameSite": "None",
    }


def _parse_cookie_pairs(cookie_header: str, *, app_host: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for part in cookie_header.split(";"):
        seg = part.strip()
        if not seg or "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        name = k.strip()
        val = v.strip()
        if not name or not val:
            continue
        # Strip accidental surrounding quotes from cmd curl ^"foo^" if present
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        items.append(_infer_cookie_defaults(name, val, app_host=app_host))
    return items


def parse_raw_cookie_bundle(raw_text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse Chrome 'Copy as cURL' (bash/cmd), raw Cookie header, or JSON, returning (cookies, meta)."""
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("请粘贴 Copy as cURL、Cookie 字符串或导出的 JSON 内容")

    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
            raw_list = parsed.get("cookies") if isinstance(parsed, dict) else parsed
            if not isinstance(raw_list, list):
                raise ValueError("JSON 中未找到 cookies 数组")
            authuser: int | None = None
            if isinstance(parsed, dict):
                nb_meta = parsed.get("notebooklm")
                if isinstance(nb_meta, dict) and isinstance(nb_meta.get("account"), dict):
                    raw_au = nb_meta["account"].get("authuser")
                    if type(raw_au) is int and raw_au >= 0:
                        authuser = raw_au
            out: list[dict[str, Any]] = []
            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                value = str(item.get("value") or "")
                domain = str(item.get("domain") or "").strip()
                if not name or not domain:
                    continue
                exp = item.get("expires", item.get("expirationDate", -1))
                try:
                    exp_num = -1 if exp is None or float(exp) <= 0 else float(exp)
                except (TypeError, ValueError):
                    exp_num = -1
                out.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": domain,
                        "path": str(item.get("path") or "/"),
                        "expires": exp_num,
                        "httpOnly": bool(item.get("httpOnly", item.get("http_only", False))),
                        "secure": bool(item.get("secure", False)),
                        "sameSite": _normalize_same_site(item.get("sameSite")),
                    }
                )
            if out:
                return out, {"source_host": None, "authuser": authuser}
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 格式无效: {exc}") from exc

    # Unwrap bash `\` and Windows cmd `^` line continuations
    unwrapped = re.sub(r"[\^\\]\r?\n\s*", " ", text)
    if "^" in unwrapped and ("curl " in unwrapped.lower() or "^\"" in text):
        unwrapped = unwrapped.replace('^"', '"').replace("^^", "^")

    source_host, parsed_authuser = _extract_curl_context(unwrapped)

    cookie_hdr = ""
    for pat in (
        r"""(?:^|\s)(?:-H|--header)\s+(['"])\s*cookie\s*:\s*(.*?)\1""",
        r"""(?:^|\s)(?:-b|--cookie)\s+(['"])(.*?)\1""",
    ):
        m = re.search(pat, unwrapped, flags=re.IGNORECASE | re.DOTALL)
        if m:
            cookie_hdr = m.group(2).strip()
            break

    if not cookie_hdr:
        for line in unwrapped.splitlines():
            line_s = line.strip()
            if re.match(r"^cookie\s*:", line_s, flags=re.IGNORECASE):
                cookie_hdr = re.sub(r"^cookie\s*:\s*", "", line_s, flags=re.IGNORECASE)
                break

    if not cookie_hdr and "=" in unwrapped and not unwrapped.lower().startswith("curl "):
        cookie_hdr = re.sub(r"^cookie\s*:\s*", "", unwrapped, flags=re.IGNORECASE).strip()

    parsed_items = _parse_cookie_pairs(cookie_hdr, app_host=source_host) if cookie_hdr else []
    if not parsed_items:
        raise ValueError(
            "未能解析出任何 Cookie，请确认复制的是 notebook.google.com 或 notebooklm.google.com 的 cURL (bash/cmd) 或 Cookie 字符串"
        )
    return parsed_items, {"source_host": source_host, "authuser": parsed_authuser}


def parse_raw_cookie_input(raw_text: str) -> list[dict[str, Any]]:
    """Parse Chrome 'Copy as cURL' (bash/cmd), raw Cookie header, or Cookie-Editor/CDP JSON."""
    cookies, _ = parse_raw_cookie_bundle(raw_text)
    return cookies


def import_cookies_into_profile(
    profile_name: str,
    cookies: list[dict[str, Any]],
    *,
    email: str | None = None,
    authuser: int | None = None,
) -> dict[str, Any]:
    """Validate and save Google/NotebookLM cookies cleanly into ~/.notebooklm/profiles/<profile>/storage_state.json."""
    dedup: dict[tuple[str, str, str], dict[str, Any]] = {}
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        value = str(c.get("value") or "")
        domain = str(c.get("domain") or "").strip()
        path = str(c.get("path") or "/")
        if not name or not domain or not _is_allowed_auth_domain(domain):
            continue
        exp = c.get("expires", c.get("expirationDate", -1))
        try:
            exp_num = -1 if exp is None or float(exp) <= 0 else float(exp)
        except (TypeError, ValueError):
            exp_num = -1
        dedup[(name, domain, path)] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "expires": exp_num,
            "httpOnly": bool(c.get("httpOnly", c.get("http_only", False))),
            "secure": bool(c.get("secure", False)),
            "sameSite": _normalize_same_site(c.get("sameSite")),
        }

    normalized = list(dedup.values())
    names = {c["name"] for c in normalized}
    missing = set(MINIMUM_REQUIRED_COOKIES) - names
    if missing:
        raise ValueError(
            f"缺少关键 Google 认证 Cookie ({', '.join(sorted(missing))})。请先确认已在浏览器成功登录 https://notebook.google.com/ 并看到笔记本列表后再同步。"
        )

    has_sec = _has_valid_secondary_binding(names)
    storage_path = get_storage_path(profile=profile_name)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    clean_email = (email or "").strip() or None
    resolved_authuser = authuser if (isinstance(authuser, int) and authuser >= 0) else 0
    should_set = bool(clean_email or authuser is not None)
    replace_profile_from_login(
        storage_path,
        {"cookies": normalized, "origins": []},
        include_domains=None,
        account_mode="set" if should_set else "keep",
        account_authuser=resolved_authuser if should_set else None,
        account_email=clean_email,
    )
    return {
        "saved": True,
        "profile": profile_name,
        "cookie_count": len(normalized),
        "has_secondary_binding": has_sec,
        "authuser": resolved_authuser,
        "storage_path": str(storage_path),
    }


def launch_nlm_login(profile_name: str, *, browser: str = "chrome") -> dict[str, Any]:
    """Launch an interactive browser login window for a single isolated profile."""
    cmd = [sys.executable, "-m", "notebooklm", "-p", profile_name, "login", "--browser", browser]
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    proc = subprocess.Popen(cmd, creationflags=flags)
    return {
        "launched": True,
        "pid": proc.pid,
        "profile": profile_name,
        "browser": browser,
        "command": subprocess.list2cmdline(cmd),
        "message": f"已启动 {profile_name} 的 NotebookLM 登录会话窗口，请在新窗口中登录 Google 后等待自动保存。",
    }
