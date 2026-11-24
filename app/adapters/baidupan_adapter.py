"""Baidu Netdisk (百度网盘) async client & credential manager (Cookie/cURL/CDP + OpenAPI)."""

from __future__ import annotations

import asyncio
import json
import posixpath
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from ..config import get_baidupan_auth_path, get_chrome_profile_dir
from . import browser_takeover

WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
PCS_DOWNLOAD_USER_AGENT = "netdisk;P2SP;3.0.0.8;netdisk;11.12.3;PC;PC-Windows"
OPENAPI_DOWNLOAD_USER_AGENT = "pan.baidu.com"
BAIDU_CDP_PORT = 9390


class BaiduPanError(RuntimeError):
    """Raised when Baidu Netdisk API returns an error or auth is invalid."""

    def __init__(self, message: str, errno: int | None = None) -> None:
        super().__init__(message)
        self.errno = errno


@dataclass
class BaiduAuthState:
    mode: str = "cookie"  # "cookie" | "openapi"
    cookies: dict[str, str] = field(default_factory=dict)
    bdstoken: str = ""
    openapi_access_token: str = ""
    openapi_refresh_token: str = ""
    openapi_expires_at: float = 0.0
    user_info: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0

    @property
    def is_authenticated(self) -> bool:
        if self.mode == "openapi":
            return bool(self.openapi_access_token)
        return bool(self.cookies.get("BDUSS"))

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items() if v)


@dataclass
class PanFileItem:
    fs_id: int
    path: str
    server_filename: str
    size: int
    isdir: bool
    md5: str = ""
    category: int = 0
    server_mtime: int = 0
    server_ctime: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fs_id": self.fs_id,
            "path": self.path,
            "name": self.server_filename,
            "size": self.size,
            "isdir": self.isdir,
            "md5": self.md5,
            "category": self.category,
            "server_mtime": self.server_mtime,
            "server_ctime": self.server_ctime,
        }


def normalize_pan_path(path: str) -> str:
    if not path or not path.strip():
        return "/"
    p = path.strip().replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    norm = posixpath.normpath(p)
    return norm if norm.startswith("/") else "/" + norm


def extract_cookie_header_from_input(raw_text: str) -> str:
    """Extract raw cookie string from cURL (bash/cmd), Cookie header, or bare BDUSS."""
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Cookie / cURL 内容不能为空")

    unwrapped = re.sub(r"[\^\\]\r?\n\s*", " ", text)
    if "^" in unwrapped and ("curl " in unwrapped.lower() or '^\x22' in text or '^"' in text):
        unwrapped = unwrapped.replace('^"', '"').replace("^^", "^")

    for pat in (
        r"""(?:^|\s)(?:-H|--header)\s+(['"])\s*cookie\s*:\s*(.*?)\1""",
        r"""(?:^|\s)(?:-b|--cookie)\s+(['"])(.*?)\1""",
    ):
        m = re.search(pat, unwrapped, flags=re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(2).strip()

    for line in unwrapped.splitlines():
        line_s = line.strip()
        if re.match(r"^cookie\s*:", line_s, flags=re.IGNORECASE):
            return re.sub(r"^cookie\s*:\s*", "", line_s, flags=re.IGNORECASE).strip()

    return unwrapped


class BaiduAuthManager:
    """Manages Baidu Netdisk credentials (Cookie/cURL/CDP + optional OpenAPI OAuth)."""

    def __init__(self, auth_file: Path | None = None) -> None:
        self._explicit_auth_file = auth_file
        self._state: BaiduAuthState | None = None

    @property
    def auth_file(self) -> Path:
        return self._explicit_auth_file or get_baidupan_auth_path()

    def _load(self) -> BaiduAuthState:
        target = self.auth_file
        if not target.exists():
            return BaiduAuthState()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            return BaiduAuthState(
                mode=raw.get("mode", "cookie"),
                cookies=dict(raw.get("cookies") or {}),
                bdstoken=str(raw.get("bdstoken") or ""),
                openapi_access_token=str(raw.get("openapi_access_token") or ""),
                openapi_refresh_token=str(raw.get("openapi_refresh_token") or ""),
                openapi_expires_at=float(raw.get("openapi_expires_at") or 0.0),
                user_info=dict(raw.get("user_info") or {}),
                updated_at=float(raw.get("updated_at") or 0.0),
            )
        except Exception:  # noqa: BLE001
            return BaiduAuthState()

    def reload(self) -> BaiduAuthState:
        self._state = self._load()
        return self._state

    @property
    def state(self) -> BaiduAuthState:
        if self._state is None:
            self._state = self._load()
        return self._state

    def save(self) -> None:
        target = self.auth_file
        target.parent.mkdir(parents=True, exist_ok=True)
        st = self.state
        st.updated_at = time.time()
        tmp = target.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(asdict(st), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(target)

    def clear(self) -> None:
        self._state = BaiduAuthState()
        target = self.auth_file
        if target.exists():
            target.unlink()

    def import_cookie_string(self, raw_cookie: str, stoken: str = "") -> BaiduAuthState:
        """Parse Chrome cURL, Cookie header string, or bare BDUSS and persist it."""
        cleaned = extract_cookie_header_from_input(raw_cookie)
        st = self.reload()
        parsed: dict[str, str] = dict(st.cookies)

        if "=" not in cleaned and not cleaned.lower().startswith("curl "):
            parsed["BDUSS"] = cleaned
        else:
            for part in cleaned.split(";"):
                item = part.strip()
                if not item or "=" not in item:
                    continue
                k, v = item.split("=", 1)
                k = k.strip()
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                if k:
                    parsed[k] = v

        if stoken.strip():
            parsed["STOKEN"] = stoken.strip()

        if not parsed.get("BDUSS"):
            raise ValueError("未能在输入中解析出 BDUSS，请确认复制了 pan.baidu.com 的完整 cURL 或 Cookie 字符串。")

        st.mode = "cookie"
        st.cookies = parsed
        st.bdstoken = ""
        self._state = st
        self.save()
        return st

    def import_cdp_cookies(self, cookies: list[dict[str, Any]]) -> BaiduAuthState:
        """Filter Chrome/Edge CDP cookies for `.baidu.com` / `pan.baidu.com` and persist."""
        st = self.reload()
        cookie_map: dict[str, str] = {}
        for item in cookies:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or "").lower().lstrip(".")
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "")
            if not name or not value:
                continue
            if domain == "baidu.com" or domain.endswith(".baidu.com"):
                cookie_map[name] = value

        if not cookie_map.get("BDUSS"):
            raise ValueError(
                "未检测到百度网盘 BDUSS Cookie，请确认已在打开的 Chrome/Edge 窗口中登录并进入 pan.baidu.com 主页。"
            )

        st.mode = "cookie"
        st.cookies = cookie_map
        st.bdstoken = ""
        self._state = st
        self.save()
        return st

    def set_openapi_token(
        self,
        access_token: str,
        refresh_token: str = "",
        expires_in: int = 2592000,
    ) -> BaiduAuthState:
        """Persist official Baidu OpenAPI OAuth 2.0 tokens."""
        if not access_token.strip():
            raise ValueError("access_token 不能为空")
        st = self.reload()
        st.mode = "openapi"
        st.openapi_access_token = access_token.strip()
        if refresh_token.strip():
            st.openapi_refresh_token = refresh_token.strip()
        st.openapi_expires_at = time.time() + max(0, int(expires_in))
        self._state = st
        self.save()
        return st

    def update_user_context(
        self, bdstoken: str | None = None, user_info: dict[str, Any] | None = None
    ) -> None:
        st = self.state
        changed = False
        if bdstoken is not None and bdstoken != st.bdstoken:
            st.bdstoken = bdstoken
            changed = True
        if user_info is not None:
            st.user_info = user_info
            changed = True
        if changed:
            self.save()

    def status_summary(self) -> dict[str, Any]:
        st = self.reload()
        return {
            "authenticated": st.is_authenticated,
            "mode": st.mode,
            "has_bduss": bool(st.cookies.get("BDUSS")),
            "has_stoken": bool(st.cookies.get("STOKEN")),
            "has_bdstoken": bool(st.bdstoken),
            "has_openapi_token": bool(st.openapi_access_token),
            "user_info": st.user_info,
            "updated_at": st.updated_at,
        }


class BaiduPanClient:
    """Async client for Baidu Netdisk supporting Cookie (full-disk) and OpenAPI modes."""

    def __init__(self, auth: BaiduAuthManager | None = None) -> None:
        self.auth = auth or auth_manager
        self.request_timeout = 30.0
        self.download_timeout = 600.0
        self.download_chunk_size = 1024 * 1024

    def _require_auth(self) -> None:
        st = self.auth.reload()
        if not st.is_authenticated:
            raise BaiduPanError("尚未登录百度网盘。请先通过浏览器登录同步 Cookie，或粘贴 cURL/BDUSS。")

    def _web_headers(self) -> dict[str, str]:
        st = self.auth.state
        headers = {
            "User-Agent": WEB_USER_AGENT,
            "Referer": "https://pan.baidu.com/disk/main",
            "Accept": "application/json, text/plain, */*",
        }
        if st.mode == "cookie":
            headers["Cookie"] = st.cookie_header()
        return headers

    def _pcs_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        st = self.auth.state
        headers = {
            "User-Agent": OPENAPI_DOWNLOAD_USER_AGENT if st.mode == "openapi" else PCS_DOWNLOAD_USER_AGENT,
            "Referer": "https://pan.baidu.com/disk/main",
        }
        if st.mode == "cookie":
            headers["Cookie"] = st.cookie_header()
        if extra:
            headers.update(extra)
        return headers

    def _check_errno(self, data: dict[str, Any], action: str) -> dict[str, Any]:
        errno = data.get("errno")
        if errno is None:
            error_code = data.get("error_code")
            if error_code:
                raise BaiduPanError(
                    f"百度网盘 {action} 失败: error_code={error_code}, msg={data.get('error_msg')}",
                    errno=int(error_code),
                )
            return data
        if int(errno) != 0:
            hints = {
                -6: "登录态已失效或 Cookie 无效，请重新登录百度网盘",
                -7: "文件或目录名非法",
                -8: "文件或目录已存在",
                -9: "文件或目录不存在",
                2: "参数错误",
                111: "access_token 已过期，请重新授权",
                31023: "参数错误",
                31024: "没有访问权限",
                31066: "文件不存在",
            }
            hint = hints.get(int(errno), data.get("errmsg") or data.get("show_msg") or "未知错误")
            raise BaiduPanError(f"百度网盘 {action} 失败 (errno={errno}): {hint}", errno=int(errno))
        return data

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        action: str = "请求",
    ) -> dict[str, Any]:
        self._require_auth()
        async with httpx.AsyncClient(timeout=self.request_timeout, follow_redirects=True) as client:
            resp = await client.request(method, url, params=params, data=data, headers=self._web_headers())
            resp.raise_for_status()
            try:
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                raise BaiduPanError(
                    f"百度网盘 {action} 返回非 JSON 响应 (HTTP {resp.status_code})，可能登录已失效。"
                ) from exc
        return self._check_errno(payload, action)

    async def get_user_info(self) -> dict[str, Any]:
        self._require_auth()
        st = self.auth.state
        if st.mode == "openapi":
            data = await self._request_json(
                "GET",
                "https://pan.baidu.com/rest/2.0/xpan/nas",
                params={"method": "uinfo", "access_token": st.openapi_access_token},
                action="获取用户信息",
            )
            info = {
                "baidu_name": data.get("baidu_name", ""),
                "netdisk_name": data.get("netdisk_name", ""),
                "uk": data.get("uk"),
                "vip_type": data.get("vip_type", 0),
                "avatar_url": data.get("avatar_url", ""),
            }
            self.auth.update_user_context(user_info=info)
            return info

        tpl = await self._request_json(
            "GET",
            "https://pan.baidu.com/api/gettemplatevariable",
            params={
                "fields": json.dumps(["bdstoken", "token", "uk", "isdocuser", "username"]),
                "channel": "chunlei",
                "web": 1,
                "app_id": 250528,
            },
            action="获取账号状态",
        )
        result = tpl.get("result") or {}
        bdstoken = str(result.get("bdstoken") or "")
        login_status: dict[str, Any] = {}
        try:
            ls = await self._request_json(
                "GET",
                "https://pan.baidu.com/api/loginStatus",
                params={"clienttype": 0, "app_id": 250528, "web": 1},
                action="获取登录详情",
            )
            login_status = ls.get("login_info") or {}
        except Exception:  # noqa: BLE001
            pass

        info = {
            "baidu_name": login_status.get("username") or result.get("username") or "",
            "uk": login_status.get("uk") or result.get("uk"),
            "vip_type": login_status.get("vip_type", 0),
            "avatar_url": login_status.get("photo", ""),
            "has_bdstoken": bool(bdstoken),
        }
        self.auth.update_user_context(bdstoken=bdstoken or None, user_info=info)
        return info

    async def ensure_bdstoken(self) -> str:
        st = self.auth.reload()
        if st.mode == "openapi":
            return ""
        if st.bdstoken:
            return st.bdstoken
        await self.get_user_info()
        token = self.auth.state.bdstoken
        if not token:
            raise BaiduPanError("无法获取 bdstoken，请确认 Cookie 包含有效的 BDUSS 和 STOKEN。")
        return token

    async def get_quota(self) -> dict[str, Any]:
        self._require_auth()
        st = self.auth.state
        params: dict[str, Any] = {"checkfree": 1, "checkexpire": 1}
        if st.mode == "openapi":
            params["access_token"] = st.openapi_access_token
        data = await self._request_json("GET", "https://pan.baidu.com/api/quota", params=params, action="查询网盘容量")
        total = int(data.get("total", 0))
        used = int(data.get("used", 0))
        free = max(0, total - used)
        return {
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "total_gb": round(total / (1024**3), 2),
            "used_gb": round(used / (1024**3), 2),
            "free_gb": round(free / (1024**3), 2),
            "used_percent": round((used / total) * 100, 2) if total > 0 else 0.0,
        }

    def _parse_item(self, raw: dict[str, Any]) -> PanFileItem:
        return PanFileItem(
            fs_id=int(raw.get("fs_id", 0)),
            path=normalize_pan_path(str(raw.get("path", "/"))),
            server_filename=str(raw.get("server_filename") or posixpath.basename(str(raw.get("path", "")))),
            size=int(raw.get("size", 0)),
            isdir=bool(int(raw.get("isdir", 0))),
            md5=str(raw.get("md5") or ""),
            category=int(raw.get("category", 0)),
            server_mtime=int(raw.get("server_mtime", 0)),
            server_ctime=int(raw.get("server_ctime", 0)),
        )

    async def list_files(
        self,
        path: str = "/",
        *,
        order: str = "name",
        desc: bool = False,
        start: int = 0,
        limit: int = 200,
    ) -> list[PanFileItem]:
        self._require_auth()
        st = self.auth.state
        norm_path = normalize_pan_path(path)
        limit = max(1, min(int(limit), 1000))
        start = max(0, int(start))

        if st.mode == "openapi":
            data = await self._request_json(
                "GET",
                "https://pan.baidu.com/rest/2.0/xpan/file",
                params={
                    "method": "list",
                    "access_token": st.openapi_access_token,
                    "dir": norm_path,
                    "order": order,
                    "desc": 1 if desc else 0,
                    "start": start,
                    "limit": limit,
                    "web": "web",
                },
                action=f"列出目录 {norm_path}",
            )
        else:
            data = await self._request_json(
                "GET",
                "https://pan.baidu.com/api/list",
                params={
                    "dir": norm_path,
                    "order": order,
                    "desc": 1 if desc else 0,
                    "start": start,
                    "limit": limit,
                    "web": 1,
                    "showempty": 0,
                    "channel": "chunlei",
                    "app_id": 250528,
                },
                action=f"列出目录 {norm_path}",
            )

        items = [self._parse_item(raw) for raw in (data.get("list") or [])]
        if order == "name" and not desc:
            items.sort(key=lambda x: (0 if x.isdir else 1, x.server_filename.lower()))
        return items

    async def list_all_files(self, path: str = "/") -> list[PanFileItem]:
        all_items: list[PanFileItem] = []
        start = 0
        page_size = 500
        while True:
            batch = await self.list_files(path, start=start, limit=page_size)
            all_items.extend(batch)
            if len(batch) < page_size:
                break
            start += len(batch)
        return all_items

    async def walk_files(self, path: str) -> list[PanFileItem]:
        """Recursively collect all non-directory files under a Baidu Pan path."""
        norm = normalize_pan_path(path)
        if norm != "/":
            parent = posixpath.dirname(norm)
            parent_entries = await self.list_all_files(parent)
            match = next((item for item in parent_entries if item.path == norm), None)
            if match is None:
                raise BaiduPanError(f"路径不存在: {norm}", errno=-9)
            if not match.isdir:
                return [match]

        collected: list[PanFileItem] = []
        queue: list[str] = [norm]
        while queue:
            current_dir = queue.pop(0)
            entries = await self.list_all_files(current_dir)
            for item in entries:
                if item.isdir:
                    queue.append(item.path)
                else:
                    collected.append(item)
        return collected

    async def search_files(
        self,
        keyword: str,
        *,
        dir_path: str = "/",
        recursion: bool = True,
        page: int = 1,
        num: int = 100,
    ) -> list[PanFileItem]:
        self._require_auth()
        st = self.auth.state
        norm_dir = normalize_pan_path(dir_path)
        num = max(1, min(int(num), 500))
        page = max(1, int(page))

        if st.mode == "openapi":
            data = await self._request_json(
                "GET",
                "https://pan.baidu.com/rest/2.0/xpan/file",
                params={
                    "method": "search",
                    "access_token": st.openapi_access_token,
                    "key": keyword,
                    "dir": norm_dir,
                    "recursion": 1 if recursion else 0,
                    "page": page,
                    "num": num,
                    "web": 1,
                },
                action=f"搜索 '{keyword}'",
            )
        else:
            data = await self._request_json(
                "GET",
                "https://pan.baidu.com/api/search",
                params={
                    "key": keyword,
                    "dir": norm_dir,
                    "recursion": 1 if recursion else 0,
                    "page": page,
                    "num": num,
                    "web": 1,
                    "channel": "chunlei",
                    "app_id": 250528,
                },
                action=f"搜索 '{keyword}'",
            )
        return [self._parse_item(raw) for raw in (data.get("list") or [])]

    async def get_file_metas(
        self,
        *,
        paths: list[str] | None = None,
        fs_ids: list[int] | None = None,
        dlink: bool = True,
    ) -> list[dict[str, Any]]:
        self._require_auth()
        st = self.auth.state
        if st.mode == "openapi":
            if not fs_ids:
                raise BaiduPanError("OpenAPI 模式查询 filemetas 需要提供 fs_ids。")
            data = await self._request_json(
                "GET",
                "https://pan.baidu.com/rest/2.0/xpan/multimedia",
                params={
                    "method": "filemetas",
                    "access_token": st.openapi_access_token,
                    "fsids": json.dumps(fs_ids),
                    "dlink": 1 if dlink else 0,
                },
                action="获取文件元信息",
            )
            return list(data.get("list") or [])

        params: dict[str, Any] = {
            "dlink": 1 if dlink else 0,
            "channel": "chunlei",
            "web": 1,
            "app_id": 250528,
        }
        if paths:
            params["target"] = json.dumps([normalize_pan_path(p) for p in paths])
        elif fs_ids:
            params["fsids"] = json.dumps(fs_ids)
        else:
            raise BaiduPanError("请提供 paths 或 fs_ids。")

        data = await self._request_json(
            "GET", "https://pan.baidu.com/api/filemetas", params=params, action="获取文件元信息"
        )
        return list(data.get("info") or data.get("list") or [])

    async def create_directory(self, path: str) -> dict[str, Any]:
        self._require_auth()
        st = self.auth.state
        norm_path = normalize_pan_path(path)
        if st.mode == "openapi":
            return await self._request_json(
                "POST",
                "https://pan.baidu.com/rest/2.0/xpan/file",
                params={"method": "create", "access_token": st.openapi_access_token},
                data={"path": norm_path, "size": 0, "isdir": 1},
                action=f"创建目录 {norm_path}",
            )
        bdstoken = await self.ensure_bdstoken()
        return await self._request_json(
            "POST",
            "https://pan.baidu.com/api/create",
            params={"a": "commit", "channel": "chunlei", "web": 1, "app_id": 250528, "bdstoken": bdstoken},
            data={"path": norm_path, "isdir": 1, "block_list": "[]"},
            action=f"创建目录 {norm_path}",
        )

    async def delete_files(self, paths: list[str]) -> dict[str, Any]:
        self._require_auth()
        st = self.auth.state
        filelist = [normalize_pan_path(p) for p in paths]
        if st.mode == "openapi":
            return await self._request_json(
                "POST",
                "https://pan.baidu.com/rest/2.0/xpan/file",
                params={"method": "filemanager", "access_token": st.openapi_access_token, "opera": "delete"},
                data={"async": 2, "filelist": json.dumps(filelist, ensure_ascii=False), "ondup": "newcopy"},
                action=f"删除 {len(filelist)} 项",
            )
        bdstoken = await self.ensure_bdstoken()
        return await self._request_json(
            "POST",
            "https://pan.baidu.com/api/filemanager",
            params={"opera": "delete", "async": 2, "onnest": "fail", "channel": "chunlei", "web": 1, "app_id": 250528, "bdstoken": bdstoken},
            data={"filelist": json.dumps(filelist, ensure_ascii=False)},
            action=f"删除 {len(filelist)} 项",
        )

    async def upload_file(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        progress_cb: Callable[[int, int], Any] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Upload a local file to Baidu Netdisk via PCS direct upload."""
        self._require_auth()
        if cancel_check and cancel_check():
            raise asyncio.CancelledError("Upload cancelled by user")

        st = self.auth.state
        src = Path(local_path)
        if not src.is_file():
            raise FileNotFoundError(f"本地文件不存在: {src}")

        total_bytes = src.stat().st_size
        norm_remote = normalize_pan_path(remote_path)
        params: dict[str, Any] = {
            "method": "upload",
            "app_id": 250528,
            "path": norm_remote,
            "ondup": "overwrite",
        }
        if st.mode == "openapi":
            params["access_token"] = st.openapi_access_token

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=self.download_timeout, write=self.download_timeout, pool=30.0),
            follow_redirects=True,
        ) as client:
            with src.open("rb") as fh:
                resp = await client.post(
                    "https://d.pcs.baidu.com/rest/2.0/pcs/file",
                    params=params,
                    headers=self._pcs_headers(),
                    files={"file": (src.name, fh, "application/octet-stream")},
                )
            resp.raise_for_status()
            data = resp.json()

        checked = self._check_errno(data, f"上传 {norm_remote}")
        if progress_cb:
            maybe = progress_cb(total_bytes, total_bytes)
            if hasattr(maybe, "__await__"):
                await maybe
        return checked

    async def resolve_download_urls(self, remote_path: str, fs_id: int | None = None) -> list[str]:
        self._require_auth()
        st = self.auth.state
        norm_path = normalize_pan_path(remote_path)
        urls: list[str] = []

        if st.mode == "openapi":
            if fs_id is None:
                parent = posixpath.dirname(norm_path)
                entries = await self.list_all_files(parent)
                match = next((e for e in entries if e.path == norm_path), None)
                if not match:
                    raise BaiduPanError(f"找不到文件: {norm_path}", errno=-9)
                fs_id = match.fs_id
            metas = await self.get_file_metas(fs_ids=[fs_id], dlink=True)
            for m in metas:
                dlink = m.get("dlink")
                if dlink:
                    sep = "&" if "?" in dlink else "?"
                    urls.append(f"{dlink}{sep}access_token={st.openapi_access_token}")
            return urls

        try:
            async with httpx.AsyncClient(timeout=self.request_timeout, follow_redirects=True) as client:
                resp = await client.get(
                    "https://d.pcs.baidu.com/rest/2.0/pcs/file",
                    params={"method": "locatedownload", "app_id": 250528, "ver": "4.0", "path": norm_path},
                    headers=self._pcs_headers(),
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    for entry in payload.get("urls") or []:
                        if entry.get("url"):
                            urls.append(str(entry["url"]))
        except Exception:  # noqa: BLE001
            pass

        urls.append(
            "https://d.pcs.baidu.com/rest/2.0/pcs/file"
            f"?method=download&app_id=250528&path={quote(norm_path, safe='/')}"
        )

        try:
            metas = await self.get_file_metas(paths=[norm_path], dlink=True)
            for m in metas:
                if m.get("dlink"):
                    urls.append(str(m["dlink"]))
        except Exception:  # noqa: BLE001
            pass
        return urls

    async def download_file(
        self,
        remote_path: str,
        dest_path: str | Path,
        *,
        fs_id: int | None = None,
        expected_size: int | None = None,
        progress_cb: Callable[[int, int], Any] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Stream-download a file from Baidu Netdisk with `.bdpart` resume and fallback URLs."""
        self._require_auth()
        norm_path = normalize_pan_path(remote_path)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part_file = dest.with_name(dest.name + ".bdpart")

        candidate_urls = await self.resolve_download_urls(norm_path, fs_id=fs_id)
        if not candidate_urls:
            raise BaiduPanError(f"无法解析下载地址: {norm_path}")

        last_error: Exception | None = None
        for url in candidate_urls:
            try:
                existing_bytes = part_file.stat().st_size if part_file.exists() else 0
                extra_headers = {"Range": f"bytes={existing_bytes}-"} if existing_bytes > 0 else {}
                started_at = time.time()

                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=30.0, read=self.download_timeout, write=30.0, pool=30.0),
                    follow_redirects=True,
                ) as client:
                    async with client.stream("GET", url, headers=self._pcs_headers(extra_headers)) as resp:
                        if resp.status_code == 416 and existing_bytes > 0:
                            part_file.replace(dest)
                            return {
                                "remote_path": norm_path,
                                "local_path": str(dest),
                                "bytes": dest.stat().st_size,
                                "elapsed_sec": round(time.time() - started_at, 2),
                            }
                        if resp.status_code not in (200, 206):
                            preview = (await resp.aread())[:300].decode("utf-8", errors="ignore")
                            raise BaiduPanError(f"下载失败 HTTP {resp.status_code}: {preview}")

                        if resp.status_code == 206:
                            mode = "ab"
                            downloaded = existing_bytes
                            content_len = int(resp.headers.get("Content-Length") or 0)
                            total_bytes = expected_size or (existing_bytes + content_len if content_len else 0)
                        else:
                            mode = "wb"
                            downloaded = 0
                            content_len = int(resp.headers.get("Content-Length") or 0)
                            total_bytes = expected_size or content_len

                        last_report = 0.0
                        with part_file.open(mode) as fh:
                            async for chunk in resp.aiter_bytes(chunk_size=self.download_chunk_size):
                                if cancel_check and cancel_check():
                                    raise asyncio.CancelledError("Download cancelled by user")
                                if not chunk:
                                    continue
                                fh.write(chunk)
                                downloaded += len(chunk)
                                now = time.time()
                                if progress_cb and (now - last_report >= 0.35 or (total_bytes and downloaded >= total_bytes)):
                                    last_report = now
                                    maybe = progress_cb(downloaded, total_bytes)
                                    if hasattr(maybe, "__await__"):
                                        await maybe

                final_size = part_file.stat().st_size
                if expected_size and expected_size > 0 and final_size != expected_size:
                    raise BaiduPanError(f"下载字节数不完整 ({final_size}/{expected_size})，将尝试备用通道")

                part_file.replace(dest)
                if progress_cb:
                    maybe = progress_cb(final_size, final_size)
                    if hasattr(maybe, "__await__"):
                        await maybe

                return {
                    "remote_path": norm_path,
                    "local_path": str(dest),
                    "bytes": final_size,
                    "elapsed_sec": round(time.time() - started_at, 2),
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue

        raise BaiduPanError(f"所有下载通道均失败 ({norm_path}): {last_error}")


def open_baidu_login_browser(browser: str = "chrome", cdp_port: int = BAIDU_CDP_PORT) -> dict[str, Any]:
    """Launch an isolated Chrome/Edge window on pan.baidu.com for QR or password login."""
    profile_dir = get_chrome_profile_dir("baidupan_bridge")
    return browser_takeover.open_account_browser(
        alias="baidupan",
        chrome_profile_dir=profile_dir,
        cdp_port=cdp_port,
        url="https://pan.baidu.com/disk/main",
        browser=browser,
    )


async def sync_baidu_cdp_cookies(
    *, auth: BaiduAuthManager | None = None, cdp_port: int = BAIDU_CDP_PORT
) -> dict[str, Any]:
    """Capture .baidu.com cookies via CDP without closing the browser window."""
    manager = auth or auth_manager
    cookies = await browser_takeover.capture_cdp_cookies(cdp_port)
    manager.import_cdp_cookies(cookies)
    return manager.status_summary()


auth_manager = BaiduAuthManager()
baidu_client = BaiduPanClient(auth=auth_manager)
