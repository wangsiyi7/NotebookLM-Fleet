"""Capability Router: dispatches between Channel 1 (Official API) and Channel 2 (notebooklm-py)."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Any

from . import nlm_py_adapter, official_api

logger = logging.getLogger(__name__)

_OFFICIAL_TEXT_EXTS = frozenset(
    {
        ".md",
        ".txt",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".html",
        ".htm",
        ".css",
        ".json",
        ".sql",
        ".yaml",
        ".yml",
        ".csv",
        ".toml",
        ".ps1",
        ".sh",
        ".xml",
        ".ini",
        ".cfg",
        ".log",
    }
)


class CapabilityRouter:
    """Routes NotebookLM operations to Official API where supported, with automatic Ch2 fallback."""

    def __init__(
        self,
        settings_map: dict[str, str],
        *,
        official: Any = official_api,
        nlm: Any = nlm_py_adapter,
    ) -> None:
        self.settings = settings_map
        self.official = official
        self.nlm = nlm

    def _proxy_kwargs(self) -> dict[str, Any]:
        proxy = (self.settings.get("official_proxy_url") or "").strip()
        return {"proxy": proxy} if proxy else {}

    def can_use_official(self, account: dict[str, Any]) -> bool:
        enabled = str(self.settings.get("official_api_enabled", "false")).lower() == "true"
        project_id = (self.settings.get("official_api_project_id") or "").strip()
        oauth_ok = account.get("official_oauth_status") == "ok"
        return enabled and bool(project_id) and oauth_ok

    async def list_notebooks(self, account: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        if self.can_use_official(account):
            try:
                project_id = self.settings["official_api_project_id"].strip()
                items = await self.official.list_notebooks(
                    account["alias"], project_id, **self._proxy_kwargs()
                )
                return items, "official_api"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Official list_notebooks failed for %s, falling back to Ch2: %s", account["alias"], exc)
        items = await self.nlm.list_notebooks(account["nlm_profile"])
        return items, "notebooklm_py"

    async def create_notebook(self, account: dict[str, Any], title: str) -> tuple[dict[str, Any], str]:
        if self.can_use_official(account):
            try:
                project_id = self.settings["official_api_project_id"].strip()
                nb = await self.official.create_notebook(
                    account["alias"], project_id, title, **self._proxy_kwargs()
                )
                return nb, "official_api"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Official create_notebook failed for %s, falling back to Ch2: %s", account["alias"], exc)
        nb = await self.nlm.create_notebook(account["nlm_profile"], title)
        return nb, "notebooklm_py"

    async def ingest_url(
        self, account: dict[str, Any], notebook_id: str, url: str, *, title: str | None = None
    ) -> tuple[dict[str, Any], str]:
        if self.can_use_official(account):
            try:
                project_id = self.settings["official_api_project_id"].strip()
                res = await self.official.create_source_url(
                    account["alias"], project_id, notebook_id, url, title, **self._proxy_kwargs()
                )
                return res, "official_api"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Official create_source_url failed for %s, falling back to Ch2: %s", account["alias"], exc)
        res = await self.nlm.add_url(account["nlm_profile"], notebook_id, url, title=title, wait=True)
        return res, "notebooklm_py"

    async def ingest_text(
        self, account: dict[str, Any], notebook_id: str, title: str, text: str
    ) -> tuple[dict[str, Any], str]:
        if self.can_use_official(account):
            try:
                project_id = self.settings["official_api_project_id"].strip()
                res = await self.official.create_source_text(
                    account["alias"], project_id, notebook_id, title, text, **self._proxy_kwargs()
                )
                return res, "official_api"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Official create_source_text failed for %s, falling back to Ch2: %s", account["alias"], exc)
        res = await self.nlm.add_text(account["nlm_profile"], notebook_id, title, text, wait=True)
        return res, "notebooklm_py"

    async def ingest_file(
        self, account: dict[str, Any], notebook_id: str, file_path: str, *, title: str | None = None
    ) -> tuple[dict[str, Any], str]:
        if self.can_use_official(account):
            fpath = Path(file_path)
            project_id = self.settings["official_api_project_id"].strip()
            pkw = self._proxy_kwargs()
            try:
                ext = fpath.suffix.lower()
                size = (await asyncio.to_thread(fpath.stat)).st_size
                if (ext in _OFFICIAL_TEXT_EXTS or fpath.name.lower() == ".env.example") and size <= 900_000:
                    text_body = await asyncio.to_thread(fpath.read_text, "utf-8", "replace")
                    text_mime = "text/markdown" if ext == ".md" else "text/plain"
                    res = await self.official.create_source_text(
                        account["alias"],
                        project_id,
                        notebook_id,
                        title or fpath.name,
                        text_body,
                        mime_type=text_mime,
                        **pkw,
                    )
                else:
                    mime, _ = mimetypes.guess_type(fpath.name)
                    res = await self.official.upload_source_file(
                        account["alias"],
                        project_id,
                        notebook_id,
                        fpath,
                        display_name=title or fpath.name,
                        mime_type=mime,
                        **pkw,
                    )
                return res, "official_api"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Official ingest_file failed for %s (%s): %s", account["alias"], fpath.name, exc)
                if account.get("nlm_auth_status") != "ok":
                    raise
        res = await self.nlm.add_file(account["nlm_profile"], notebook_id, file_path, title=title, wait=True)
        return res, "notebooklm_py"

    async def ingest_drive(
        self,
        account: dict[str, Any],
        notebook_id: str,
        file_id: str,
        title: str,
        *,
        mime_type: str = "google-doc",
        upload_only: bool = False,
    ) -> tuple[dict[str, Any], str]:
        if upload_only:
            res = await self.nlm.add_drive_file(account["nlm_profile"], notebook_id, file_id, title=title, wait=True)
        else:
            res = await self.nlm.add_drive(
                account["nlm_profile"], notebook_id, file_id, title, mime_type=mime_type, wait=True
            )
        return res, "notebooklm_py"

    async def share_editor(
        self, account: dict[str, Any], notebook_id: str, target_email: str, *, notify: bool = False
    ) -> tuple[dict[str, Any], str]:
        res = await self.nlm.share_editor(account["nlm_profile"], notebook_id, target_email, notify=notify)
        return res, "notebooklm_py"

    async def ask(
        self,
        account: dict[str, Any],
        notebook_id: str,
        question: str,
        *,
        source_ids: list[str] | None = None,
        save_as_note: bool = False,
        note_title: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        res = await self.nlm.ask_question(
            account["nlm_profile"],
            notebook_id,
            question,
            source_ids=source_ids,
            save_as_note=save_as_note,
            note_title=note_title,
        )
        return res, "notebooklm_py"

    async def generate_and_download(
        self,
        account: dict[str, Any],
        notebook_id: str,
        kind: str,
        output_dir: str,
        *,
        instructions: str | None = None,
        language: str = "zh-Hans",
        later: bool = False,
        title_prefix: str = "",
    ) -> tuple[dict[str, Any], str]:
        res = await self.nlm.generate_and_download(
            account["nlm_profile"],
            notebook_id,
            kind,
            output_dir,
            instructions=instructions,
            language=language,
            later=later,
            title_prefix=title_prefix,
        )
        return res, "notebooklm_py"
