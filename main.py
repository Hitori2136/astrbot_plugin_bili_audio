"""B 站音频提取插件。

指令：
    /提取音频 BV号   提取音频，默认整段发送一条 QQ 语音条

设计约束（按 AstrBot 4.29.1 / 4.19.5 的公共接口编写）：
- 只接受 B 站普通视频的 BV 号，插件会拼成
  https://www.bilibili.com/video/BV号；
- 只走 B 站官方 html5 播放接口（见 bili_api.py），音频统一转成
  16kHz 单声道 WAV；默认整段发送，可在配置里开启按 60 秒切片；
- 语音使用 AstrBot 的 Record 组件：OneBot/NapCat 与 QQ 官方机器人各自会转 silk；
- 不依赖 requests，不修改 AstrBot 核心依赖版本。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.star import Context, Star

try:  # httpx 由 AstrBot 运行环境提供，缺少时插件会提示依赖缺失
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

try:  # 插件正常以包的形式加载
    from . import bili_api as _bili_api
except ImportError:  # pragma: no cover - 脚本方式运行时退化为没有官方接口
    try:
        import bili_api as _bili_api  # type: ignore[no-redef]
    except ImportError:
        _bili_api = None  # type: ignore[assignment]

DIRECT_API_AVAILABLE = _bili_api is not None and httpx is not None

PLUGIN_NAME = "astrbot_plugin_bili_audio"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BILI_REFERER = "https://www.bilibili.com/"
BILI_QR_GENERATE_URL = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
)
BILI_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
BILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
QR_LOGIN_TIMEOUT = 180.0
QR_POLL_INTERVAL = 2.0
QR_STATE_SUCCESS = 0
QR_STATE_EXPIRED = 86038
QR_STATE_SCANNED = 86090
BUVID_FILENAME = "buvid.json"
DEFAULT_TIMEOUT = 60
MAX_ERROR_CHARS = 160
# 扫码登录接口响应的读取上限；正常响应只有几 KB，固定即可。
LOGIN_RESPONSE_LIMIT = 1024 * 1024
# 同一次指令里 probe 与下载共用视频信息，避免重复打接口。
INFO_CACHE_TTL = 300.0
# 官方接口下的是 muxed mp4，比纯音频流大，给它一个比解析更宽松的下限。
MIN_DOWNLOAD_SECONDS = 120.0
# B 站接口错误码分类。
RISK_CODES = frozenset({-352, -403, -412, 567})
REGION_CODES = frozenset({-10403, 87008})
LOGIN_CODES = frozenset({-101, -111})
UNAVAILABLE_CODES = frozenset({-404, 62002, 62004, 62012})

_BV_RE = re.compile(r"(?<![0-9A-Za-z])BV[0-9A-Za-z]{10}(?![0-9A-Za-z])")
_URL_LIKE_RE = re.compile(r"(?i)(https?://|www\.|b23\.tv|bilibili\.com)")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_COOKIE_HEADER_RE = re.compile(r"(?i)(cookie\s*:\s*)[^\r\n]+")
_COOKIE_PAIR_RE = re.compile(
    r"(?i)\b(SESSDATA|bili_jct|DedeUserID|sid|access_key|refresh_token)"
    r"\s*=\s*[^;\s]+"
)
_URL_CRED_RE = re.compile(r"(?i)(https?://)[^/\s@]+@")


# --------------------------------------------------------------------------- #
# 异常与数据结构
# --------------------------------------------------------------------------- #
class UserVisibleError(Exception):
    """消息会直接回给用户；不要把堆栈或原始响应带出去。"""


class UsageError(UserVisibleError):
    pass


class MissingDependencyError(UserVisibleError):
    pass


class FfmpegMissingError(UserVisibleError):
    pass


class DownloadFailedError(UserVisibleError):
    pass


class MediaTooLargeError(UserVisibleError):
    pass


class AudioConversionError(UserVisibleError):
    pass


@dataclass(frozen=True)
class BiliTarget:
    video_id: str
    url: str


@dataclass
class MediaMeta:
    title: str
    duration: float
    uploader: str = ""
    webpage_url: str = ""
    info: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class AudioChunks:
    paths: list[Path]
    duration: float
    total_chunks: int
    truncated: bool


@dataclass
class QrCode:
    url: str
    qrcode_key: str


@dataclass
class QrPoll:
    state: int
    payload: dict[str, Any]
    set_cookies: list[str]


# 只保留登录需要且适合写进 Cookie 头的字段，避免把 gourl 等参数带进去。
_COOKIE_NAMES = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
    "buvid3",
    "buvid4",
    "buvid_fp",
    "buvid_fp_plain",
    "b_nut",
)


# --------------------------------------------------------------------------- #
# 文本、URL 与脱敏
# --------------------------------------------------------------------------- #
def redact(text: object) -> str:
    """把 Cookie、URL 认证信息替换掉，避免日志泄露。"""
    out = _COOKIE_HEADER_RE.sub(lambda m: f"{m.group(1)}***", str(text))
    out = _COOKIE_PAIR_RE.sub(lambda m: f"{m.group(1)}=***", out)
    out = _URL_CRED_RE.sub(lambda m: f"{m.group(1)}***@", out)
    return out


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _short_error(text: object) -> str:
    cleaned = _strip_ansi(redact(text)).strip()
    cleaned = re.sub(r"(?i)^error:\s*", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:MAX_ERROR_CHARS]


def _clean_title(title: object) -> str:
    text = _CONTROL_RE.sub("", str(title or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:60] or "B站视频"


def _fmt_duration(seconds: float) -> str:
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _extract_bv(text: str) -> str:
    """从用户输入里提取 BV 号（区分大小写，不接受链接）。"""
    raw = str(text or "").strip()
    if _URL_LIKE_RE.search(raw):
        return ""
    match = _BV_RE.search(raw)
    return match.group(0) if match else ""


async def parse_bili_target(text: str) -> BiliTarget:
    """把 BV 号解析成规范的 B 站视频地址。"""
    video_id = _extract_bv(text)
    if not video_id:
        raise UsageError(
            "用法：/提取音频 BV号\n"
            "示例：/提取音频 BV1ikYa6zEkp"
        )
    return BiliTarget(
        video_id=video_id,
        url=f"https://www.bilibili.com/video/{video_id}",
    )


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "y"}:
            return True
        if lowered in {"0", "false", "no", "off", "n"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: object, default: int, low: int, high: int) -> int:
    try:
        number = int(float(value))  # 配置页偶尔会给出 "60.0"
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _as_float(value: object, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return max(low, min(number, high))


@dataclass
class PluginSettings:
    max_audio_mb: int = 100
    audio_chunk_seconds: int = 60
    max_audio_chunks: int = 30
    audio_send_interval: float = 0.6
    send_progress: bool = True
    cooldown_seconds: int = 20
    max_concurrent: int = 1
    timeout_seconds: int = DEFAULT_TIMEOUT
    enable_slicing: bool = False
    cookie: str = ""
    proxy: str = ""
    cleanup_delay_seconds: float = 600.0

    @property
    def max_audio_bytes(self) -> int:
        return self.max_audio_mb * 1024 * 1024

    @classmethod
    def from_config(cls, raw: dict | None) -> PluginSettings:
        raw = raw or {}

        def get(key: str, default: object = None) -> object:
            value = raw.get(key, default)
            return default if value is None else value

        cookie = str(get("cookie", "") or "").strip()
        if cookie and "=" not in cookie:
            cookie = f"SESSDATA={cookie}"

        return cls(
            max_audio_mb=_as_int(get("max_audio_mb", 100), 100, 1, 100),
            audio_chunk_seconds=_as_int(
                get("audio_chunk_seconds", 60), 60, 1, 60
            ),
            max_audio_chunks=_as_int(get("max_audio_chunks", 30), 30, 1, 120),
            audio_send_interval=_as_float(
                get("audio_send_interval", 0.6), 0.6, 0.0, 5.0
            ),
            send_progress=_as_bool(get("send_progress", True), True),
            cooldown_seconds=_as_int(get("cooldown_seconds", 20), 20, 0, 600),
            max_concurrent=_as_int(get("max_concurrent", 1), 1, 1, 3),
            timeout_seconds=_as_int(
                get("timeout_seconds", DEFAULT_TIMEOUT), DEFAULT_TIMEOUT, 10, 600
            ),
            enable_slicing=_as_bool(get("enable_slicing", False), False),
            cookie=cookie,
            proxy=str(get("proxy", "") or "").strip(),
        )


# --------------------------------------------------------------------------- #
# ffmpeg 与 WAV
# --------------------------------------------------------------------------- #
def _imageio_ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    try:
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # 下载/解压失败等
        logger.debug("imageio-ffmpeg unavailable: %s", redact(exc))
        return None
    if exe and Path(exe).is_file():
        return str(Path(exe).resolve())
    return None


def resolve_ffmpeg() -> str:
    """按 PATH -> imageio-ffmpeg 的顺序找到 ffmpeg。"""
    found = shutil.which("ffmpeg")
    if found:
        return str(Path(found).resolve())

    bundled = _imageio_ffmpeg_exe()
    if bundled:
        return bundled

    raise FfmpegMissingError(
        "未找到 ffmpeg，无法提取音频。请安装 ffmpeg，或重新安装插件依赖"
        "（imageio-ffmpeg 会自带二进制）。"
    )


async def convert_to_wav(
    source: Path,
    target: Path,
    ffmpeg: str,
    timeout: float = 60.0,
    processes: set[asyncio.subprocess.Process] | None = None,
) -> Path:
    """把任意音频转成 16kHz 单声道 16-bit PCM WAV，方便切片与 silk 编码。"""
    target.parent.mkdir(parents=True, exist_ok=True)

    if source.suffix.lower() == ".wav":
        try:
            with wave.open(str(source), "rb") as wav:
                if (
                    wav.getnchannels() == 1
                    and wav.getsampwidth() == 2
                    and wav.getframerate() == 16000
                ):
                    shutil.copyfile(source, target)
                    return target
        except (wave.Error, OSError):
            pass

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = creationflags

    # 不用 PIPE 读 stderr：某些沙箱/Windows 环境会拒绝创建匿名管道；
    # 写日志文件同样能拿到错误详情，且对长输出更稳。
    log_path = target.parent / "ffmpeg.log"
    with open(log_path, "wb") as log_file:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            **popen_kwargs,
        )
        if processes is not None:
            processes.add(process)
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                await _kill_process(process)
                raise AudioConversionError("音频转码超时，请换更短的视频。") from exc
            except asyncio.CancelledError:
                await _kill_process(process)
                raise
            except Exception:
                await _kill_process(process)
                raise
        finally:
            if processes is not None:
                processes.discard(process)

    detail = ""
    try:
        detail = _short_error(_read_text_tail(log_path))
    except OSError:
        pass

    if process.returncode != 0:
        raise AudioConversionError(f"音频转码失败：{detail or 'ffmpeg 返回错误'}")

    if not target.exists() or target.stat().st_size <= 44:
        raise AudioConversionError("音频转码失败：没有生成有效的 WAV 文件。")
    return target


def slice_wav(
    wav_path: Path,
    chunk_seconds: int,
    max_chunks: int,
    outdir: Path,
) -> AudioChunks:
    """按固定时长把 WAV 切片；不重编码，只重写 PCM 帧。"""
    chunk_seconds = max(1, int(chunk_seconds))
    max_chunks = max(1, int(max_chunks))
    outdir.mkdir(parents=True, exist_ok=True)

    with wave.open(str(wav_path), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        frame_rate = source.getframerate()
        total_frames = source.getnframes()
        if frame_rate <= 0 or total_frames <= 0:
            raise AudioConversionError("音频为空，无法切片。")

        frames_per_chunk = max(1, int(frame_rate * chunk_seconds))
        total_chunks = max(1, math.ceil(total_frames / frames_per_chunk))
        limit = min(total_chunks, max_chunks)
        paths: list[Path] = []

        for index in range(limit):
            remaining = total_frames - index * frames_per_chunk
            frames = source.readframes(min(frames_per_chunk, remaining))
            if not frames:
                break
            chunk_path = outdir / f"part_{index + 1:03d}.wav"
            with wave.open(str(chunk_path), "wb") as target:
                target.setnchannels(channels)
                target.setsampwidth(sample_width)
                target.setframerate(frame_rate)
                target.writeframes(frames)
            paths.append(chunk_path)

        duration = total_frames / frame_rate
        if not paths:
            raise AudioConversionError("音频切片失败：没有生成任何语音段。")
        return AudioChunks(
            paths=paths,
            duration=duration,
            total_chunks=total_chunks,
            truncated=total_chunks > limit,
        )


# --------------------------------------------------------------------------- #
# 错误码翻译
# --------------------------------------------------------------------------- #
def map_api_error(exc: BaseException) -> UserVisibleError:
    """把官方接口异常翻译成可以直接回给用户的提示。"""
    if isinstance(exc, UserVisibleError):
        return exc

    if _bili_api is not None and isinstance(exc, _bili_api.BiliDownloadError):
        if exc.too_large:
            return MediaTooLargeError(
                "视频源文件超过 max_audio_mb 上限，已停止下载；"
                "可在插件配置里调大该值后重试。"
            )
        return DownloadFailedError(f"下载音频失败：{_short_error(exc)}")

    if _bili_api is not None and isinstance(exc, _bili_api.BiliApiError):
        code = exc.numeric_code
        if code is None:
            return DownloadFailedError(f"无法连接 B 站接口：{_short_error(exc)}")
        if code in LOGIN_CODES:
            return DownloadFailedError(
                "该视频需要登录才能访问，请发送 /账号登录 或用插件配置填写 B 站 Cookie。"
            )
        if code in REGION_CODES:
            return DownloadFailedError(
                "该视频在当前地区不可用（地区限制），请配置国内出口的代理后重试。"
            )
        if code in RISK_CODES:
            return DownloadFailedError(
                "B 站拒绝了官方接口请求（风控），请稍后重试；长期失败可配置代理。"
            )
        if code in UNAVAILABLE_CODES:
            return DownloadFailedError("视频不存在、已删除或当前不可见。")
        return DownloadFailedError(f"B 站接口返回错误（code={exc.code}），请稍后重试。")

    return DownloadFailedError(f"下载失败：{_short_error(exc)}")


def _view_cid(data: dict[str, Any]) -> int | None:
    """从 view 接口结果里取第一个分P 的 cid。"""
    try:
        cid = int(data.get("cid") or 0)
    except (TypeError, ValueError):
        cid = 0
    if cid > 0:
        return cid

    pages = data.get("pages")
    if isinstance(pages, list) and pages:
        first = pages[0]
        if isinstance(first, dict):
            try:
                page_cid = int(first.get("cid") or 0)
            except (TypeError, ValueError):
                return None
            return page_cid or None
    return None


def _read_text_tail(path: Path, limit: int = 8192) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


async def _kill_process(process: asyncio.subprocess.Process) -> None:
    """确保超时/取消时子进程被终止，避免留下孤儿进程。"""
    if process.returncode is not None:
        return
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        pass


class BiliService:
    """通过 B 站官方接口获取视频信息与音频源文件。"""

    def __init__(self, settings: PluginSettings, ffmpeg: str | None = None):
        self.settings = settings
        self.ffmpeg = ffmpeg
        self.active_processes: set[asyncio.subprocess.Process] = set()
        # 设备指纹与视频信息缓存，减少同一进程内对 B 站接口的重复请求。
        self._buvid: dict[str, str] = {}
        self._buvid_loaded = False
        self._state_dir: Path | None = None
        self._view_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def set_ffmpeg(self, ffmpeg: str) -> None:
        self.ffmpeg = ffmpeg

    def set_settings(self, settings: PluginSettings) -> None:
        """配置写回后刷新运行时设置（Cookie 等随即生效）。"""
        self.settings = settings

    async def aclose(self) -> None:
        """插件卸载时终止仍在运行的 ffmpeg 子进程。"""
        for process in list(self.active_processes):
            await _kill_process(process)
        self.active_processes.clear()

    # ---------------------------- 官方接口 ------------------------------ #
    def set_state_dir(self, path: Path | None) -> None:
        """设置设备指纹等本地状态的存放目录。"""
        self._state_dir = path

    @property
    def direct_enabled(self) -> bool:
        """官方接口是否可用（缺少 httpx 或 bili_api 时不可用）。"""
        return DIRECT_API_AVAILABLE

    def _buvid_path(self) -> Path | None:
        if self._state_dir is None:
            return None
        return Path(self._state_dir) / BUVID_FILENAME

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Referer": BILI_REFERER,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    def _api_headers(self) -> dict[str, str]:
        headers = self._base_headers()
        cookie = _bili_api.merge_cookie(self.settings.cookie or "", self._buvid)
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _client(self) -> Any:
        timeout = float(self.settings.timeout_seconds)
        return _bili_api.build_client(
            proxy=self.settings.proxy,
            timeout=httpx.Timeout(
                connect=min(10.0, timeout),
                read=min(30.0, max(10.0, timeout / 2)),
                write=min(30.0, max(10.0, timeout / 2)),
                pool=5.0,
            ),
        )

    async def _ensure_buvid(self, client: Any, *, refresh: bool = False) -> None:
        """复用本地设备指纹；refresh 时重新申请一个。"""
        if not refresh:
            if self._buvid_loaded:
                return
            stored = _bili_api.load_buvid(self._buvid_path())
            if stored:
                self._buvid = stored
                self._buvid_loaded = True
                return
        try:
            values = await _bili_api.fetch_buvid(
                client, headers=self._base_headers()
            )
        except Exception as exc:
            logger.debug("B 站设备指纹获取失败，继续匿名请求：%s", redact(exc))
            self._buvid = {}
            self._buvid_loaded = True
            return
        self._buvid = values
        self._buvid_loaded = True
        if values:
            _bili_api.save_buvid(self._buvid_path(), values)

    async def _direct_view(self, bvid: str) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._view_cache.get(bvid)
        if cached and now < cached[0]:
            return cached[1]
        async with self._client() as client:
            await self._ensure_buvid(client)
            data = await _bili_api.fetch_video(
                client, bvid, headers=self._api_headers()
            )
        self._view_cache[bvid] = (time.monotonic() + INFO_CACHE_TTL, data)
        while len(self._view_cache) > 32:
            self._view_cache.pop(next(iter(self._view_cache)))
        return data

    async def _direct_play_info(self, bvid: str, cid: int) -> Any:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                async with self._client() as client:
                    await self._ensure_buvid(client, refresh=attempt > 0)
                    return await _bili_api.fetch_play_info(
                        client, bvid, cid, headers=self._api_headers()
                    )
            except Exception as exc:
                last_error = exc
                if attempt > 0 or not self._retryable(exc):
                    raise
                logger.info(
                    "B 站播放接口被拒绝（%s），更换设备指纹后重试。",
                    redact(str(exc)),
                )
                await asyncio.sleep(1.5)
        raise last_error  # type: ignore[misc]

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        if _bili_api is None:
            return False
        return isinstance(exc, _bili_api.BiliApiError) and exc.retryable

    async def _direct_download(self, urls: list[str], dest: Path) -> None:
        timeout = float(self.settings.timeout_seconds)
        deadline = time.monotonic() + max(timeout, MIN_DOWNLOAD_SECONDS)
        async with self._client() as client:
            await _bili_api.download_media(
                client,
                urls,
                dest,
                headers=self._api_headers(),
                max_bytes=self.settings.max_audio_bytes,
                deadline=deadline,
            )

    async def _direct_audio(self, target: BiliTarget, workdir: Path) -> Path:
        data = await self._direct_view(target.video_id)
        cid = _view_cid(data)
        if not cid:
            raise DownloadFailedError("B 站接口没有返回视频分P信息。")
        dest = workdir / "source.mp4"
        last: Exception | None = None
        for attempt in range(2):
            try:
                play = await self._direct_play_info(target.video_id, cid)
                limit = self.settings.max_audio_bytes
                if play.total_size and play.total_size > limit:
                    raise _bili_api.BiliDownloadError(
                        f"源文件 {play.total_size / 1048576:.1f}MB"
                        f" 超过 {limit / 1048576:.1f}MB 上限",
                        too_large=True,
                    )
                await self._direct_download(list(play.urls), dest)
                return dest
            except _bili_api.BiliDownloadError as exc:
                last = exc
                if attempt > 0 or exc.too_large:
                    raise
                logger.info(
                    "B 站 CDN 下载失败（%s），重新获取播放地址后重试。",
                    redact(str(exc)),
                )
        raise last  # type: ignore[misc]

    @staticmethod
    def _direct_meta(data: dict[str, Any], target: BiliTarget) -> MediaMeta:
        owner = data.get("owner")
        uploader = str(owner.get("name") or "") if isinstance(owner, dict) else ""
        return MediaMeta(
            title=_clean_title(data.get("title")),
            duration=_as_float(data.get("duration"), 0.0, 0.0, 86400.0),
            uploader=uploader,
            webpage_url=target.url,
            info={"source": "bilibili-api"},
        )

    async def probe(self, target: BiliTarget) -> MediaMeta:
        if not self.direct_enabled:
            raise MissingDependencyError(
                "缺少 httpx 或 bili_api.py，无法访问 B 站官方接口。"
            )
        try:
            data = await self._direct_view(target.video_id)
        except Exception as exc:
            raise map_api_error(exc) from exc
        return self._direct_meta(data, target)

    async def fetch_audio(self, target: BiliTarget, workdir: Path) -> Path:
        if not self.direct_enabled:
            raise MissingDependencyError(
                "缺少 httpx 或 bili_api.py，无法访问 B 站官方接口。"
            )
        attempt = workdir / "api"
        attempt.mkdir(parents=True, exist_ok=True)
        try:
            return await self._direct_audio(target, attempt)
        except Exception as exc:
            shutil.rmtree(attempt, ignore_errors=True)
            raise map_api_error(exc) from exc


# --------------------------------------------------------------------------- #
# B 站扫码登录
# --------------------------------------------------------------------------- #
def _login_headers(cookie: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": BILI_REFERER,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _http_json_sync(
    url: str,
    headers: dict[str, str],
    timeout: float,
    max_bytes: int,
) -> tuple[dict[str, Any], list[str]]:
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise UserVisibleError(
                    f"B 站登录接口响应超过 {max_bytes // (1024 * 1024)}MB 上限。"
                )
            set_cookies = response.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as exc:
        detail = _short_error(
            exc.read(max_bytes + 1).decode("utf-8", "replace")
        )
        raise UserVisibleError(
            f"B 站登录接口返回 HTTP {exc.code}"
            + (f"：{detail}" if detail else "")
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UserVisibleError("无法连接 B 站登录服务，请检查网络或代理。") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UserVisibleError("B 站登录接口返回了无法解析的数据。") from exc
    if not isinstance(payload, dict):
        raise UserVisibleError("B 站登录接口返回格式异常。")
    return payload, list(set_cookies)


class BiliLoginClient:
    """B 站 Web 扫码登录接口的轻量客户端。"""

    def __init__(
        self, timeout: float = 20.0, max_response_bytes: int = 16 * 1024 * 1024
    ):
        self.timeout = max(5.0, min(float(timeout), 60.0))
        self.max_response_bytes = max(1024, int(max_response_bytes))

    async def _get_json(
        self, url: str, cookie: str | None = None
    ) -> tuple[dict[str, Any], list[str]]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    _http_json_sync,
                    url,
                    _login_headers(cookie),
                    self.timeout,
                    self.max_response_bytes,
                ),
                timeout=self.timeout + 5,
            )
        except asyncio.TimeoutError as exc:
            raise UserVisibleError("B 站登录接口超时，请检查网络或稍后重试。") from exc

    async def generate(self) -> QrCode:
        payload, _ = await self._get_json(BILI_QR_GENERATE_URL)
        if payload.get("code") != 0:
            raise UserVisibleError(
                "生成 B 站登录二维码失败："
                f"{payload.get('message') or payload.get('code')}"
            )
        data = payload.get("data") or {}
        url = str(data.get("url") or "").strip()
        key = str(data.get("qrcode_key") or "").strip()
        if not url or not key:
            raise UserVisibleError("生成 B 站登录二维码失败：返回数据不完整。")
        return QrCode(url=url, qrcode_key=key)

    async def poll(self, qrcode_key: str) -> QrPoll:
        query = urllib.parse.urlencode(
            {"qrcode_key": qrcode_key, "source": "main-fe-header"}
        )
        payload, set_cookies = await self._get_json(
            f"{BILI_QR_POLL_URL}?{query}"
        )
        if payload.get("code") != 0:
            raise UserVisibleError(
                "查询扫码状态失败："
                f"{payload.get('message') or payload.get('code')}"
            )
        data = payload.get("data") or {}
        try:
            state = int(data.get("code") if data.get("code") is not None else -1)
        except (TypeError, ValueError):
            state = -1
        return QrPoll(state=state, payload=data, set_cookies=set_cookies)

    async def fetch_nav(self, cookie: str) -> dict[str, Any]:
        try:
            payload, _ = await self._get_json(BILI_NAV_URL, cookie=cookie)
        except Exception:
            return {}
        if payload.get("code") == 0 and isinstance(payload.get("data"), dict):
            data = payload["data"]
            if data.get("isLogin"):
                return data
        return {}

    @staticmethod
    def extract_cookie(poll: QrPoll) -> str:
        """优先从 Set-Cookie 提取，失败时回退到 data.url 的查询参数。"""
        jar: dict[str, str] = {}
        for header in poll.set_cookies:
            first = str(header).split(";", 1)[0]
            name, separator, value = first.partition("=")
            name = name.strip()
            if separator and name in _COOKIE_NAMES and value.strip():
                jar[name] = value.strip()

        if "SESSDATA" not in jar:
            login_url = str(poll.payload.get("url") or "")
            query = urllib.parse.urlsplit(login_url).query
            for item in query.split("&"):
                name, separator, value = item.partition("=")
                name = urllib.parse.unquote(name)
                if separator and name in _COOKIE_NAMES and name not in jar:
                    # data.url 是 application/x-www-form-urlencoded，
                    # 值需要解码后再写进 Cookie 头。
                    jar[name] = urllib.parse.unquote(value.strip())

        if not jar.get("SESSDATA"):
            raise UserVisibleError(
                "扫码已确认，但没有拿到 SESSDATA。请重新发送 /账号登录。"
            )
        return "; ".join(f"{name}={jar[name]}" for name in _COOKIE_NAMES if name in jar)


def make_qr_png(data: str, target: Path) -> Path:
    try:
        import qrcode
    except ImportError as exc:
        raise MissingDependencyError(
            "缺少 qrcode 依赖，无法生成登录二维码。请重载插件安装 requirements.txt。"
        ) from exc

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )
        qr.add_data(data)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        image.save(str(target))
    except Exception as exc:
        raise UserVisibleError(f"生成登录二维码图片失败：{_short_error(exc)}") from exc
    return target


# --------------------------------------------------------------------------- #
# 消息组件兼容层
# --------------------------------------------------------------------------- #
def _make_record_component(path: Path) -> Record:
    factory = getattr(Record, "fromFileSystem", None)
    if callable(factory):
        return factory(str(path))
    return Record(file=str(path), path=str(path))


def _make_image_component(path: Path) -> Image:
    factory = getattr(Image, "fromFileSystem", None)
    if callable(factory):
        return factory(str(path))
    return Image(file=str(path), path=str(path))


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
def _build_supported_platforms():
    enum = filter.PlatformAdapterType
    supported = enum.AIOCQHTTP | enum.QQOFFICIAL
    extra = getattr(enum, "QQOFFICIAL_WEBHOOK", None)
    if extra is not None:
        supported |= extra
    return supported


_SUPPORTED_PLATFORMS = _build_supported_platforms()


class BiliMediaPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.settings = PluginSettings.from_config(self.config)
        self.service = BiliService(self.settings)
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrent)
        self._cooldowns: dict[str, float] = {}
        self._ffmpeg: str | None = None
        self._tmp_dirs: set[Path] = set()
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._cleanup_delay = self.settings.cleanup_delay_seconds
        self.login_client = BiliLoginClient(
            self.settings.timeout_seconds, LOGIN_RESPONSE_LIMIT
        )
        self._login_lock = asyncio.Lock()
        self._data_dir_path: Path | None = None
        # 设备指纹缓存在插件数据目录，重载后仍保持同一身份。
        self.service.set_state_dir(self._data_dir())

    # ---------------------------- 指令入口 ---------------------------- #
    @filter.command("提取音频")
    @filter.platform_adapter_type(_SUPPORTED_PLATFORMS)
    async def extract_audio(self, event: AstrMessageEvent, bvid: str = ""):
        async for result in self._handle_media(event, bvid):
            yield result

    # ---------------------------- 扫码登录 ---------------------------- #
    @filter.command("账号登录")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(_SUPPORTED_PLATFORMS)
    async def account_login(self, event: AstrMessageEvent):
        self._block_llm(event)

        if self._login_lock.locked():
            yield event.plain_result(
                "已有扫码登录流程正在进行，请先完成或等待它过期，再重新发送 /账号登录。"
            )
            return
        await self._login_lock.acquire()

        workdir: Path | None = None
        try:
            caption = (
                "请用哔哩哔哩 App 扫描二维码，并在手机上确认登录。\n"
                "二维码 3 分钟内有效；确认后机器人会把 Cookie 写入插件配置。"
            )

            qr = await self.login_client.generate()
            workdir = Path(tempfile.mkdtemp(prefix="astrbot_bili_login_"))
            self._tmp_dirs.add(workdir)
            qr_path = workdir / "login_qr.png"
            await asyncio.to_thread(make_qr_png, qr.url, qr_path)
            yield event.chain_result(
                [Plain(caption), _make_image_component(qr_path)]
            )

            deadline = time.monotonic() + QR_LOGIN_TIMEOUT
            scanned_notified = False
            consecutive_errors = 0
            while time.monotonic() < deadline:
                try:
                    poll = await self.login_client.poll(qr.qrcode_key)
                    consecutive_errors = 0
                except UserVisibleError as exc:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        yield event.plain_result(str(exc))
                        return
                    await asyncio.sleep(QR_POLL_INTERVAL)
                    continue

                if poll.state == QR_STATE_SUCCESS:
                    cookie = self.login_client.extract_cookie(poll)
                    nav = await self.login_client.fetch_nav(cookie)
                    uid = self._cookie_value(cookie, "DedeUserID") or str(
                        nav.get("mid") or ""
                    )
                    nickname = str(nav.get("uname") or "B站账号")
                    saved = self._apply_cookie(cookie)
                    text = f"B 站账号登录成功：{nickname}"
                    if uid:
                        text += f"（UID {uid}）"
                    if saved:
                        text += (
                            "\nCookie 已写入插件配置，"
                            "后续 /提取音频 会自动使用该账号。"
                        )
                    else:
                        text += (
                            "\nCookie 只在本次运行内有效；写入配置失败，"
                            "重载插件后需要重新扫码。"
                        )
                    yield event.plain_result(text)
                    return

                if poll.state == QR_STATE_EXPIRED:
                    yield event.plain_result(
                        "二维码已过期，请重新发送 /账号登录 获取新的二维码。"
                    )
                    return

                if poll.state == QR_STATE_SCANNED and not scanned_notified:
                    scanned_notified = True
                    yield event.plain_result(
                        "已扫码，请在手机 B 站上点击确认登录。"
                    )

                await asyncio.sleep(QR_POLL_INTERVAL)

            yield event.plain_result(
                "二维码已过期（3 分钟），请重新发送 /账号登录。"
            )

        except UserVisibleError as exc:
            yield event.plain_result(str(exc))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("B站扫码登录出现未预期错误", exc_info=True)
            yield event.plain_result("扫码登录失败，请稍后重试。详情已写入日志。")
        finally:
            if workdir is not None:
                # 不要立即删除：框架可能在 yield 之后才读取图片；延迟清理。
                self._schedule_cleanup(workdir)
            self._login_lock.release()

    @filter.command("账号状态", alias={"登录状态"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(_SUPPORTED_PLATFORMS)
    async def account_status(self, event: AstrMessageEvent):
        self._block_llm(event)
        cookie = str(self.config.get("cookie") or "").strip()
        if not cookie:
            yield event.plain_result(
                "尚未配置 B 站 Cookie。发送 /账号登录 扫码后会自动写入配置。"
            )
            return

        nav = await self.login_client.fetch_nav(cookie)
        if not nav:
            yield event.plain_result(
                "插件配置里已有 B 站 Cookie，但校验未通过：可能已失效，"
                "请发送 /账号登录 重新扫码。"
            )
            return

        name = str(nav.get("uname") or "B站账号")
        uid = str(nav.get("mid") or "")
        text = f"当前使用插件配置里的 B 站账号：{name}"
        if uid:
            text += f"（UID {uid}）"
        text += "\n发送 /账号登录 可更换账号，发送 /退出登录 可清除 Cookie。"
        yield event.plain_result(text)

    @filter.command("退出登录")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(_SUPPORTED_PLATFORMS)
    async def account_logout(self, event: AstrMessageEvent):
        self._block_llm(event)
        if not str(self.config.get("cookie") or "").strip():
            yield event.plain_result(
                "当前没有配置 B 站 Cookie。发送 /账号登录 扫码即可登录。"
            )
            return
        if not self._apply_cookie(""):
            yield event.plain_result(
                "已清空本次运行使用的 Cookie，但写入配置失败，请检查日志。"
            )
            return
        yield event.plain_result(
            "已清除插件配置里的 B 站 Cookie。发送 /账号登录 可重新扫码。"
        )

    # ---------------------------- 主流程 ------------------------------ #
    async def _handle_media(self, event: AstrMessageEvent, bvid: str):
        self._block_llm(event)

        query = str(bvid or "").strip()
        if not query:
            query = self._strip_command(event)

        try:
            target = await parse_bili_target(query)
        except UserVisibleError as exc:
            yield event.plain_result(str(exc))
            return

        remaining = self._cooldown_remaining(event)
        if remaining > 0:
            yield event.plain_result(
                f"操作太频繁，请 {max(1, int(remaining))} 秒后再试。"
            )
            return
        self._mark_cooldown(event)

        workdir: Path | None = None
        acquired = False
        success = False
        try:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=15.0)
                acquired = True
            except asyncio.TimeoutError:
                yield event.plain_result("当前任务较多，请稍后再试。")
                return

            workdir = Path(tempfile.mkdtemp(prefix="astrbot_bili_audio_"))
            self._tmp_dirs.add(workdir)
            ffmpeg = await self._ensure_ffmpeg()

            meta = await self.service.probe(target)
            if self.settings.send_progress:
                yield event.plain_result(self._progress_text(meta))

            source = await self.service.fetch_audio(target, workdir)
            wav_path = workdir / "audio.wav"
            await convert_to_wav(
                source,
                wav_path,
                ffmpeg,
                timeout=min(60.0, float(self.settings.timeout_seconds)),
                processes=self.service.active_processes,
            )

            if not self.settings.enable_slicing:
                self._track_file(event, wav_path)
                success = True
                yield event.chain_result([_make_record_component(wav_path)])
            else:
                chunks = slice_wav(
                    wav_path,
                    self.settings.audio_chunk_seconds,
                    self.settings.max_audio_chunks,
                    workdir / "chunks",
                )
                for index, chunk in enumerate(chunks.paths):
                    self._track_file(event, chunk)
                    success = True
                    yield event.chain_result([_make_record_component(chunk)])
                    if index + 1 < len(chunks.paths):
                        await asyncio.sleep(self.settings.audio_send_interval)
                if chunks.truncated:
                    yield event.plain_result(
                        f"音频共 {_fmt_duration(chunks.duration)}，超过单次 "
                        f"{len(chunks.paths)} 段限制，仅发送前 {len(chunks.paths)} 段"
                        f"（每段 {self.settings.audio_chunk_seconds} 秒）。"
                    )

        except UserVisibleError as exc:
            logger.warning("B站音频处理失败：%s", redact(str(exc)))
            yield event.plain_result(str(exc))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("B站音频处理出现未预期错误", exc_info=True)
            yield event.plain_result("处理失败，请稍后再试。详情已写入日志。")
        finally:
            if acquired:
                self._semaphore.release()
            if workdir is not None:
                if success:
                    self._schedule_cleanup(workdir)
                else:
                    self._rmtree(workdir)

    # ---------------------------- 辅助 -------------------------------- #
    def _apply_cookie(self, cookie: str) -> bool:
        """把 Cookie 写回插件配置并刷新运行时设置，返回是否持久化成功。"""
        value = str(cookie or "").strip()
        try:
            self.config["cookie"] = value
        except Exception:
            logger.error("更新 B 站 Cookie 配置失败", exc_info=True)
        self.settings = PluginSettings.from_config(self.config)
        self.service.set_settings(self.settings)

        saver = getattr(self.config, "save_config", None)
        if not callable(saver):
            logger.warning("当前环境不支持写回插件配置，Cookie 仅在本次运行有效。")
            return False
        try:
            saver()
            return True
        except Exception:
            logger.error("保存插件配置失败", exc_info=True)
            return False


    def _data_dir(self, create: bool = False) -> Path:
        if self._data_dir_path is None:
            try:
                try:
                    from astrbot.api.star import StarTools
                except ImportError:
                    from astrbot.core.star.star_tools import StarTools

                path = Path(StarTools.get_data_dir(PLUGIN_NAME))
            except Exception as exc:
                logger.debug("无法获取插件数据目录，使用回退路径：%s", redact(exc))
                path = Path("data/plugin_data") / PLUGIN_NAME
            self._data_dir_path = path
        if create:
            self._data_dir_path.mkdir(parents=True, exist_ok=True)
        return self._data_dir_path






    @staticmethod
    def _cookie_value(cookie: str, name: str) -> str:
        for item in str(cookie or "").split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key == name:
                return value.strip()
        return ""


    async def _ensure_ffmpeg(self) -> str:
        if self._ffmpeg is None:
            # imageio-ffmpeg 首次解析/解压二进制可能阻塞，不能放在事件循环里。
            self._ffmpeg = await asyncio.to_thread(resolve_ffmpeg)
            self.service.set_ffmpeg(self._ffmpeg)
        return self._ffmpeg

    def _block_llm(self, event: AstrMessageEvent) -> None:
        setter = getattr(event, "should_call_llm", None)
        if callable(setter):
            try:
                setter(False)
            except Exception:
                logger.debug("should_call_llm(False) 调用失败", exc_info=True)

    def _strip_command(self, event: AstrMessageEvent) -> str:
        text = str(event.get_message_str() or "").strip()
        return re.sub(
            r"^\s*[!！/／]?\s*提取音频\s*", "", text, count=1
        ).strip()

    def _cooldown_key(self, event: AstrMessageEvent) -> str:
        platform = ""
        sender = ""
        try:
            platform = str(event.get_platform_name() or "")
            sender = str(event.get_sender_id() or "")
        except Exception:
            pass
        if not sender:
            try:
                sender = str(event.get_group_id() or "")
            except Exception:
                sender = ""
        return f"{platform}:{sender or 'unknown'}"

    def _cooldown_remaining(self, event: AstrMessageEvent) -> float:
        key = self._cooldown_key(event)
        now = time.monotonic()
        if len(self._cooldowns) > 512:
            self._cooldowns = {
                item: until for item, until in self._cooldowns.items() if until > now
            }
        until = self._cooldowns.get(key, 0.0)
        if until <= now:
            self._cooldowns.pop(key, None)
            return 0.0
        return until - now

    def _mark_cooldown(self, event: AstrMessageEvent) -> None:
        seconds = self.settings.cooldown_seconds
        if seconds > 0:
            self._cooldowns[self._cooldown_key(event)] = time.monotonic() + seconds

    def _progress_text(self, meta: MediaMeta) -> str:
        title = _clean_title(meta.title)
        duration = f"（{_fmt_duration(meta.duration)}）" if meta.duration > 0 else ""
        estimate = ""
        if self.settings.enable_slicing and meta.duration > 0:
            count = min(
                math.ceil(meta.duration / self.settings.audio_chunk_seconds),
                self.settings.max_audio_chunks,
            )
            estimate = f"，预计 {count} 段语音"
        return f"已识别《{title}》{duration}，正在提取音频{estimate}…"

    def _track_file(self, event: AstrMessageEvent, path: Path) -> None:
        tracker = getattr(event, "track_temporary_local_file", None)
        if callable(tracker):
            try:
                tracker(str(path))
            except Exception:
                logger.debug("track_temporary_local_file 调用失败", exc_info=True)

    def _schedule_cleanup(self, path: Path) -> None:
        delay = float(self._cleanup_delay)
        if delay <= 0:
            self._rmtree(path)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._rmtree(path)
            return

        async def cleanup() -> None:
            try:
                await asyncio.sleep(delay)
            finally:
                self._rmtree(path)

        task = loop.create_task(cleanup())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def _rmtree(self, path: Path) -> None:
        self._tmp_dirs.discard(path)
        shutil.rmtree(path, ignore_errors=True)

    async def terminate(self) -> None:
        """插件卸载/重载时清理临时文件与后台任务。"""
        tasks = list(self._cleanup_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.service.aclose()
        for path in list(self._tmp_dirs):
            self._rmtree(path)
        logger.debug("astrbot_plugin_bili_audio 已卸载")
