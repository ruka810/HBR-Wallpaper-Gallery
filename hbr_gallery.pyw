# -*- coding: utf-8 -*-
"""
Heaven Burns Red 壁纸图库提取器

一个本地 Web 图库工具：扫描游戏目录里的图片图集，以缩略图墙展示，
支持逐张勾选、分类过滤、搜索、壁纸尺寸筛选，并把选中的图片批量导出为 PNG 当壁纸。

- 只读游戏安装目录（不修改、不写入）。
- 读取本地安装目录和游戏更新目录，可按需补全缺失资源。
- 目录索引用 mmap 解析超大 catalog.bin，避免 OOM。
- 按浏览顺序在后台解码并缓存缩略图。

用法：双击本文件（.pyw）运行，自动打开浏览器访问本地图库。
"""

from __future__ import annotations

import io
import hashlib
import json
import os
import mmap
import re
import struct
import queue
import stat
import tempfile
import sys
import threading
import time
import webbrowser
from urllib.request import Request, urlopen
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from uuid import uuid4
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from PIL import Image as PILImage, PngImagePlugin
    PIL_OK = True
except Exception as _exc:
    PILImage = None
    PIL_OK = False
    print("警告：Pillow 不可用，将无法生成缩略图：", _exc)
try:
    _RESAMPLE = PILImage.Resampling.LANCZOS
except Exception:
    try:
        _RESAMPLE = PILImage.LANCZOS
    except Exception:
        _RESAMPLE = None
try:
    import UnityPy as _UnityPy
    _UnityPy.config.FALLBACK_UNITY_VERSION = "6000.0.65f1"
    UNITYPY_OK = True
except Exception as exc:
    _UnityPy = None
    UNITYPY_OK = False
    print("警告：UnityPy 不可用，将无法解码贴图：", exc)

# ----------------------------------------------------------------------------
# 路径配置
# ----------------------------------------------------------------------------
DEFAULT_GAME_DIR = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\HeavenBurnsRed\HeavenBurnsRed_Data"
    r"\StreamingAssets\aa\StandaloneWindows64"
)

TOOL_DIR = Path(__file__).resolve().parent          # 程序自身的文件夹（移动整个文件夹即可，其余路径都相对它）
INDEX_CACHE = TOOL_DIR / "hbr_index.json"
INFO_FILE = TOOL_DIR / "hbr_info.json"               # 已解析到的分辨率映射
THUMB_DIR = TOOL_DIR / "hbr_thumbs"
SELECTION_FILE = TOOL_DIR / "hbr_selection.json"     # 已选 id
OUTPUT_ROOT = TOOL_DIR / "HBR_Wallpapers"
PREFERENCES_FILE = TOOL_DIR / "hbr_preferences.json"
WEB_DIR = TOOL_DIR / "web"
CONFIG_FILE = TOOL_DIR / "hbr_config.json"           # 可选：游戏数据目录/缓存目录配置
SERVER_INDEX_FILE = TOOL_DIR / "hbr_index_server.json"
REMOTE_DIR = TOOL_DIR / "remote_q"
CDN_HOST = "heaven-burns-red-assets.akamaized.net"
CDN_ROOT = f"https://{CDN_HOST}/prod/assets"
INDEX_VERSION = 4

# 游戏数据目录：默认指向本机游戏路径，也可在 hbr_config.json 里写 {"game_dir": "..."} 覆盖
_CFG = {}
_config_error = None
try:
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("配置必须是 JSON 对象")
    for key in ("game_dir", "unity_cache_dir", "cache_dir"):
        value = config.get(key)
        if value is not None and (not isinstance(value, str) or "\x00" in value):
            raise ValueError(f"{key} 必须是目录路径字符串")
    _CFG = config
except FileNotFoundError:
    pass
except (OSError, ValueError) as exc:
    _config_error = str(exc)


def _detect_game_dir() -> Path:
    """按 配置 > 默认路径 > Steam 库 的顺序定位游戏资源目录。"""
    cand: list[Path] = []
    cfg_dir = _CFG.get("game_dir")
    if cfg_dir:
        cand.append(Path(cfg_dir))
    cand.append(DEFAULT_GAME_DIR)
    # 扫描 Steam 安装库
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = Path(winreg.QueryValueEx(k, "SteamPath")[0])
        cand.append(steam)
        vdf = steam / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            txt = vdf.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
                cand.append(Path(m.group(1).replace("\\\\", "\\")))
    except Exception:
        pass
    for base in cand:
        base = Path(base)
        for sub in (
            base / "HeavenBurnsRed_Data" / "StreamingAssets" / "aa" / "StandaloneWindows64",
            base / "steamapps" / "common" / "HeavenBurnsRed" / "HeavenBurnsRed_Data"
                 / "StreamingAssets" / "aa" / "StandaloneWindows64",
            base,
        ):
            if sub.is_dir() and (sub.parent / "catalog.bin").is_file():
                return sub
    return cand[0] if cand else DEFAULT_GAME_DIR


GAME_BUNDLE_DIR = _detect_game_dir()
CATALOG_FILE = GAME_BUNDLE_DIR.parent / "catalog.bin"
UNITY_CACHE_DIR = Path(_CFG.get("unity_cache_dir") or
                       Path.home() / "AppData/LocalLow/Unity/wfs_HeavenBurnsRed")

THUMB_DEFAULT_W = 256
THUMB_CACHE_NAME = re.compile(r"[0-9]+(?:-[0-9a-f]{16})?\.png")
BUNDLE_LRU_CAP = 8     # 每个工作线程的 bundle 缓存上限
THUMB_WORKERS = 4
EXPORT_WORKERS = 6     # 导出并发线程数

UINT32_MAX = 0xFFFFFFFF
DYN_STR = 0x40000000
UNI_STR = 0x80000000
STR_MASK = 0x3FFFFFFF
EXPORT_EXTS = (".png", ".jpg", ".jpeg", ".bmp")
IMAGE_EXTS = EXPORT_EXTS + (".tga", ".tif", ".tiff", ".psd", ".dds", ".webp", ".exr", ".gif")

# ----------------------------------------------------------------------------
# 运行状态
# ----------------------------------------------------------------------------
ITEMS: list[dict] = []                       # {id,p,c,b,s,w,h}
SELECTED: set[int] = set()
_state_lock = threading.Lock()
_preferences = {"out": "", "folders": [], "last_check": None, "last_thumbnail_sync": None}
_picker_requests = queue.Queue()
_picker_lock = threading.Lock()
_picker_available = threading.Event()

_bundle_cache = threading.local()  # UnityPy readers seek in mutable streams
_thumb_pool: ThreadPoolExecutor | None = None
_thumb_inflight = {}
_thumb_failed = set()
_thumb_ready: set[int] = set()
_thumb_lock = threading.Lock()
_stopping = threading.Event()
_full_cache = OrderedDict()
_full_cache_lock = threading.Lock()
_FULL_CACHE_BYTES = 96 * 1024 * 1024
_full_cache_bytes = 0
_BY_ID: dict[int, dict] = {}
_library_job = {"id": 0, "status": "idle", "message": "", "revision": 0}
_library_lock = threading.Lock()
_download_sources = {}
_inventory = {}
_export_lock = threading.Lock()

# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def safe_component(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")[:110].rstrip(". ")
    if re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", cleaned.split(".")[0]):
        cleaned = "_" + cleaned
    return cleaned or "unnamed"


def basename_of(path: str) -> str:
    parts = path.replace("\\", "/").rstrip("/").split("/")
    return parts[-1] if parts else "unnamed"


def image_like(path: str) -> bool:
    return path.lower().endswith(IMAGE_EXTS)


LANGUAGE_NAMES = {"ko": "韩文", "zhtw": "繁中"}
STORY_GROUPS = {"MC": "主线 MC", "AC": "活动 AC"}


def story_category(chapter):
    code = re.match(r"([A-Z]{2})\d", chapter.upper())
    if code:
        prefix = code[1]
        group = STORY_GROUPS.get(prefix, prefix)
    elif chapter.lower().startswith("memorystory"):
        group = "记忆剧情"
    else:
        group = "其他剧情"
    return f"剧情CG/{group}/{chapter}"


def classify_image(path):
    parts = path.replace("\\", "/").strip("/").split("/")
    folded = [p.casefold() for p in parts]
    lang = ""
    if len(parts) > 4 and folded[0] == "assets" and folded[2] == "localize":
        lang = folded[3]
        parts = parts[:2] + parts[4:]
        folded = folded[:2] + folded[4:]
    stem = parts[-1].rsplit(".", 1)[0].casefold()
    category = "其他贴图"
    kind = "utility"
    pair_key = None
    if folded[:3] == ["assets", "contents", "image"] and len(parts) >= 6:
        folder, chapter = folded[3], parts[4]
        if folder in ("still", "gallery"):
            kind = "cg" if folder == "still" else "gallery"
            key_stem = stem.removeprefix("thumb_") if folder == "gallery" else stem
            for code in ("ko", "zhtw"):
                if key_stem.endswith("_" + code) and (kind == "gallery" or lang == code):
                    key_stem = key_stem.removesuffix("_" + code)
                    lang = code
                    break
            if chapter.casefold() == "common":
                code = re.match(r"(?:still_)?([a-z]{2}\d{2}[a-z0-9]*)_", key_stem)
                if code:
                    chapter = code[1].upper()
            pair_key = (chapter.casefold(), key_stem, lang)
            category = story_category(chapter)
            if folder == "still" and folded[4] in {"bathroomscape", "bathroomwashingplace", "sky"}:
                category, kind = f"场景背景/{chapter}", "background"
            elif folder == "still" and (folded[4] == "time" or stem == "bg_gameover_black"):
                category, kind = f"剧情演出素材/{chapter}", "scene_part"
        elif folder == "background":
            category, kind = f"场景背景/{chapter}", "background"
        elif folder in ("advparts", "itemcut"):
            category, kind = f"剧情演出素材/{parts[3]}/{chapter}", "scene_part"
        else:
            category = f"其他贴图/图片素材/{parts[3]}"
    elif folded[:3] == ["assets", "contents", "character"] and len(parts) >= 5:
        if "card" in folded[4:-2] and (stem == folded[-2] or re.search(r"_r\d+[a-z]?$", stem)):
            category, kind = f"角色卡面/{parts[3]}", "card"
        else:
            category = f"其他贴图/角色附属/{parts[3]}"
    elif folded[:4] == ["assets", "contents", "duel", "character"] and len(parts) >= 8:
        if folded[-2] == "card" and stem == folded[-3]:
            category, kind = f"角色卡面/对决/{parts[4].removeprefix('Duel_')}", "card"
        else:
            category = "其他贴图/对决附属"
    elif folded[:2] == ["assets", "ui"]:
        category = "其他贴图/界面/" + "/".join(parts[2:4])
    elif len(parts) >= 3:
        category = f"其他贴图/{parts[2]}"
    return category, kind, lang, pair_key


# ----------------------------------------------------------------------------
# catalog.bin 索引（mmap）
# ----------------------------------------------------------------------------
def _u32(m, offset):
    return struct.unpack_from("<I", m, offset)[0]


def _make_catalog_reader(m):
    @lru_cache(maxsize=500_000)
    def auto_string(sid):
        if sid == UINT32_MAX:
            return None
        off = sid & STR_MASK
        n = _u32(m, off - 4)
        enc = "utf-16-le" if sid & UNI_STR else "ascii"
        return m[off:off + n].decode(enc, errors="replace")

    @lru_cache(maxsize=500_000)
    def read_string(sid, sep):
        if sid == UINT32_MAX:
            return None
        if not sid & DYN_STR:
            return auto_string(sid)
        parts = []
        seen = set()
        nxt = sid
        while nxt != UINT32_MAX:
            off = nxt & STR_MASK
            if off in seen:
                raise ValueError("目录字符串引用存在循环")
            seen.add(off)
            parts.append(auto_string(_u32(m, off)) or "")
            nxt = _u32(m, off + 4)
        return sep.join(reversed(parts))

    def read_key_string(kof):
        to, oo = _u32(m, kof), _u32(m, kof + 4)
        if read_string(_u32(m, to + 4), ".") != "System.String":
            return None
        sid = _u32(m, oo)
        sep = struct.unpack_from("<H", m, oo + 4)[0]
        return read_string(sid, chr(sep))

    return read_string, read_key_string


def build_index(progress=None, catalog_path=None):
    cat_file = catalog_path or CATALOG_FILE
    items = []
    cache_keys = {}
    with open(cat_file, "rb") as f:
        m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    try:
        read_string, read_key_string = _make_catalog_reader(m)
        keys_offset = _u32(m, 8)
        if not 4 <= keys_offset <= len(m):
            raise ValueError("目录索引位置无效")
        key_bytes = _u32(m, keys_offset - 4)
        if key_bytes % 8 or key_bytes > len(m) - keys_offset:
            raise ValueError("目录索引大小无效")
        key_count = key_bytes // 8
        log(f"开始扫描目录，共 {key_count} 个 key …")
        last = time.time()
        for i in range(key_count):
            if (i & 0x3FFF) == 0 and time.time() - last > 0.5:
                last = time.time()
                if progress:
                    progress(i, key_count)
            entry = keys_offset + i * 8
            try:
                path = read_key_string(_u32(m, entry))
            except Exception:
                continue
            if not path or not image_like(path):
                continue
            owners = []
            volume_only = True
            size = 0
            loc_set = _u32(m, entry + 4)
            try:
                loc_count = _u32(m, loc_set - 4) // 4
                for li in range(loc_count):
                    loc = _u32(m, loc_set + li * 4)
                    dep_set = _u32(m, loc + 12)
                    if dep_set == UINT32_MAX or _u32(m, dep_set - 4) < 4:
                        continue
                    # BundledAssetProvider loads the asset from the first dependency.
                    dep = _u32(m, dep_set)
                    name = read_string(_u32(m, dep), "/")
                    if not name or not re.fullmatch(r"[\w.-]+\.bundle", name, re.IGNORECASE):
                        continue
                    if volume_only:
                        try:
                            # ResourceLocation's type ID points to an assembly and class name.
                            type_offset = _u32(m, loc + 24)
                            volume_only = read_string(_u32(m, type_offset + 4), ".") == "UnityEngine.Texture3D"
                        except (struct.error, ValueError):
                            volume_only = False
                    if name in owners:
                        continue
                    owners.append(name)
                    try:
                        # Addressables AssetBundleRequestOptions holds the cache name and Hash128.
                        options = _u32(m, _u32(m, dep + 20) + 4)
                        hash_offset, name_id, _, bundle_size = struct.unpack_from("<4I", m, options)
                        size = max(size, bundle_size)
                        if name not in cache_keys:
                            cache_name = read_string(name_id, "_")
                            cache_hash = m[hash_offset:hash_offset + 16]
                            if (cache_name and re.fullmatch(r"[\w-]+", cache_name)
                                    and len(cache_hash) == 16 and any(cache_hash)):
                                cache_keys[name] = (cache_name, cache_hash.hex())
                    except (struct.error, ValueError):
                        pass
            except (struct.error, ValueError):
                pass
            if owners:
                item = {"p": path, "b": owners, "s": size, "w": None, "h": None}
                if volume_only:
                    item["texture3d"] = True
                items.append(item)
    finally:
        m.close()
    log(f"扫描完成，找到 {len(items)} 个候选图片资产。")
    return items, cache_keys


def _write_json(path, data):
    tmp = path.with_name(path.name + f".{uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_index_cache(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {"items": data}
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"items": []}


def _catalog_signature(path):
    stat = path.stat()
    return [INDEX_VERSION, str(path.resolve()), stat.st_mtime_ns, stat.st_size]


def _catalog_version(path):
    with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        match = re.search(rb"assets/(\d+\.\d+\.\d+)", m)
        return match[1].decode() if match else ""


def _path_key(path):
    return path.replace("\\", "/").casefold()


def _server_catalogs():
    user_cache = Path.home() / "AppData" / "LocalLow" / "wfs" / "HeavenBurnsRed" / "com.unity.addressables"
    game_cache = Path(_CFG.get("cache_dir") or user_cache)
    return [*game_cache.glob("catalog_*.bin"),
            *(REMOTE_DIR / "catalogs").glob("*/catalog_*.bin")]


def _catalog_candidates():
    for path in _server_catalogs():
        if not re.fullmatch(r"catalog_[a-zA-Z0-9_-]+\.bin", path.name):
            continue
        try:
            stat = path.stat()
            if stat.st_size <= 1_000_000:
                continue
            version = _catalog_version(path)
            if version:
                yield version, stat.st_mtime_ns, path
        except (OSError, ValueError):
            continue


def _version_key(version):
    return tuple(map(int, version.split(".")))


def find_server_catalog() -> Path | None:
    best = max(_catalog_candidates(), default=None, key=lambda x: (_version_key(x[0]), x[1]))
    return best[2] if best else None


def _catalog_endpoint(candidates):
    endpoints = {(version, path.stem) for version, _, path in candidates}
    try:
        settings = json.loads(CATALOG_FILE.with_name("settings.json").read_text(encoding="utf-8-sig"))
        for item in settings.get("m_CatalogLocations", []):
            if "AddressablesMainContentCatalogRemoteHash" not in item.get("m_Keys", []):
                continue
            match = re.fullmatch(r"https://assets/(\d+\.\d+\.\d+)/StandaloneWindows64/(catalog_[a-zA-Z0-9_-]+)\.hash",
                                 item.get("m_InternalId", ""))
            if match:
                endpoints.add(match.groups())
    except (OSError, ValueError):
        pass
    if not endpoints:
        raise ValueError("游戏没有提供可核对的服务器目录地址")
    return max(endpoints, key=lambda pair: (_version_key(pair[0]), pair[1]))


def _md5_file(path):
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sync_server_catalog():
    """Check the game's known catalog endpoint and cache a verified copy."""
    candidates = list(_catalog_candidates())
    version, name = _catalog_endpoint(candidates)
    root = f"{CDN_ROOT}/{version}/StandaloneWindows64/{name}"
    with urlopen(root + ".hash", timeout=15) as response:
        checksum = response.read(65).decode("ascii").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{32}", checksum):
        raise ValueError("服务器目录校验值无效")

    matches = [p for v, _, p in candidates if v == version and p.stem == name]
    if _catalog_version(CATALOG_FILE) == version:
        matches.append(CATALOG_FILE)
    for candidate in matches:
        if _md5_file(candidate) == checksum:
            return candidate, f"已核对服务器资源目录 {version} · 目录内容一致"

    dst = REMOTE_DIR / "catalogs" / version / f"{name}.bin"
    _cache_destination(dst.parent)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(f".{uuid4().hex}.part")
    try:
        with urlopen(root + ".bin", timeout=30) as response, tmp.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        if _md5_file(tmp) != checksum:
            raise ValueError("服务器资源目录未通过完整性校验")
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst, f"已更新服务器资源目录 {version}"


def _cache_destination(path):
    """Guard tool-managed writes against directory junctions and symlinks."""
    for directory in (path, *path.parents):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("工具缓存目录通过链接指向其他位置")


def _bundle_files(root):
    if not root.is_dir():
        return {}
    files = {}
    with os.scandir(root) as entries:
        for entry in entries:
            if not re.fullmatch(r"[\w.-]+\.bundle", entry.name, re.IGNORECASE):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_size:
                    files[entry.name.casefold()] = Path(entry.path)
            except OSError:
                continue
    return files


def _is_regular_file(info):
    return (stat.S_ISREG(info.st_mode) and
            not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _file_signature(path):
    info = path.stat(follow_symlinks=False)
    if not _is_regular_file(info):
        raise OSError("不是常规文件")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _matching_bundles(installed, downloaded):
    verified, skipped = {}, []
    for name in sorted(installed.keys() & downloaded.keys()):
        game, cached = installed[name], downloaded[name]
        try:
            signatures = _file_signature(game), _file_signature(cached)
            if signatures[0][2] != signatures[1][2]:
                raise ValueError("大小不同")
            with game.open("rb") as left, cached.open("rb") as right:
                while chunk := left.read(1024 * 1024):
                    if chunk != right.read(len(chunk)):
                        raise ValueError("内容不同")
                if right.read(1):
                    raise ValueError("内容不同")
            if signatures != (_file_signature(game), _file_signature(cached)):
                raise ValueError("校验期间文件发生变化")
            verified[name] = (game, cached, signatures)
        except (OSError, ValueError) as exc:
            skipped.append({"name": name, "reason": str(exc)})
    return verified, skipped


def _available_bundles(cache_keys, installed, downloaded, verified=None):
    available = defaultdict(list)
    # Keep the existing tool copy ahead of an unverified same-name installation file.
    for origin, files in (("download", downloaded), ("game", installed)):
        for name, path in files.items():
            if origin == "download" and name in (verified or {}):
                continue
            available[name].append((path, origin))
    unity_names = set()
    if UNITY_CACHE_DIR.is_dir():
        for bundle, (name, version_hash) in cache_keys.items():
            version_dir = UNITY_CACHE_DIR / name / version_hash
            path = version_dir / "__data"
            try:
                if path.stat().st_size and (version_dir / "__info").is_file():
                    key = bundle.casefold()
                    available[key].append((path, "game-cache"))
                    unity_names.add(key)
            except OSError:
                continue
    return available, len(unity_names)


def _bundle_tokens(cache_keys, bundles):
    return {name: hashlib.blake2s(
        f"{name.casefold()}:{cache_keys.get(name, ('', ''))[1]}".encode(), digest_size=8
    ).hexdigest() for name in bundles}


def _asset_token(bundle_token, key):
    return hashlib.blake2s(f"{bundle_token}:{key}".encode(), digest_size=8).hexdigest()


def _prepare_items(local, server, available, versions, cache_keys):
    """Merge catalog entries by resource path and retain older local bundle fallbacks."""
    rows, sources = {}, {}
    # A cached catalog may still include an untyped copy of the same volume texture.
    volume_paths = {_path_key(entry["p"]) for group in (local, server) for entry in group
                    if entry.get("texture3d")}
    catalogs = sorted(zip((local, server), versions),
                      key=lambda pair: _version_key(pair[1]) if pair[1] else ())
    for group, version in catalogs:
        for entry in group:
            if not entry["b"]:
                continue
            key = _path_key(entry["p"])
            if key in volume_paths:
                continue
            it = rows.setdefault(key, {**entry, "b": [], "key": key})
            it["b"] = list(dict.fromkeys(entry["b"] + it["b"]))
            if version:
                for name in entry["b"]:
                    prior_size = sources.get(name, ("", 0))[1]
                    # The catalog size is attributable if the image has a single owner.
                    sources[name] = (version, max(prior_size, entry["s"] if len(entry["b"]) == 1 else 0))
    tokens = _bundle_tokens(cache_keys, {b for entry in rows.values() for b in entry["b"]})
    originals, previews = defaultdict(list), defaultdict(list)
    downloadable = {}
    thumbnails = _cached_thumbnail_files()
    for it in rows.values():
        matches = [(b, path, origin) for b in it["b"]
                   for path, origin in available.get(b.casefold(), ())]
        it["available"] = [path for _, path, _ in matches]
        it["missing"] = not matches
        need = next((b for b in it["b"] if b in sources), None)
        it["download"] = need if it["missing"] else None
        it["origin"] = matches[0][2] if matches else ("server" if it["download"] else "missing")
        bundle = matches[0][0] if matches else (need or it["b"][0])
        bundle_token = tokens[bundle]
        it["bundle_token"] = bundle_token
        # An index can reuse a numeric ID for a different asset, even within the same bundle.
        it["token"] = _asset_token(bundle_token, it["key"])
        if len(it["b"]) > 1:
            it["fallback_tokens"] = [_asset_token(tokens[name], it["key"]) for name in it["b"] if name != bundle]
        it["cached"] = bool(thumbnails.get(_thumb_name(it)))
        if it["download"]:
            downloadable[need] = sources[need]
        category, kind, lang, key = classify_image(it["p"])
        it.update(c=category, kind=kind, language=LANGUAGE_NAMES.get(lang, lang),
                  preview_id=None, preview_of=None)
        if kind == "gallery":
            it["c"] = category.replace("剧情CG/", "游戏预览/", 1)
            previews[key].append(it)
        elif key is not None:
            originals[key].append(it)
    for key, group in previews.items():
        full = originals.get(key, ())
        if len(group) == len(full) == 1:
            original, preview = full[0], group[0]
            original["preview_id"] = preview["id"]
            preview["preview_of"] = original["id"]
    return list(rows.values()), downloadable


def load_library(rescan=False, progress=None, server_catalog=None, verify_duplicates=False):
    """Refresh changed catalogs and keep numerical IDs stable across local/server merges."""
    local_cache = _read_index_cache(INDEX_CACHE)
    server_cache = _read_index_cache(SERVER_INDEX_FILE)
    local_old, server_old = local_cache["items"], server_cache["items"]
    previous_ids = {}
    id_owners = {}
    collisions = set()
    for entry in local_old + server_old:
        key, iid = _path_key(entry["p"]), entry["id"]
        if iid in id_owners and id_owners[iid] != key:
            collisions.add(iid)
            continue
        previous_ids.setdefault(key, iid)
        id_owners[iid] = key
    next_id = max(max(id_owners, default=-1) + 1, local_cache.get("next_id", 0),
                  server_cache.get("next_id", 0))

    local_signature = _catalog_signature(CATALOG_FILE)
    local_changed = rescan or local_cache.get("signature") != local_signature
    local, local_cache_keys = (build_index() if local_changed else
                               (local_old, local_cache.get("cache_keys", {})))
    if not local:
        raise ValueError("游戏安装目录中没有可读的图片索引")
    local_version = _catalog_version(CATALOG_FILE) if local_changed else local_cache.get("version", "")

    server_catalog = find_server_catalog() if server_catalog is None else server_catalog
    server, server_signature = server_old, server_cache.get("signature")
    server_version, server_changed = server_cache.get("version", ""), False
    server_cache_keys = server_cache.get("cache_keys", {})
    if server_catalog:
        try:
            signature = _catalog_signature(server_catalog)
            candidate_version = _catalog_version(server_catalog)
            if (server_signature != signature and candidate_version and
                    (not server_version or _version_key(candidate_version) >= _version_key(server_version))):
                fresh, fresh_keys = build_index(catalog_path=server_catalog)
                if not fresh:
                    raise ValueError("游戏的更新目录中没有可读图片")
                server_version = candidate_version
                server, server_cache_keys = fresh, fresh_keys
                server_signature, server_changed = signature, True
        except (OSError, ValueError, struct.error) as exc:
            if progress:
                progress(f"游戏更新目录读取失败，已保留原有索引 · {exc}")
            log(f"游戏更新目录读取失败，使用已有索引：{exc}")
    if not server_signature:
        # A legacy cache recorded all dependencies; its first entry owns the asset.
        server = [{**it, "b": it["b"][:1]} for it in server]
    local_paths = {_path_key(it["p"]) for it in local}
    known_paths = {_path_key(it["p"]) for it in local_old + server_old}
    for group, changed in ((local, local_changed), (server, server_changed)):
        for it in group:
            key = _path_key(it["p"])
            if key not in previous_ids:
                previous_ids[key] = next_id
                next_id += 1
            it["id"] = previous_ids[key]
            if changed:
                it["new"] = int(key not in known_paths or (group is server and key not in local_paths))

    try:
        info = json.loads(INFO_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    if not isinstance(info, dict):
        info = {}

    installed = _bundle_files(GAME_BUNDLE_DIR)
    downloaded = _bundle_files(REMOTE_DIR)
    cache_keys = {**local_cache_keys, **server_cache_keys}
    verified, skipped = _matching_bundles(installed, downloaded) if verify_duplicates else ({}, [])
    available, unity_count = _available_bundles(cache_keys, installed, downloaded, verified)
    items, sources = _prepare_items(local, server, available, (local_version, server_version), cache_keys)
    if local_changed or collisions or next_id != local_cache.get("next_id"):
        _write_json(INDEX_CACHE, {"signature": local_signature, "items": local,
                                  "version": local_version, "next_id": next_id,
                                  "cache_keys": local_cache_keys})
    if server_changed or collisions:
        _write_json(SERVER_INDEX_FILE, {"signature": server_signature, "items": server,
                                        "version": server_version, "next_id": next_id,
                                        "cache_keys": server_cache_keys})

    for item in items:
        rec = info.get(str(item["id"]))
        # Retain recorded dimensions from the prior bundle-only key for existing IDs.
        if (item["id"] not in collisions and isinstance(rec, dict) and
                rec.get("token") in (item["token"], item["bundle_token"])):
            item["w"], item["h"] = rec.get("w"), rec.get("h")
    inventory = {"game": len(installed), "game-cache": unity_count, "download": len(downloaded),
                 "server": len(sources), "catalog": server_version or local_version}
    log(f"图库索引就绪：{len(items)} 张，{sum(it['missing'] for it in items)} 张缺少原图。")
    return items, sources, inventory, verified, skipped


def download_bundles(sources, progress):
    """Publish each verified download atomically, allowing other bundles to finish after errors."""
    _cache_destination(REMOTE_DIR)
    REMOTE_DIR.mkdir(parents=True, exist_ok=True)

    def download(entry):
        name, (version, expected_size) = entry
        dst = REMOTE_DIR / name
        tmp = dst.with_suffix(f".{uuid4().hex}.part")
        try:
            url = f"{CDN_ROOT}/{version}/StandaloneWindows64/{name}"
            req = Request(url, headers={"User-Agent": "UnityPlayer/2021.3.33f1"})
            with urlopen(req, timeout=25) as response, tmp.open("wb") as output:
                header = response.read(8)
                if not header.startswith((b"UnityFS\0", b"UnityRaw", b"UnityWeb")):
                    raise ValueError("响应不是 Unity 资源包")
                output.write(header)
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                received = output.tell()
                length = response.headers.get("Content-Length")
                if length is not None and received != int(length):
                    raise ValueError("资源包下载不完整")
                if expected_size and received != expected_size:
                    raise ValueError(f"资源包大小不符（{received} / {expected_size} 字节）")
            with tmp.open("rb") as stream:
                if not _UnityPy.load(stream, path=str(REMOTE_DIR)).container:
                    raise ValueError("资源包中没有可读取的资源")
            os.replace(tmp, dst)
            return name, None
        except Exception as exc:
            log(f"下载失败 {name}：{exc}")
            return name, str(exc)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError as exc:
                log(f"下载临时文件无法删除 {tmp.name}：{exc}")

    failures = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = [pool.submit(download, entry) for entry in sources.items()]
        for done, task in enumerate(as_completed(tasks), 1):
            name, error = task.result()
            if error:
                failures[name] = error
            progress(done, len(tasks))
    return failures


def load_selection():
    global SELECTED
    if SELECTION_FILE.exists():
        try:
            data = json.loads(SELECTION_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, list) or any(type(i) is not int for i in data):
                raise ValueError("已选图片列表无效")
            SELECTED = set(data)
            log(f"已加载勾选状态（{len(SELECTED)} 项）。")
            return
        except Exception as exc:
            log(f"勾选缓存损坏：{exc}")
    SELECTED = set()
    save_selection()


def save_selection():
    _write_json(SELECTION_FILE, sorted(SELECTED))


_last_info_save = 0.0
_info_lock = threading.Lock()


def persist_info(force=False):
    global _last_info_save
    with _info_lock:
        now = time.monotonic()
        if not force and now - _last_info_save < 2:
            return
        info = {str(it["id"]): {"w": it["w"], "h": it["h"], "token": it["token"]}
                for it in ITEMS if it["w"] and it["h"]}
        try:
            _write_json(INFO_FILE, info)
            _last_info_save = now
        except OSError as exc:
            log(f"分辨率缓存写入失败：{exc}")


# ----------------------------------------------------------------------------
# 纹理解码与缩略图
# ----------------------------------------------------------------------------
def _get_container(path):
    cache = getattr(_bundle_cache, "containers", None)
    if cache is None:
        cache = _bundle_cache.containers = OrderedDict()
    signature = (path, _file_signature(path))
    if signature in cache:
        cache.move_to_end(signature)
        return cache[signature]
    # UnityPy unpacks the bundle into memory. Close the underlying handle even
    # when the environment stays cached, so Windows can remove a redundant copy.
    with path.open("rb") as stream:
        env = _UnityPy.load(stream, path=str(path.parent))
    container = defaultdict(list)
    for name, obj in env.container.items():
        if obj.m_PathID:
            container[_path_key(name)].append(obj)
    # Keep the Environment alive so Sprite dependencies can still be read.
    cache[signature] = (env, container)
    if len(cache) > BUNDLE_LRU_CAP:
        cache.popitem(last=False)
    return cache[signature]


def _decode_image(item):
    candidates = [(item["p"], b) for b in item["available"]]
    for name, bundle in candidates:
        try:
            source = _get_container(bundle)
        except Exception as exc:
            log(f"资源包读取失败 {bundle}：{exc}")
            continue
        for obj in source[1].get(_path_key(name), ()) if source else ():
            try:
                if obj.type.name in ("Texture2D", "Sprite"):
                    image = obj.read().image
                    if image is not None:
                        return image if image.mode in ("RGB", "RGBA") else image.convert("RGBA")
            except Exception as exc:
                log(f"贴图读取失败 {name}（{bundle}）：{exc}")
    return None


def _thumb_name(item):
    return f"{item['id']}-{item['token']}.png"


def _thumb_path(item, width):
    return THUMB_DIR / str(width) / _thumb_name(item)


def _ready_thumbnail(path):
    try:
        return _file_signature(path)[2] > 0
    except OSError:
        return False


def _cached_thumbnail_files():
    folder = THUMB_DIR / str(THUMB_DEFAULT_W)
    files = {}
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if not THUMB_CACHE_NAME.fullmatch(entry.name):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    if _is_regular_file(info):
                        files[entry.name] = info.st_size
                except OSError:
                    continue
    except FileNotFoundError:
        pass
    return files


def _thumbnail_inventory(include_obsolete=False):
    items = ITEMS
    files = _cached_thumbnail_files()
    pending = []
    cached = unavailable = 0
    for item in items:
        name = _thumb_name(item)
        if files.get(name):
            cached += 1
        elif item["missing"]:
            unavailable += 1
        else:
            pending.append(item)
        if include_obsolete:
            files.pop(name, None)
            for token in item.get("fallback_tokens", ()):
                files.pop(f"{item['id']}-{token}.png", None)
    obsolete = list(files) if include_obsolete and items else []
    return {"total": len(items), "cached": cached, "pending": len(pending),
            "unavailable": unavailable, "obsolete": len(obsolete)}, pending, obsolete


def _request_thumb(item, width):
    dst = _thumb_path(item, width)
    key = (item["id"], item["token"], width)
    with _thumb_lock:
        if _ready_thumbnail(dst):
            return dst, "ready", None
        if item["missing"]:
            return None, "missing", None
        if key in _thumb_failed:
            return None, "failed", None
        existing = _thumb_inflight.get(key)
        if existing or len(_thumb_inflight) >= 48 or _stopping.is_set():
            return None, "pending", existing
        worker = _thumb_pool.submit(_generate_thumb, item, width, dst, key)
        _thumb_inflight[key] = worker
        return None, "pending", worker


def _generate_thumb(item, width, dst, key):
    size = None
    tmp = dst.with_suffix(f".{uuid4().hex}.tmp")
    try:
        image = _decode_image(item)
        if image is not None:
            original_size = image.size
            image.thumbnail((width, width), _RESAMPLE)
            _cache_destination(dst.parent)
            dst.parent.mkdir(parents=True, exist_ok=True)
            image.save(tmp, "PNG")
            os.replace(tmp, dst)
            size = original_size
    except Exception as exc:
        log(f"缩略图失败 {item['p']}：{exc}")
    finally:
        with _thumb_lock:
            _thumb_inflight.pop(key, None)
            current = _BY_ID.get(item["id"])
            if current and current["token"] == item["token"]:
                if size:
                    current["w"], current["h"] = size
                    if width == THUMB_DEFAULT_W:
                        current["cached"] = True
                        _thumb_ready.add(item["id"])
                elif current is item:
                    _thumb_failed.add(key)
        try:
            tmp.unlink(missing_ok=True)
        except OSError as exc:
            log(f"缩略图临时文件无法删除 {tmp.name}：{exc}")
    if size:
        persist_info()
    return size is not None


def _cleanup_thumbnails(names, counts):
    folder = THUMB_DIR / str(THUMB_DEFAULT_W)
    removed = released = skipped = done = 0
    total = len(names)
    last_report = 0.0

    def report():
        nonlocal last_report
        last_report = time.monotonic()
        counts["obsolete"] = total - done + skipped
        _job_progress(f"正在清理多余缩略图 · {done}/{total}", done=done, total=total,
                      thumbnail_counts=counts.copy())

    report()
    try:
        _cache_destination(folder)
    except (OSError, ValueError) as exc:
        log(f"缩略图缓存目录无法清理：{exc}")
        return {"removed": 0, "bytes": 0, "skipped": total}
    for name in names:
        if _stopping.is_set():
            break
        path = folder / name
        try:
            size = _file_signature(path)[2]
            path.unlink()
            removed += 1
            released += size
        except FileNotFoundError:
            pass
        except OSError as exc:
            skipped += 1
            log(f"缩略图清理跳过 {name}：{exc}")
        done += 1
        if done == total or time.monotonic() - last_report >= 0.2:
            report()
    return {"removed": removed, "bytes": released, "skipped": skipped}


def _sync_thumbnails(cleanup=False):
    counts, items, obsolete = _thumbnail_inventory(include_obsolete=cleanup)
    total = counts["pending"]
    # A manual check should record its result even when the cache needs no work.
    if not total and not cleanup:
        return {"saved": 0, "failed": 0}
    cleanup_result = _cleanup_thumbnails(obsolete, counts) if obsolete else None
    items.sort(key=lambda item: item["available"][0])
    with _thumb_lock:
        for item in items:
            _thumb_failed.discard((item["id"], item["token"], THUMB_DEFAULT_W))

    pending = deque()
    done = saved = 0
    last_report = 0.0

    def report():
        nonlocal last_report
        last_report = time.monotonic()
        _job_progress(f"正在同步本地缩略图 · {done}/{total}", done=done, total=total,
                      thumbnail_counts={**counts, "cached": counts["cached"] + saved,
                                        "pending": total - saved})

    def record(ok):
        nonlocal done, saved
        saved += bool(ok)
        done += 1
        if done == total or time.monotonic() - last_report >= 0.2:
            report()

    if total:
        report()
    for item in items:
        while not _stopping.is_set():
            file, status, worker = _request_thumb(item, THUMB_DEFAULT_W)
            if worker:
                pending.append(worker)
                if len(pending) >= THUMB_WORKERS:
                    record(pending.popleft().result())
                break
            if status != "pending":
                record(file is not None)
                break
            _stopping.wait(0.05)
        if _stopping.is_set():
            break
    while pending and not _stopping.is_set():
        record(pending.popleft().result())

    if saved:
        with _library_lock:
            _library_job["revision"] += 1
        persist_info(force=True)
    result = {"saved": saved, "failed": done - saved}
    if cleanup_result is not None:
        result["cleanup"] = cleanup_result
    if done == total and not _stopping.is_set():
        summary = {"time": time.time(), **result}
        with _state_lock:
            try:
                _save_preferences(last_thumbnail_sync=summary)
            except OSError as exc:
                _preferences["last_thumbnail_sync"] = summary
                log(f"缩略图同步记录写入失败：{exc}")
    return result


def _publish_library(items, sources, inventory):
    global ITEMS, _BY_ID, _download_sources, _inventory
    with _library_lock, _thumb_lock:
        old = _BY_ID
        for item in items:
            prior = old.get(item["id"])
            if prior and prior["token"] == item["token"]:
                if not item["w"]:
                    item["w"], item["h"] = prior["w"], prior["h"]
                if item["id"] in _thumb_ready and not item["cached"]:
                    item["cached"] = _ready_thumbnail(_thumb_path(item, THUMB_DEFAULT_W))
        ITEMS = items
        _BY_ID = {item["id"]: item for item in items}
        _download_sources = sources
        _thumb_ready.clear()
        _thumb_ready.update(item["id"] for item in items if item["cached"])
        _thumb_failed.clear()
        _inventory = inventory
        _library_job["revision"] += 1


def _publish_scan(scan):
    items, sources, inventory, _, _ = scan
    _publish_library(items, sources, inventory)


def _remove_matching_bundles(verified, skipped):
    removed = released = 0
    for name, (game, cached, signatures) in verified.items():
        try:
            _cache_destination(cached.parent)
            if cached.parent != REMOTE_DIR or cached.resolve() == game.resolve():
                raise ValueError("缓存位置与游戏目录重叠")
            if signatures != (_file_signature(game), _file_signature(cached)):
                raise ValueError("清理前文件发生变化")
            cached.unlink()
            removed += 1
            released += signatures[1][2]
        except (OSError, ValueError) as exc:
            skipped.append({"name": name, "reason": str(exc)})
    return {"removed": removed, "bytes": released, "skipped": skipped}


def _read_preferences():
    try:
        data = json.loads(PREFERENCES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        log(f"设置读取失败：{exc}")
        return
    if isinstance(data, dict):
        _preferences.update(data)
        last_check = data.get("last_check")
        if "last_thumbnail_sync" not in data and isinstance(last_check, dict) and "time" in last_check:
            thumbs = last_check.get("thumbs")
            if isinstance(thumbs, dict) and {"saved", "failed"} <= thumbs.keys():
                _preferences["last_thumbnail_sync"] = {"time": last_check["time"], **thumbs}


def _save_preferences(**changes):
    new = {**_preferences, **changes}
    _write_json(PREFERENCES_FILE, new)
    _preferences.update(changes)


def _settings_path():
    return Path(_preferences["out"]) if _preferences["out"] else OUTPUT_ROOT


def _export_directory(value):
    if not isinstance(value, str):
        raise ValueError("请输入绝对目录路径")
    value = value.strip()
    root = Path(value) if value else OUTPUT_ROOT
    if not root.is_absolute():
        raise ValueError("请输入完整的本地文件夹路径")
    root = root.resolve()
    protected = (GAME_BUNDLE_DIR.parents[3], UNITY_CACHE_DIR,
                 Path(_CFG.get("cache_dir") or Path.home() /
                      "AppData/LocalLow/wfs/HeavenBurnsRed/com.unity.addressables"),
                 REMOTE_DIR, THUMB_DIR)
    for directory in protected:
        if root.is_relative_to(directory.resolve()):
            raise ValueError("此目录属于游戏资源或图库缓存")
    return root


def _writable_directory(value):
    root = _export_directory(value)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".hbr-write-", dir=root):
        pass
    return root


def _ids(body):
    values = body.get("ids", [])
    if not isinstance(values, list) or any(type(i) is not int for i in values):
        raise ValueError("图片列表无效")
    return list(dict.fromkeys(values))


def _collection_rows():
    ids = {item["key"]: item["id"] for item in ITEMS}
    return [{"id": row["id"], "name": row["name"],
             "ids": [ids[key] for key in row["keys"] if key in ids],
             "count": len(row["keys"])} for row in _preferences["folders"]]


def _edit_collection(body):
    action = body.get("action")
    folders = list(_preferences["folders"])
    folder_id = body.get("id")
    index = next((i for i, row in enumerate(folders) if row["id"] == folder_id), None)
    if action != "create" and index is None:
        raise ValueError("收藏夹已不存在")
    if action in {"create", "rename"}:
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError("收藏夹名称需要 1 到 60 个字符")
        name = name.strip()
        if any(i != index and row["name"].casefold() == name.casefold()
               for i, row in enumerate(folders)):
            raise ValueError("已存在同名收藏夹")
        if action == "create":
            folder_id = uuid4().hex
            folders.append({"id": folder_id, "name": name, "keys": []})
        else:
            folders[index] = {**folders[index], "name": name}
    elif action == "delete":
        del folders[index]
    elif action in {"add", "remove"}:
        by_id = _BY_ID
        keys = {by_id[i]["key"] for i in _ids(body) if i in by_id}
        old = set(folders[index]["keys"])
        keys = old | keys if action == "add" else old - keys
        folders[index] = {**folders[index], "keys": sorted(keys)}
    else:
        raise ValueError("未知收藏夹操作")
    _save_preferences(folders=folders)
    return {"folders": _collection_rows(), "id": folder_id}


def _edit_selection(body):
    action = body.get("action")
    ids = set(_ids(body))
    if action == "add":
        new = SELECTED | ids.intersection(_BY_ID)
    elif action == "remove":
        new = SELECTED - ids
    elif action == "clear":
        new = set()
    else:
        raise ValueError("未知勾选操作")
    _write_json(SELECTION_FILE, sorted(new))
    SELECTED.clear()
    SELECTED.update(new)
    return {"ids": sorted(SELECTED)}


def _has_preview(item, lookup):
    paired = lookup.get(item["preview_id"])
    return (not item["missing"] or item["cached"] or
            (paired and (not paired["missing"] or paired["cached"])))


def _needed_bundles(action, ids):
    lookup = _BY_ID
    names = set()
    for iid in ids:
        item = lookup.get(iid)
        if not item:
            continue
        if action == "preview":
            if _has_preview(item, lookup):
                continue
            paired = lookup.get(item["preview_id"])
            if paired and paired["download"]:
                item = paired
        if item["download"]:
            names.add(item["download"])
    return {name: source for name, source in _download_sources.items() if name in names}


def _estimate(action, ids):
    with _library_lock:
        bundles = _needed_bundles(action, ids)
        revision = _library_job["revision"]
    return {"bundles": len(bundles), "bytes": sum(source[1] for source in bundles.values()),
            "unknown": sum(not source[1] for source in bundles.values()), "revision": revision}


def _job_progress(message, **details):
    with _library_lock:
        _library_job.update(message=message, **details)


def _run_refresh():
    warnings = []
    server_catalog = None
    try:
        server_catalog, message = sync_server_catalog()
    except (OSError, ValueError) as exc:
        message = f"服务器核对失败 · {exc}"
        warnings.append(message)
    _job_progress("正在扫描本地 Bundle 并核对重复缓存")
    scan = load_library(server_catalog=server_catalog, verify_duplicates=True, progress=warnings.append)
    _publish_scan(scan)
    cleanup = _remove_matching_bundles(scan[3], scan[4])
    if scan[3] and len(scan[3]) != cleanup["removed"]:
        # Restore any fallback that could not be removed because it changed or was locked.
        _publish_scan(load_library(server_catalog=False))
    elif cleanup["removed"]:
        with _library_lock:
            _inventory["download"] -= cleanup["removed"]

    sources = _download_sources.copy()
    failures = {}
    if sources:
        _job_progress(f"正在下载待补齐的 Bundle · 0/{len(sources)}", done=0, total=len(sources))
        try:
            failures = download_bundles(
                sources, lambda done, total: _job_progress(
                    f"正在下载待补齐的 Bundle · {done}/{total}", done=done, total=total))
        except (OSError, ValueError) as exc:
            failures = dict.fromkeys(sources, str(exc))
        _publish_scan(load_library(server_catalog=False))

    thumbs = _sync_thumbnails()
    downloaded = len(sources) - len(failures)
    summary = {"time": time.time(), "message": message, "warnings": warnings,
               "downloaded": downloaded, "bundle_failures": failures, "thumbs": thumbs, **cleanup}
    try:
        with _state_lock:
            _save_preferences(last_check=summary)
    except OSError as exc:
        warnings.append(f"检查结果无法写入设置 · {exc}")
        with _state_lock:
            _preferences["last_check"] = summary
    result = f"Bundle 状态已更新 · 下载 {downloaded} 个 · 清理 {cleanup['removed']} 个"
    if failures:
        result += f" · 下载失败 {len(failures)} 个"
    if thumbs["saved"] or thumbs["failed"]:
        result += f" · 缩略图保存 {thumbs['saved']} 张"
    if thumbs["failed"]:
        result += f" · 缩略图失败 {thumbs['failed']} 张"
    if warnings:
        result += " · 检查有提示"
    return result, {"cleanup": cleanup, "warnings": warnings, "downloaded": downloaded,
                    "bundle_failures": failures, "thumbs": thumbs}


def _start_library_job(action, ids=None, expected_revision=None):
    with _library_lock:
        if _library_job["status"] == "running":
            return None
        if expected_revision is not None and expected_revision != _library_job["revision"]:
            raise ValueError("Bundle 状态已改变，请重新提交此操作")
        message = {"refresh": "正在核对服务器资源目录", "thumbnails": "正在检查本地缩略图缓存",
                   "export": "正在准备保存", "preview": "正在准备加载预览"}[action]
        job = {"id": _library_job["id"] + 1, "status": "running", "message": message,
               "revision": _library_job["revision"], "action": action}
        _library_job.clear()
        _library_job.update(job)
    with _state_lock:
        out = _settings_path()

    def run():
        try:
            if action == "refresh":
                message, details = _run_refresh()
            elif action == "thumbnails":
                thumbs = _sync_thumbnails(cleanup=True)
                details = {"thumbs": thumbs}
                message = "缩略图检查完成 · 无需补全或清理"
                cleanup = thumbs.get("cleanup")
                if thumbs["saved"] or thumbs["failed"] or cleanup:
                    message = f"缩略图补全结束 · 保存 {thumbs['saved']} 张 · 失败 {thumbs['failed']} 张"
                if cleanup:
                    message += f" · 清理 {cleanup['removed']} 张"
                    if cleanup["skipped"]:
                        message += f" · 跳过 {cleanup['skipped']} 张"
            else:
                if action == "export":
                    _writable_directory(str(out))
                sources = _needed_bundles(action, ids)
                failures = {}
                if sources:
                    failures = download_bundles(
                        sources, lambda done, total: _job_progress(
                            f"正在下载 Bundle · {done}/{total}", done=done, total=total))
                    _publish_scan(load_library(server_catalog=False))
                details = {"bundle_failures": failures}
                if action == "export":
                    details.update(run_export(ids, out, failures))
                    message = (f"保存完成 · {details['ok']} 成功 / {details['skipped']} 跳过 / "
                               f"{details['failed']} 失败")
                else:
                    details["failed_ids"] = [iid for iid in ids if iid not in _BY_ID or
                                             not _has_preview(_BY_ID[iid], _BY_ID)]
                    failed = len(details["failed_ids"])
                    message = (f"预览已准备 · {failed} 张仍缺少预览" if failed else "预览已准备")
            _job_progress(message, status="done", **details)
        except Exception as exc:
            log(f"图库操作失败：{exc}")
            _job_progress(f"操作失败 · {exc}", status="error", failed_ids=ids or [])

    threading.Thread(target=run, daemon=True, name="gallery-job").start()
    return job


def export_filename(item):
    stem = basename_of(item["p"]).rsplit(".", 1)[0]
    lang = item["language"]
    if lang and not stem.casefold().endswith(("_ko", "_zhtw")):
        stem += "_" + lang
    return safe_component(stem) + ".png"


def export_path(item, out_root):
    return out_root.joinpath(*(safe_component(part) for part in item["c"].split("/")),
                             export_filename(item))



def _export_one(item, root):
    destination = export_path(item, root)
    folder = _export_directory(str(destination.parent))
    if not folder.is_relative_to(root.resolve()):
        raise ValueError("分类目录指向输出目录之外")
    key = item["key"]
    token = item["token"]
    if destination.is_file() and not destination.is_symlink():
        try:
            with PILImage.open(destination, formats=("PNG",)) as saved:
                if saved.info.get("HBR-Asset") == key and saved.info.get("HBR-Version") == token:
                    return "skipped"
        except (OSError, PILImage.DecompressionBombError):
            pass

    image = _decode_image(item)
    if image is None:
        raise ValueError("原始贴图无法解码")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_itxt("HBR-Asset", key)
    metadata.add_itxt("HBR-Version", token)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".hbr-{uuid4().hex}.tmp"
    try:
        image.save(tmp, "PNG", pnginfo=metadata)
        with _export_lock:
            for counter in range(10_000):
                name = destination.name if not counter else f"{destination.stem}__{counter}.png"
                candidate = folder / name
                if not candidate.exists():
                    try:
                        if os.name == "nt":
                            tmp.rename(candidate)
                        else:
                            os.link(tmp, candidate)
                        return "ok"
                    except FileExistsError:
                        continue
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                try:
                    with PILImage.open(candidate, formats=("PNG",)) as saved:
                        saved_key = saved.info.get("HBR-Asset")
                        if saved_key == key and saved.info.get("HBR-Version") == token:
                            return "skipped"
                        if ((saved_key is None or saved_key == key) and image.size == saved.size and
                                image.convert("RGBA").tobytes() == saved.convert("RGBA").tobytes()):
                            return "skipped"
                except (OSError, PILImage.DecompressionBombError):
                    continue
        raise OSError("分类目录中有过多同名文件")
    finally:
        tmp.unlink(missing_ok=True)


def run_export(ids, out_root, bundle_failures):
    snapshot = _BY_ID
    groups = defaultdict(list)
    for iid in ids:
        item = snapshot.get(iid)
        bundle = item["available"][0] if item and item["available"] else None
        groups[bundle].append(iid)

    ok = skipped = done = 0
    failures = []
    progress_lock = threading.Lock()

    def export(iid):
        item = snapshot.get(iid)
        if not item:
            return "", "索引中暂时找不到该图片"
        if item["missing"]:
            return item["p"], bundle_failures.get(item["download"], "本机和服务器均无可用原图")
        try:
            return item["p"], _export_one(item, out_root)
        except Exception as exc:
            return item["p"], str(exc)

    def export_group(group):
        nonlocal ok, skipped, done
        for iid in group:
            path, result = export(iid)
            with progress_lock:
                if result == "ok":
                    ok += 1
                elif result == "skipped":
                    skipped += 1
                else:
                    failures.append({"id": iid, "path": path, "reason": result})
                done += 1
                _job_progress(f"正在保存图片 · {done}/{len(ids)}", done=done, total=len(ids))

    with ThreadPoolExecutor(max_workers=EXPORT_WORKERS) as executor:
        for _ in executor.map(export_group, groups.values()):
            pass
    return {"ok": ok, "skipped": skipped, "failed": len(failures), "failures": failures,
            "failed_ids": [item["id"] for item in failures], "out": str(out_root)}


def index_payload():
    with _thumb_lock:
        items = [{key: item[key] for key in
                  ("id", "p", "c", "w", "h", "missing", "kind", "preview_id",
                   "preview_of", "origin", "language", "token")} |
                 {"new": item.get("new", 0), "cached": item["id"] in _thumb_ready,
                  "downloadable": bool(item["download"])} for item in ITEMS]
        revision = _library_job["revision"]
    return {"items": items, "revision": revision}


def _full_image(item):
    global _full_cache_bytes
    key = (item["id"], item["token"])
    with _full_cache_lock:
        data = _full_cache.get(key)
        if data is not None:
            _full_cache.move_to_end(key)
            return data
    image = _decode_image(item)
    if image is None:
        return None
    with _thumb_lock:
        current = _BY_ID.get(item["id"])
        if current and current["token"] == item["token"]:
            current["w"], current["h"] = image.size
    persist_info()
    buf = io.BytesIO()
    image.save(buf, "PNG")
    data = buf.getvalue()
    if len(data) <= _FULL_CACHE_BYTES:
        with _full_cache_lock:
            previous = _full_cache.pop(key, b"")
            _full_cache_bytes -= len(previous)
            while _full_cache_bytes + len(data) > _FULL_CACHE_BYTES:
                _full_cache_bytes -= len(_full_cache.popitem(last=False)[1])
            _full_cache[key] = data
            _full_cache_bytes += len(data)
    return data


class GalleryServer(ThreadingHTTPServer):
    request_queue_size = 64
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, data, ctype, status=200, cache="no-store", **headers):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), str(value))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _json(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 3_000_000:
            raise ValueError("请求大小无效")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("请求格式无效")
        return data

    def log_message(self, *a):
        pass

    def _local_request(self, mutation=False):
        host = f"127.0.0.1:{self.server.server_port}"
        origin = self.headers.get("Origin")
        fetch_site = self.headers.get("Sec-Fetch-Site")
        return (self.headers.get("Host") == host and
                fetch_site in (None, "none", "same-origin") and
                (not origin or origin == "http://" + host) and
                (not mutation or self.headers.get_content_type() == "application/json"))

    def do_GET(self):
        if not self._local_request():
            self._json({"error": "仅接受本机图库请求"}, 403)
            return
        url = urlparse(self.path)
        file_info = {"/": ("index.html", "text/html"), "/app.css": ("app.css", "text/css"),
                     "/app.js": ("app.js", "text/javascript")}.get(url.path)
        try:
            if file_info:
                self._send((WEB_DIR / file_info[0]).read_bytes(), file_info[1] + "; charset=utf-8",
                           Content_Security_Policy="default-src 'self'; img-src 'self' blob: data:; "
                                                   "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            elif url.path == "/index":
                self._json(index_payload())
            elif url.path == "/state":
                with _state_lock:
                    self._json({"ids": sorted(SELECTED), "out": str(_settings_path()),
                                "folders": _collection_rows(), "picker": _picker_available.is_set()})
            elif url.path == "/status":
                with _library_lock:
                    payload = {"job": _library_job.copy(), "inventory": _inventory.copy()}
                with _state_lock:
                    payload["last_check"] = _preferences["last_check"]
                self._json(payload)
            elif url.path == "/thumbnail-status":
                counts, _, _ = _thumbnail_inventory(include_obsolete=True)
                with _state_lock:
                    last_sync = _preferences.get("last_thumbnail_sync")
                self._json({"counts": counts, "last_sync": last_sync})
            elif url.path in {"/thumb", "/full"}:
                self._asset(url)
            else:
                self._json({"error": "页面不存在"}, 404)
        except (OSError, ValueError) as exc:
            self._json({"error": str(exc)}, 400)

    def _asset(self, url):
        query = parse_qs(url.query)
        iid = int(query.get("id", ["-1"])[0])
        item = _BY_ID.get(iid)
        if not item:
            self._json({"error": "图片不存在"}, 404)
            return
        version = query.get("v", [""])[0]
        if version and version != item["token"]:
            self._json({"error": "图片来源已更新"}, 409)
            return
        if url.path == "/full":
            data = None if item["missing"] else _full_image(item)
            if data is not None:
                self._send(data, "image/png")
            else:
                self._json({"error": "原始贴图无法读取"}, 404)
            return
        width = int(query.get("w", [str(THUMB_DEFAULT_W)])[0])
        if not 32 <= width <= 1024:
            raise ValueError("缩略图尺寸无效")
        file, status, _ = _request_thumb(item, width)
        if file:
            self._send(file.read_bytes(), "image/png",
                       cache="private, max-age=86400" if version else "no-store",
                       X_Image_Width=item["w"] or 0, X_Image_Height=item["h"] or 0)
        else:
            self._json({"status": status}, 202 if status == "pending" else 404)

    def do_POST(self):
        if not self._local_request(mutation=True):
            self.close_connection = True
            self._json({"error": "仅接受本机图库请求"}, 403)
            return
        try:
            body = self._read_body()
            self._post(urlparse(self.path).path, body)
        except (OSError, ValueError, RuntimeError) as exc:
            self._json({"error": str(exc)}, 400)

    def _post(self, path, body):
        if path == "/selection":
            with _state_lock:
                result = _edit_selection(body)
        elif path == "/collections":
            with _state_lock:
                result = _edit_collection(body)
        elif path == "/settings":
            value = body.get("out")
            out = _writable_directory(value)
            with _state_lock:
                _save_preferences(out=str(out) if value.strip() else "")
            result = {"out": str(out)}
        elif path == "/openout":
            if "job" in body:
                with _library_lock:
                    out = _library_job.get("out") if body["job"] == _library_job["id"] else None
                if not out:
                    raise ValueError("找不到该任务的保存目录")
            else:
                with _state_lock:
                    out = _settings_path()
            os.startfile(_writable_directory(str(out)))
            result = {}
        elif path == "/pick":
            if not _picker_available.is_set():
                raise ValueError("无法打开系统文件夹窗口，请直接输入路径")
            if not _picker_lock.acquire(blocking=False):
                self._json({"error": "文件夹选择窗口已打开"}, 409)
                return
            response = queue.Queue()
            _picker_requests.put((body.get("out", ""), response))
            try:
                selection, error = response.get(timeout=300)
                if error:
                    raise ValueError(error)
                result = {"out": selection}
            except queue.Empty:
                raise ValueError("文件夹选择已超时") from None
            finally:
                _picker_lock.release()
        elif path in {"/estimate", "/jobs"}:
            action = body.get("action")
            allowed = ("preview", "export") if path == "/estimate" else ("preview", "export", "refresh", "thumbnails")
            if action not in allowed:
                raise ValueError("未知图库操作")
            ids = _ids(body)
            if action in ("preview", "export") and not ids:
                raise ValueError("请先选择图片")
            if path == "/estimate":
                result = _estimate(action, ids)
            else:
                job = _start_library_job(action, ids, body.get("revision"))
                if job is None:
                    self._json({"error": "图库正在运行另一项任务"}, 409)
                else:
                    self._json(job, 202)
                return
        else:
            self._json({"error": "操作不存在"}, 404)
            return
        self._json(result)


def _fatal(msg: str) -> None:
    """致命错误：控制台与图形弹窗双通道提示（.pyw 无控制台也能看到）。"""
    print(msg, flush=True)
    try:
        import tkinter as tk
        from tkinter import messagebox
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showerror("HBR 壁纸图库", msg)
        _r.destroy()
    except Exception:
        pass
    raise SystemExit(msg)



def main():
    global _thumb_pool
    if sys.version_info < (3, 11):
        _fatal("请使用 Python 3.11 或更高版本运行图库。")
    if _config_error is not None:
        _fatal(f"无法读取 {CONFIG_FILE.name}。\n{_config_error}")
    if not PIL_OK or not UNITYPY_OK:
        missing = [name for name, ok in (("Pillow", PIL_OK), ("UnityPy", UNITYPY_OK)) if not ok]
        _fatal("缺少依赖：" + "、".join(missing) +
               "\n\n请在本文件夹打开命令行后执行：\n    python -m pip install -r requirements.txt")
    if not GAME_BUNDLE_DIR.is_dir() or not CATALOG_FILE.is_file():
        _fatal("未找到游戏资源目录：\n" + str(GAME_BUNDLE_DIR) +
               "\n\n请确认游戏已安装，或在 hbr_config.json 中填写实际的 StandaloneWindows64 目录。")

    stop = _stopping
    state = {}
    _read_preferences()
    load_selection()

    def start():
        global _thumb_pool
        try:
            _publish_scan(load_library(rescan="--rescan" in sys.argv))
            if stop.is_set():
                return
            _thumb_pool = ThreadPoolExecutor(max_workers=THUMB_WORKERS)
            server = GalleryServer(("127.0.0.1", 0), Handler)
            state["server"] = server
            url = f"http://127.0.0.1:{server.server_port}/"
            state["url"] = url
            log(f"本地图库已启动：{url}")
            webbrowser.open(url)
            server.serve_forever()
        except Exception as exc:
            state["error"] = str(exc)
            stop.set()

    def window():
        import tkinter as tk
        from tkinter import filedialog, ttk
        root = tk.Tk()
        root.title("HBR 壁纸图库")
        root.geometry("480x174")
        root.configure(bg="#151920")
        label = tk.Label(root, text="正在读取图库索引…", fg="#ecedf0", bg="#151920",
                         font=("Microsoft YaHei", 11), justify="left", wraplength=430)
        label.pack(padx=12, pady=14, fill="both", expand=True)

        def close():
            stop.set()
            root.destroy()

        def update():
            if stop.is_set():
                root.destroy()
                return
            if state.get("url"):
                label.config(text="图库正在运行\n" + state["url"])
            try:
                value, response = _picker_requests.get_nowait()
            except queue.Empty:
                pass
            else:
                try:
                    initial = Path(value) if isinstance(value, str) and value else _settings_path()
                    if not initial.is_dir():
                        initial = Path.home()
                    root.lift()
                    selection = filedialog.askdirectory(parent=root, initialdir=str(initial),
                                                        title="选择图片保存目录", mustexist=True)
                    response.put((selection, None))
                except (OSError, ValueError, tk.TclError) as exc:
                    response.put(("", str(exc)))
            if not stop.is_set():
                root.after(150, update)

        root.protocol("WM_DELETE_WINDOW", close)
        ttk.Button(root, text="退出图库", command=close).pack(pady=(0, 14))
        _picker_available.set()
        root.after(150, update)
        try:
            root.mainloop()
        finally:
            _picker_available.clear()

    worker = threading.Thread(target=start, daemon=True, name="gallery-server")
    worker.start()
    try:
        try:
            window()
        except Exception as exc:
            log(f"窗口无法打开，图库继续在浏览器运行：{exc}")
            stop.wait()
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        server = state.get("server")
        if server is not None and worker.is_alive():
            server.shutdown()
            server.server_close()
        if _thumb_pool is not None:
            _thumb_pool.shutdown(wait=True, cancel_futures=True)
        if ITEMS:
            persist_info(force=True)
    if state.get("error"):
        _fatal("图库启动失败：\n" + state["error"])


if __name__ == "__main__":
    main()
