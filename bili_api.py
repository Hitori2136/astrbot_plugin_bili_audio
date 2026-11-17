"""B 站官方接口客户端：视频信息、播放地址与流式下载。

只请求 api.bilibili.com 与 *.bilivideo.com，不经过任何第三方解析站点。
``httpx`` 只在运行时使用（插件环境由 AstrBot 提供）；本模块可以被没有
``httpx`` 的环境导入，方便离线测试。

这里刻意走 ``platform=html5`` 的播放接口：它返回单文件 mp4（``durl``），
不要求 WBI 签名，也不做地区判定，因此比 PC 端 DASH 接口稳定得多。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:  # pragma: no cover - 缺少 httpx 时模块仍可导入
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
PLAYURL_URL = "https://api.bilibili.com/x/player/playurl"
BUVID_URL = "https://api.bilibili.com/x/frontend/finger/spi"

# 接口层拒绝：换设备指纹后重试才有意义。
RETRYABLE_CODES = frozenset({-352, -403, -412, 567})
# 视频本身不可用：重试没有意义。
FATAL_CODES = frozenset({-404, 62002, 62004, 62012})
# 地区/版权限制。
REGION_CODES = frozenset({-10403, 87008})

BUVID_MAX_AGE_SECONDS = 30 * 24 * 3600
STATE_FILE_LIMIT = 8192
CHUNK_SIZE = 256 * 1024
BUVID_VALUE_LIMIT = 256


class BiliApiError(Exception):
    """B 站接口返回了非 0 业务码，或响应无法解析。"""

    def __init__(
        self,
        code: Any,
        message: str = "",
        *,
        stage: str = "接口请求",
    ) -> None:
        self.code = code
        self.message = message
        self.stage = stage
        detail = f"，{message}" if message else ""
        super().__init__(f"{stage}失败（code={code}{detail}）")

    def _numeric(self) -> int | None:
        try:
            return int(self.code)
        except (TypeError, ValueError):
            return None

    @property
    def numeric_code(self) -> int | None:
        return self._numeric()

    @property
    def retryable(self) -> bool:
        return self._numeric() in RETRYABLE_CODES

    @property
    def fatal(self) -> bool:
        return self._numeric() in FATAL_CODES

    @property
    def region_limited(self) -> bool:
        return self._numeric() in REGION_CODES


class BiliDownloadError(Exception):
    """播放地址下载失败（网络、HTTP 状态或文件体积）。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        too_large: bool = False,
    ) -> None:
        self.status = status
        self.too_large = too_large
        super().__init__(message)


@dataclass(frozen=True)
class PlayInfo:
    """播放地址与接口给出的文件体积（未知时为 None）。"""

    urls: tuple[str, ...]
    total_size: int | None = None


def build_client(*, proxy: str, timeout: Any) -> Any:
    """按配置创建 httpx 客户端；只认显式代理，不读取环境变量。"""
    if httpx is None:  # pragma: no cover - 正常环境不会走到
        raise RuntimeError("缺少 httpx，无法使用 B 站官方接口")
    options: dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": True,
        "trust_env": False,
        "headers": {"Accept-Encoding": "identity"},
    }
    if proxy:
        try:
            return httpx.AsyncClient(proxy=proxy, **options)
        except TypeError:  # httpx < 0.26 使用 proxies
            return httpx.AsyncClient(proxies=proxy, **options)
    return httpx.AsyncClient(**options)


async def _get_json(
    client: Any,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    stage: str,
) -> dict[str, Any]:
    try:
        response = await client.get(url, params=params, headers=headers)
    except Exception as exc:
        raise BiliApiError(None, f"网络请求失败：{exc}", stage=stage) from exc

    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        raise BiliApiError(status, f"HTTP {status}", stage=stage)

    try:
        payload = response.json()
    except Exception as exc:
        raise BiliApiError(None, "返回内容不是 JSON", stage=stage) from exc
    if not isinstance(payload, dict):
        raise BiliApiError(None, "返回结构异常", stage=stage)

    code = payload.get("code")
    if code != 0:
        raise BiliApiError(code, str(payload.get("message") or ""), stage=stage)

    data = payload.get("data")
    return data if isinstance(data, dict) else {}


async def fetch_video(
    client: Any,
    bvid: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """获取视频元数据（标题、时长、封面、第一个分P 的 cid）。"""
    return await _get_json(
        client,
        VIEW_URL,
        params={"bvid": bvid},
        headers=headers,
        stage="获取视频信息",
    )


async def fetch_play_info(
    client: Any,
    bvid: str,
    cid: int,
    *,
    headers: Mapping[str, str] | None = None,
) -> PlayInfo:
    """取 html5 播放接口的直链，附带备用 CDN 地址与文件体积。"""
    data = await _get_json(
        client,
        PLAYURL_URL,
        params={
            "bvid": bvid,
            "cid": cid,
            "qn": 16,
            "type": "mp4",
            "platform": "html5",
        },
        headers=headers,
        stage="获取播放地址",
    )

    urls: list[str] = []
    total = 0
    size_unknown = False
    for item in data.get("durl") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if url.startswith("https://"):
            urls.append(url)
        for backup in item.get("backup_url") or []:
            if isinstance(backup, str) and backup.startswith("https://"):
                urls.append(backup)
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > 0:
            total += size
        else:
            size_unknown = True
    if not urls:
        raise BiliApiError(None, "没有返回可用的播放地址", stage="获取播放地址")
    return PlayInfo(tuple(urls), None if size_unknown or not total else total)


async def fetch_buvid(
    client: Any,
    *,
    headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """取设备指纹 Cookie，让多次请求看起来来自同一台设备。"""
    data = await _get_json(
        client,
        BUVID_URL,
        headers=headers,
        stage="获取设备指纹",
    )
    values: dict[str, str] = {}
    for key, name in (("b_3", "buvid3"), ("b_4", "buvid4")):
        value = str(data.get(key) or "").strip()
        if value:
            values[name] = value
    return values


async def download_media(
    client: Any,
    urls: list[str],
    dest: Path,
    *,
    headers: Mapping[str, str] | None = None,
    max_bytes: int,
    deadline: float,
) -> int:
    """依次尝试直链与备用地址，成功返回写入字节数。"""
    last: BiliDownloadError | None = None
    for url in urls:
        try:
            return await _download_one(
                client,
                url,
                dest,
                headers=headers,
                max_bytes=max_bytes,
                deadline=deadline,
            )
        except BiliDownloadError as exc:
            last = exc
            _remove_quietly(dest)
            if exc.too_large:
                raise
    raise last or BiliDownloadError("没有可用的播放地址")


async def _download_one(
    client: Any,
    url: str,
    dest: Path,
    *,
    headers: Mapping[str, str] | None,
    max_bytes: int,
    deadline: float,
) -> int:
    part = dest.with_name(dest.name + ".part")
    written = 0
    try:
        async with client.stream("GET", url, headers=headers) as response:
            status = int(getattr(response, "status_code", 0) or 0)
            if status != 200:
                raise BiliDownloadError(f"CDN 返回 HTTP {status}", status=status)

            total = _content_length(response)
            if total is not None and total > max_bytes:
                raise BiliDownloadError(
                    f"源文件 {_format_mb(total)} 超过 {_format_mb(max_bytes)} 上限",
                    too_large=True,
                )

            with open(part, "wb") as handle:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise BiliDownloadError(
                            f"源文件超过 {_format_mb(max_bytes)} 上限",
                            too_large=True,
                        )
                    if deadline and time.monotonic() > deadline:
                        raise BiliDownloadError("下载超时，请稍后重试")
                    handle.write(chunk)
    except BiliDownloadError:
        _remove_quietly(part)
        raise
    except Exception as exc:
        _remove_quietly(part)
        raise BiliDownloadError(f"下载中断：{exc}") from exc

    if written <= 0:
        _remove_quietly(part)
        raise BiliDownloadError("下载到的文件为空")

    try:
        os.replace(part, dest)
    except OSError as exc:
        _remove_quietly(part)
        raise BiliDownloadError(f"保存文件失败：{exc}") from exc
    return written


def load_buvid(path: Path | None) -> dict[str, str]:
    """读取本地缓存的设备指纹，过期或损坏时返回空。"""
    if path is None:
        return {}
    try:
        if path.stat().st_size > STATE_FILE_LIMIT:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}

    try:
        saved_at = float(payload.get("saved_at") or 0)
    except (TypeError, ValueError):
        saved_at = 0.0
    if saved_at and time.time() - saved_at > BUVID_MAX_AGE_SECONDS:
        return {}

    values: dict[str, str] = {}
    for name in ("buvid3", "buvid4"):
        value = str(payload.get(name) or "").strip()
        if (
            value
            and len(value) <= BUVID_VALUE_LIMIT
            and value.isprintable()
        ):
            values[name] = value
    return values


def save_buvid(path: Path | None, values: Mapping[str, str]) -> None:
    """尽力持久化设备指纹；失败不影响主流程。"""
    if path is None or not values:
        return
    payload = {name: value for name, value in values.items() if value}
    if not payload:
        return
    payload["saved_at"] = time.time()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError:
        pass


def merge_cookie(base: str, extra: Mapping[str, str]) -> str:
    """合并用户 Cookie 与设备指纹；用户显式配置的同名项优先。"""
    pairs: dict[str, str] = {}
    for item in str(base or "").split(";"):
        key, separator, value = item.strip().partition("=")
        key = key.strip()
        if separator and key and value:
            pairs.setdefault(key, value.strip())
    for key, value in extra.items():
        if value and key not in pairs:
            pairs[key] = value
    return "; ".join(f"{name}={value}" for name, value in pairs.items())


def _content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("Content-Length")
    except Exception:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _format_mb(size: int) -> str:
    return f"{size / 1024 / 1024:.1f}MB"


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
