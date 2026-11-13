# -*- coding: utf-8 -*-
"""
Heaven Burns Red 壁纸图库提取器

一个本地 Web 图库工具：扫描游戏目录里的图片图集，以缩略图墙展示，
支持逐张勾选、分类过滤、搜索、壁纸尺寸筛选，并把选中的图片批量导出为 PNG 当壁纸。

- 只读游戏安装目录（不修改、不写入）。
- 两万多个 bundle 全部在本机，无需联网下载。
- 目录索引用 mmap 解析超大 catalog.bin，避免 OOM。
- 缩略图按需后台解码（固定 2 线程节流）+ 磁盘缓存，低内存机器也稳。

用法：双击本文件（.pyw）运行，自动打开浏览器访问本地图库。
"""

from __future__ import annotations

import gc
import io
import json
import os
import mmap
import re
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from socket import AF_INET, SOCK_STREAM
import socket

try:
    from PIL import Image as PILImage
    PIL_OK = True
except Exception as _exc:
    PILImage = None
    PIL_OK = False
    print("警告：Pillow 不可用，将无法生成缩略图：", _exc)
try:
    _RESAMPLE = PILImage.Image.Resampling.LANCZOS
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
CHECKPOINT_FILE = TOOL_DIR / "hbr_progress.txt"
CONFIG_FILE = TOOL_DIR / "hbr_config.json"           # 可选：游戏数据目录/缓存目录配置
SERVER_INDEX_FILE = TOOL_DIR / "hbr_index_server.json"  # 服务器缺失新图（仅检测列出，下载待接通）
REMOTE_DIR = TOOL_DIR / "remote_q"        # 从服务器下载的缺失 bundle（待游戏目录本地化）
CDN_HOST = "heaven-burns-red-assets.akamaized.net"
CDN_VERSION = "6.10.10"                    # 从最新缓存 catalog 的 assets/{v} 提取（见 find_server_version）

# 游戏数据目录：默认指向本机游戏路径，也可在 hbr_config.json 里写 {"game_dir": "..."} 覆盖
_CFG = {}
try:
    if CONFIG_FILE.exists():
        import json as _json
        _CFG = _json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
except Exception:
    pass
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

THUMB_DEFAULT_W = 256
BUNDLE_LRU_CAP = 8     # 内存中 bundle Environment 的数量上限
THUMB_WORKERS = 4      # 缩略图并发解码线程数（LRU 已增至 8，4 线程兼顾速度与内存）
EXPORT_WORKERS = 6     # 导出并发线程数

UINT32_MAX = 0xFFFFFFFF
DYN_STR = 0x40000000
UNI_STR = 0x80000000
STR_MASK = 0x3FFFFFFF
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp")

# ----------------------------------------------------------------------------
# 运行状态
# ----------------------------------------------------------------------------
ITEMS: list[dict] = []                       # {id,p,c,b,s,w,h}
SELECTED: set[int] = set()
EXP = {"running": False, "total": 0, "done": 0, "ok": 0,
       "skipped": 0, "failed": 0, "phase": "", "log": [], "finished": False}
_state_lock = threading.Lock()

_env_cache: OrderedDict[str, object] = OrderedDict()
_env_lock = threading.Lock()
_thumb_pool: ThreadPoolExecutor | None = None
_thumb_inflight: set[int] = set()
_thumb_status: dict[int, str] = {}   # id -> pending/ok/fail
_thumb_stat = {"done": 0, "fail": 0}   # 已成功/失败 的缩略图数量
_thumb_lock = threading.Lock()
_full_cache: OrderedDict[int, bytes] = OrderedDict()   # 原图bytes内存缓存（LRU，上限64张），仅热内存不落盘
_full_cache_lock = threading.Lock()
_FULL_CACHE_MAX = 64

# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _state_lock:
        EXP["log"].append(line)
        if len(EXP["log"]) > 400:
            del EXP["log"][:100]
    print(line, flush=True)


def safe_component(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", name).strip(". ")
    return cleaned or "unnamed"


def basename_of(path: str) -> str:
    parts = path.replace("\\", "/").rstrip("/").split("/")
    return parts[-1] if parts else "unnamed"


def image_like(path: str) -> bool:
    return path.lower().endswith(IMAGE_EXTS)


def category_of(path: str) -> str:
    parts = path.replace("\\", "/").lstrip("/").split("/")
    if len(parts) >= 5 and parts[:4] == ["Assets", "Contents", "Image", "Still"]:
        ch = parts[4]
        up = ch.upper()
        for g in ("AC", "CC", "DC", "MC", "SC"):
            if up.startswith(g):
                ch = g
                break
        if up.startswith("TIME"):
            ch = "Time"
        elif up.startswith("MEMORY"):
            ch = "MemoryStory"
        return "剧情CG/" + ch
    if (parts[0] == "Assets" and "Card" in parts
            and basename_of(path).rsplit(".", 1)[0] == parts[-2]):
        return "角色卡牌"
    third = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else parts[0])
    return "其他/" + third


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
        nxt = sid
        while nxt != UINT32_MAX:
            off = nxt & STR_MASK
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


def build_index(progress=None, catalog_path=None) -> list[dict]:
    cat_file = catalog_path or CATALOG_FILE
    items = []
    with open(cat_file, "rb") as f:
        m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    try:
        read_string, read_key_string = _make_catalog_reader(m)
        keys_offset = _u32(m, 8)
        key_count = _u32(m, keys_offset - 4) // 8
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
            bundles = []
            size = 0
            loc_set = _u32(m, entry + 4)
            try:
                loc_count = _u32(m, loc_set - 4) // 4
                for li in range(loc_count):
                    loc = _u32(m, loc_set + li * 4)
                    dep_set = _u32(m, loc + 12)
                    if dep_set == UINT32_MAX:
                        continue
                    dc = _u32(m, dep_set - 4) // 4
                    for di in range(dc):
                        do = _u32(m, dep_set + di * 4)
                        bn = read_string(_u32(m, do), "/")
                        if bn and bn.lower().endswith(".bundle") and bn not in bundles:
                            bundles.append(bn)
                            try:
                                size = _u32(m, _u32(m, _u32(m, do + 20) + 4) + 12)
                            except Exception:
                                pass
            except Exception:
                pass
            if bundles:
                items.append({"id": len(items), "p": path, "c": category_of(path),
                              "b": bundles, "s": size, "w": None, "h": None})
    finally:
        m.close()
    log(f"扫描完成，找到 {len(items)} 个图片资产。")
    return items


def load_or_build_index(force=False, progress=None) -> list[dict]:
    raw = None
    if not force and INDEX_CACHE.exists():
        try:
            raw = json.loads(INDEX_CACHE.read_text(encoding="utf-8"))
            log(f"从缓存加载索引（{len(raw)} 个资产）。")
        except Exception as exc:
            log(f"索引缓存损坏，重新扫描：{exc}")
    if raw is None:
        t0 = time.perf_counter()
        new_items = build_index(progress)
        # 复用旧索引的 id（按路径），保证重扫后已生成缩略图/勾选状态不失效
        old_map = {}
        if INDEX_CACHE.exists():
            try:
                for o in json.loads(INDEX_CACHE.read_text(encoding="utf-8")):
                    old_map[o["p"]] = o["id"]
            except Exception:
                pass
        have = set(old_map.values())
        nxt = (max(have) + 1) if have else 0
        for it in new_items:
            # 可靠地标记"本次更新新增的图"：路径未出现在旧索引即视为更新新增
            it["new"] = 0 if it["p"] in old_map else 1
            oid = old_map.get(it["p"])
            if oid is not None:
                it["id"] = oid
            else:
                while nxt in have:
                    nxt += 1
                it["id"] = nxt
                have.add(nxt)
                nxt += 1
        raw = new_items
        INDEX_CACHE.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        log(f"索引已保存，耗时 {time.perf_counter() - t0:.1f}s；本次新增 {sum(i['new'] for i in raw)} 张。")
    if INFO_FILE.exists():
        try:
            info = json.loads(INFO_FILE.read_text(encoding="utf-8"))
            for it in raw:
                rec = info.get(str(it["id"]))
                if rec:
                    it["w"], it["h"] = rec.get("w"), rec.get("h")
        except Exception:
            pass
    return raw


# ----------------------------------------------------------------------------
# 服务器补全：从最新缓存目录检测“服务器上有、本地没有”的新图（仅列出，不下载）
# ----------------------------------------------------------------------------
_BY_ID: dict = {}               # id -> item，用于 HTTP 按 id 精确查找（远端 id 与下标不一致）



def find_server_version() -> str:
    """从最新服务器 catalog 内嵌的 assets/{version} 提取实时版本；失败回退默认。"""
    global CDN_VERSION
    try:
        cat = find_server_catalog()
        if cat is not None:
            with open(cat, "rb") as f:
                data = f.read()
            m = re.search(rb"assets/(\d+\.\d+\.\d+)", data)
            if m:
                CDN_VERSION = m.group(1).decode()
                return CDN_VERSION
    except Exception:
        pass
    return CDN_VERSION


def download_bundles(bundle_names, progress=None):
    """把缺失 bundle 下载到 REMOTE_DIR（幂等：已存在且非空则跳过）。
    返回 (ok, fail)。进度回调 progress(done,total)。"""
    import urllib.request
    REMOTE_DIR.mkdir(parents=True, exist_ok=True)
    if not bundle_names:
        return (0, 0)
    total = len(bundle_names); done = 0; fail = 0
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def _one(bn):
        dst = REMOTE_DIR / bn
        if dst.is_file() and dst.stat().st_size > 0:
            return (bn, True)
        url = f"https://{CDN_HOST}/prod/assets/{CDN_VERSION}/StandaloneWindows64/{bn}"
        tmp = dst.with_suffix(".part")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "UnityPlayer/2021.3.33f1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status != 200:
                    return (bn, False)
                chunk = r.read()
            if tmp.exists():
                tmp.unlink()
            tmp.write_bytes(chunk)
            tmp.rename(dst)
            return (bn, True)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return (bn, False)
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_one, bn): bn for bn in bundle_names}
        for ft in as_completed(futs):
            bn, ok = ft.result()
            done += 1
            if ok:
                okc = 1
            else:
                fail += 1; okc = 0
            log(f"下载 {'OK' if ok else '失败'} {done}/{total} {bn}")
            if progress:
                progress(done, total)
    return (done, fail)


def server_bundle_needs() -> list:
    """返回所有 missing 项（src=remote）所需的、尚未在本地的唯一 bundle 名。"""
    need = []
    seen = set()
    for it in ITEMS:
        if not it.get("missing"):
            continue
        for bn in it.get("b", []):
            if bn in seen:
                continue
            seen.add(bn)
            if (GAME_BUNDLE_DIR / bn).is_file() or (REMOTE_DIR / bn).is_file():
                continue
            need.append(bn)
    return need


def _after_download():
    """下载完成后：远程项本地已有可解析来源 -> 从 remote 项转正并入预生成。"""
    global _STEM_TABLE
    changed = False
    for it in ITEMS:
        if not it.get("missing"):
            continue
        if any((GAME_BUNDLE_DIR / bn).is_file() or (REMOTE_DIR / bn).is_file() for bn in it.get("b", [])):
            it["missing"] = 0        # 已有来源，可正常解码/缩略图/导出
            it.pop("src", None)
            changed = True
    if changed:
        _STEM_TABLE = None
        persist_info()
        _persist_server_state()


def _persist_server_state() -> None:
    """把服务器来源项（含转正后）的最新状态写回服务端索引缓存，避免下次启动误标 missing。"""
    try:
        rec = [{"id": it["id"], "p": it["p"], "b": it.get("b", []),
                "c": it.get("c", ""), "missing": it.get("missing", 0),
                "src": it.get("src"), "new": it.get("new", 0),
                "w": it.get("w"), "h": it.get("h")}
               for it in ITEMS
               if it.get("src") == "remote" or it.get("missing") is not None]
        SERVER_INDEX_FILE.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def clean_remote_q() -> int:
    """删除 remote_q/ 中已被游戏本地化的 bundle（同 hash 已存在于 GAME_BUNDLE_DIR），释放空间。
    返回删除数量。只动本工具目录，绝不动游戏目录。"""
    if not REMOTE_DIR.is_dir():
        return 0
    removed = 0
    for f in REMOTE_DIR.glob("*.bundle"):
        if (GAME_BUNDLE_DIR / f.name).is_file():
            try:
                f.unlink()
                removed += 1
            except Exception:
                pass
    if removed:
        log(f"已清理 {removed} 个已被本地化、无需再保留的远程 bundle。")
    return removed


def rebuild_by_id(items) -> None:
    global _BY_ID
    _BY_ID = {it["id"]: it for it in items}


def find_server_catalog() -> Path | None:
    # 游戏每次更新会把最新 Addressables 目录刷进 LocalLow 缓存；取其中最新且 >1MB 的一份。
    # 也支持 hbr_config.json 里写 {"cache_dir": "…"} 覆盖。
    override = _CFG.get("cache_dir")
    cand_dirs = [Path(override)] if override else []
    if not override:
        lowlow = Path.home() / "AppData" / "LocalLow"
        cand_dirs.append(lowlow / "wfs" / "HeavenBurnsRed" / "com.unity.addressables")
    best, bs = None, -1.0
    for d in cand_dirs:
        if not d.is_dir():
            continue
        for f in d.glob("catalog_*.bin"):
            try:
                sz, mt = f.stat().st_size, f.stat().st_mtime
            except Exception:
                continue
            if sz > 1_000_000 and mt > bs:
                best, bs = f, mt
    if best is not None:
        log(f"服务器（最新缓存）目录：{best}")
    return best


def detect_server_missing(local_items) -> list[dict]:
    cat = find_server_catalog()
    if cat is None:
        log("未找到服务器（更新缓存）目录，跳过服务器补全检测。")
        return []
    try:
        server_all = build_index(progress=None, catalog_path=cat)
    except Exception as exc:
        log(f"服务器目录解析失败：{exc}")
        return []
    local_keys = {it["p"] for it in local_items}
    new = []
    for s in server_all:
        if s["p"] not in local_keys:
            # 已下载到 remote_q/（或游戏目录恰有）则可解析：保留但不标占位
            have = any((GAME_BUNDLE_DIR / bn).is_file() or (REMOTE_DIR / bn).is_file()
                       for bn in s.get("b", []))
            s["missing"] = 0 if have else 1
            s["src"] = "remote"
            s["new"] = 1
            s["w"], s["h"] = None, None
            new.append(s)
    if new:
        log(f"服务器缺失新图 {len(new)} 张（点“服务器下载”可从官方CDN拉补充全）。")
    return new


def merge_server_missing(local_items, force=False):
    # 优先读缓存（启动秒开）；force=True 时强制重扫服务器目录（手动"重新检测"）
    if not force and SERVER_INDEX_FILE.exists():
        try:
            stored = json.loads(SERVER_INDEX_FILE.read_text(encoding="utf-8"))
            local_keys = {it["p"] for it in local_items}
            kept = []
            for r in stored:
                if r["p"] in local_keys:
                    continue     # 已进入本地索引（如重扫后游戏目录已含该bundle）
                have = any((GAME_BUNDLE_DIR / bn).is_file() or (REMOTE_DIR / bn).is_file()
                           for bn in r.get("b", []))
                r["missing"] = 0 if have else 1
                r["id"] = len(local_items) + len(kept)   # 与合并后下标对齐，保证 id==index
                kept.append(r)
            if kept:
                log(f"服务器补全：从缓存列出 {len(kept)} 张缺失新图（可点“重新检测”刷新）。")
                return local_items + kept
        except Exception:
            pass
    remote = detect_server_missing(local_items)
    if not remote:
        return local_items
    stored = []
    for r in remote:
        r["id"] = len(local_items) + len(stored)   # 与合并后下标对齐，保证 id==index
        stored.append(r)
    try:
        SERVER_INDEX_FILE.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return local_items + stored


def _default_selected_ids() -> list[int]:
    return [it["id"] for it in ITEMS
            if it["c"].startswith(("剧情CG/", "角色卡牌")) and not it.get("missing")]


def load_selection():
    global SELECTED
    if SELECTION_FILE.exists():
        try:
            SELECTED = set(json.loads(SELECTION_FILE.read_text(encoding="utf-8")))
            log(f"已加载勾选状态（{len(SELECTED)} 项）。")
            return
        except Exception as exc:
            log(f"勾选缓存损坏：{exc}")
    SELECTED = set(_default_selected_ids())
    save_selection()
    log(f"已按默认预选（剧情CG + 角色卡牌）勾选 {len(SELECTED)} 项。")


def save_selection():
    SELECTION_FILE.write_text(json.dumps(sorted(SELECTED)), encoding="utf-8")


_last_info_save = 0.0   # persist_info 节流


def persist_info():
    global _last_info_save
    now = time.time()
    if now - _last_info_save < 2.0:      # 节流：最多每 2 秒写一次全量，避免每张缩略图都重建+写盘
        return
    _last_info_save = now
    info = {str(it["id"]): {"w": it["w"], "h": it["h"]}
            for it in ITEMS if it["w"]}
    try:
        INFO_FILE.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 纹理解码（bundle LRU 缓存 + 固定工人池节流）
# ----------------------------------------------------------------------------
def get_env(bundle_name):
    with _env_lock:
        env = _env_cache.get(bundle_name)
        if env is not None:
            _env_cache.move_to_end(bundle_name)
            return env
    path = GAME_BUNDLE_DIR / bundle_name
    rpath = REMOTE_DIR / bundle_name
    if path.is_file():
        use = path
    elif rpath.is_file():
        use = rpath
    else:
        return None
    if _UnityPy is None:
        return None
    loaded = _UnityPy.load(str(use))
    evict = None
    with _env_lock:
        _env_cache[bundle_name] = loaded
        _env_cache.move_to_end(bundle_name)
        while len(_env_cache) > BUNDLE_LRU_CAP:
            _, evict = _env_cache.popitem(last=False)
    if evict is not None:
        del evict
        gc.collect()          # 仅在真正淘汰时回收，避免每次加载都触发
    return loaded


_STEM_TABLE = None       # 规范化文件名 -> item 列表，供跨项同内容解析


def _canon_stem(p: str) -> str:
    """规范化文件主名：去掉 Thumb_ 前缀、扩展名、并转小写，用于匹配内容相同的归档条目。"""
    name = p.split("/")[-1]
    if name.lower().startswith("thumb_"):
        name = name[6:]
    return name.rsplit(".", 1)[0].lower()


def _stem_table() -> dict:
    global _STEM_TABLE
    if _STEM_TABLE is None:
        t = {}
        for it in ITEMS:
            t.setdefault(_canon_stem(it["p"]), []).append(it)
        _STEM_TABLE = t
    return _STEM_TABLE


def _find_texture(item):
    for bn in item["b"]:
        env = get_env(bn)
        if env is None:
            continue
        c = env.container
        obj = c[item["p"]] if item["p"] in c else None
        if obj is not None and obj.type.name in ("Texture2D", "Sprite"):
            return obj
    # 兜底：按规范化文件名跨项查找同内容的可解析条目
    # （游戏更新后部分归档 bundle 被删除/改名，原路径失效，但其 Gallery 镜像仍可解析）
    for alt in _stem_table().get(_canon_stem(item["p"]), []):
        if alt["p"] == item["p"]:
            continue
        for bn in alt["b"]:
            env = get_env(bn)
            if env is None:
                continue
            c = env.container
            if alt["p"] in c:
                obj = c[alt["p"]]
                if obj.type.name in ("Texture2D", "Sprite"):
                    return obj
    return None


def _decode_object(obj, item, w, size_dir) -> None:
    """把已解出的纹理对象解码为缩略图并落盘（供单个详情与分组预生成共用）。"""
    ok = False
    if obj is None:
        with _thumb_lock:
            _thumb_status[item["id"]] = "fail"
    else:
        try:
            tex = obj.read()
            img = tex.image
            if img is None:
                with _thumb_lock:
                    _thumb_status[item["id"]] = "fail"
            else:
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
                item["w"], item["h"] = img.size
                img.thumbnail((w, w), _RESAMPLE)
                size_dir.mkdir(parents=True, exist_ok=True)
                tmp = size_dir / (str(item["id"]) + f".{os.getpid()}.tmp")
                img.save(tmp, "PNG")
                os.replace(tmp, size_dir / (str(item["id"]) + ".png"))
                ok = True
                with _thumb_lock:
                    _thumb_status[item["id"]] = "ok"
        except Exception as exc:
            with _thumb_lock:
                _thumb_status[item["id"]] = "fail"
            log(f"缩略图失败 {item['p']}: {exc}")
    with _thumb_lock:
        _thumb_stat["done" if ok else "fail"] += 1


def _decode_thumb(item, w, size_dir) -> None:
    _decode_object(_find_texture(item), item, w, size_dir)


def pregen_all_thumbs():
    """后台一次性自动生成全部缩略图（按 bundle 分组，每个 bundle 只打开一次，桶内全部解码）。"""
    from collections import defaultdict
    groups = defaultdict(list)
    pending = []
    for it in ITEMS:
        if it.get("missing") and not any((REMOTE_DIR / bn).is_file() for bn in it.get("b", [])):
            continue                  # 服务器缺失项且 bundle 未下载，跳过预生成
        # 用第一个在磁盘上存在的 bundle 分组；都没有则留到兜底慢速通道
        for bn in it["b"]:
            if bn and (GAME_BUNDLE_DIR / bn).is_file():
                groups[bn].append(it)
                break
        else:
            pending.append(it)

    # 多进程并行：UnityPy 解码受 GIL 限制，线程并发无效；多进程直接利用多核
    size_dir = THUMB_DIR / str(THUMB_DEFAULT_W)
    size_dir.mkdir(parents=True, exist_ok=True)
    tasks = [(bn, its) for bn, its in groups.items()]
    for i in range(0, len(pending), 128):
        tasks.append((None, pending[i:i + 128]))
    # 预占 inflight：预生成期间 /thumb 不重复解码，随结果流陆续释放
    _item_by_id = {it["id"]: it for it in ITEMS}
    todo_ids = [it["id"] for _, its in tasks for it in its]
    with _thumb_lock:
        for iid in todo_ids:
            _thumb_inflight.add(iid)
            _thumb_status.setdefault(iid, "pending")
    nproc = max(2, min(8, (os.cpu_count() or 4) - 1))
    log(f"开始后台预生成缩略图：{len(tasks)} 个任务，{len(todo_ids)} 张，{nproc} 进程")
    global _pregen_phase
    done = fail = 0
    if tasks:
        pool = _MP_POOL
        if pool is None:
            _pregen_phase = "skip"
            log("多进程池不可用，缩略图改为按需生成（滚动到才解码）。")
            return
        _pregen_phase = "running"
        for res in pool.imap_unordered(_mp_thumb_task, tasks, chunksize=1):
            for (iid, ok, w, h) in res:
                if ok:
                    done += 1
                    if w or h:
                        it = _item_by_id.get(iid)
                        if it is not None:
                            it["w"], it["h"] = w, h
                else:
                    fail += 1
                with _thumb_lock:
                    _thumb_inflight.discard(iid)
                    _thumb_status[iid] = "ok" if ok else "fail"
                    _thumb_stat["done" if ok else "fail"] += 1
    # 极端情况兜底：确保不残留占用
    with _thumb_lock:
        for iid in todo_ids:
            _thumb_inflight.discard(iid)
    persist_info()
    _pregen_phase = "done"
    log(f"缩略图后台预生成完成：成功 {done}，失败 {fail}。")


_MP_THUMB_DIR = None          # 子进程缩略图输出目录
_MP_W: int = THUMB_DEFAULT_W  # 子进程缩略图边长
_MP_POOL = None               # 全局多进程池（必须由 main 在主线程创建）
_pregen_phase = "idle"        # idle/init/running/done/skip，供前端展示预生成状态


def _init_mp_pool() -> None:
    """在主线程创建多进程池（Windows spawn 禁止在非主线程建池）。失败则回退为按需生成。"""
    global _MP_POOL
    if _MP_POOL is not None:
        return
    global _pregen_phase
    _pregen_phase = "init"
    size_dir = THUMB_DIR / str(THUMB_DEFAULT_W)
    size_dir.mkdir(parents=True, exist_ok=True)
    nproc = max(2, min(8, (os.cpu_count() or 4) - 1))
    from multiprocessing import Pool
    try:
        _MP_POOL = Pool(processes=nproc, initializer=_mp_init_all,
                        initargs=(ITEMS, str(size_dir), THUMB_DEFAULT_W))
        log(f"多进程缩略图池已就绪（{nproc} 进程）。")
    except Exception as exc:
        _MP_POOL = None
        _pregen_phase = "skip"
        log(f"多进程池创建失败，缩略图改为按需生成：{exc}")


def _mp_init_all(global_items, thumb_dir, w) -> None:
    """子进程初始化：注入全局 items / stem / bundle 缓存（spawn 下可 pickle 的顶层函数）。"""
    global ITEMS, _STEM_TABLE, _env_cache, _MP_THUMB_DIR, _MP_W
    ITEMS = global_items
    _STEM_TABLE = None
    _env_cache.clear()
    _MP_THUMB_DIR = Path(thumb_dir)
    _MP_W = w


def _mp_thumb_task(task):
    """子进程内处理一个 bundle 的所有条目（或一小段兜底条目）；复用现有解码函数。"""
    bn, items = task
    c = None
    if bn is not None:
        try:
            env = _UnityPy.load(str(GAME_BUNDLE_DIR / bn))
        except Exception:
            env = None
        c = env.container if env is not None else None
    out = []
    for it in items:
        dst = _MP_THUMB_DIR / (str(it["id"]) + ".png")
        if dst.is_file():
            out.append((it["id"], True, it.get("w"), it.get("h")))
            continue
        obj = None
        if c is not None and it["p"] in c and c[it["p"]].type.name in ("Texture2D", "Sprite"):
            obj = c[it["p"]]
        if obj is None:
            try:
                obj = _find_texture(it)      # 跨项/其它 bundle 兜底
            except Exception:
                obj = None
        try:
            _decode_object(obj, it, _MP_W, _MP_THUMB_DIR)
            ok = dst.is_file()
        except Exception:
            ok = False
        out.append((it["id"], ok, it.get("w"), it.get("h")))
    return out


def ensure_thumb(item, w) -> Path | None:
    """返回缩略图路径；未生成则提交后台任务（单飞 + 固定池节流）。"""
    size_dir = THUMB_DIR / str(w)
    dst = size_dir / (str(item["id"]) + ".png")
    if dst.is_file():
        return dst
    with _thumb_lock:
        if item["id"] in _thumb_inflight:
            return None
        _thumb_inflight.add(item["id"])
        _thumb_status[item["id"]] = "pending"

    def _run():
        try:
            _decode_thumb(item, w, size_dir)
        finally:
            with _thumb_lock:
                _thumb_inflight.discard(item["id"])
            persist_info()

    _thumb_pool.submit(_run)
    return None


# ----------------------------------------------------------------------------
# 导出
# ----------------------------------------------------------------------------
def _export_one(item, out_root):
    cat = safe_component(item["c"])
    rel_dir = out_root / cat
    dst = rel_dir / safe_component(basename_of(item["p"]))
    if dst.suffix.lower() not in IMAGE_EXTS:
        dst = dst.with_suffix(".png")
    if dst.exists():
        return "skipped"
    obj = _find_texture(item)
    if obj is None:
        return "fail"
    try:
        img = obj.read().image
        if img is None:
            return "fail"
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        rel_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        cand = dst
        while cand.exists():
            n += 1
            cand = dst.with_name(f"{dst.stem}__{n}{dst.suffix}")
        tmp = cand.with_suffix(cand.suffix + ".tmp")
        img.save(tmp, "PNG")
        os.replace(tmp, cand)
        return "ok"
    except Exception as exc:
        log(f"导出失败 {item['p']}: {exc}")
        return "fail"


def run_export(ids, out_root):
    if EXP["running"]:
        return
    with _state_lock:
        EXP.update(running=True, total=0, done=0, ok=0, skipped=0,
                   failed=0, phase="准备中", log=[], finished=False)
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    loaded = {str(item["id"]): item for item in ITEMS}
    # 去重（同 id 只导一次），已存在的目标文件由 _export_one 记为跳过
    work = []
    seen = set()
    for i in ids:
        it = loaded.get(str(i))
        if not it or i in seen or it.get("missing"):
            continue
        seen.add(i)
        work.append(it)
    with _state_lock:
        EXP["total"] = len(work)
    log(f"导出开始：{len(work)} 个资产 → {out_root}")
    ok = skips = fails = 0
    done = 0
    ck_lines = []
    with ThreadPoolExecutor(max_workers=EXPORT_WORKERS) as ex:
        futs = {ex.submit(_export_one, it, out_root): it for it in work}
        for fut in as_completed(futs):
            res = fut.result()
            if res == "ok":
                ok += 1
                it = futs[fut]
                rel = out_root / safe_component(it["c"]) / safe_component(basename_of(it["p"]))
                if rel.suffix.lower() not in IMAGE_EXTS:
                    rel = rel.with_suffix(".png")
                ck_lines.append(f"{it['id']}\t{rel}")
            elif res == "skipped":
                skips += 1
            else:
                fails += 1
            done += 1
            with _state_lock:
                EXP.update(done=done, ok=ok, skipped=skips, failed=fails,
                           phase=f"处理中 {done}/{EXP['total']}")
    if ck_lines:
        with CHECKPOINT_FILE.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(ck_lines) + "\n")
    with _state_lock:
        EXP.update(running=False, finished=True, phase="完成",
                   ok=ok, skipped=skips, failed=fails)
    log(f"导出完成：成功 {ok}，跳过 {skips}，失败 {fails}。")


# ----------------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------------
def items_payload():
    return [{"id": it["id"], "p": it["p"], "c": it["c"], "s": it["s"],
             "w": it["w"], "h": it["h"], "new": it.get("new", 0),
             "missing": it.get("missing", 0)} for it in ITEMS]


def cat_counts():
    counts = OrderedDict()
    for it in ITEMS:
        counts[it["c"]] = counts.get(it["c"], 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(counts.items())]


class Handler(BaseHTTPRequestHandler):
    def _send(self, data, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", ctype)
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path
        if p == "/":
            self._send(PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif p == "/index":
            self._send(json.dumps({"items": items_payload(),
                                   "cats": cat_counts(),
                                   "missing": sum(i.get("missing", 0) for i in ITEMS)}).encode("utf-8"))
        elif p == "/selection":
            self._send(json.dumps({"ids": sorted(SELECTED),
                                   "out": str(OUTPUT_ROOT)}).encode("utf-8"))
        elif p.startswith("/thumb?"):
            q = parse_qs(urlparse(p).query)
            try:
                iid = int(q.get("id", ["-1"])[0])
                w = int(q.get("w", [str(THUMB_DEFAULT_W)])[0])
                item = _BY_ID.get(iid)
                if item is None or item.get("missing"):
                    self.send_error(404)
                    return
                dst = ensure_thumb(item, w)
                if dst and dst.is_file():
                    self._send(dst.read_bytes(), "image/png")
                    return
                with _thumb_lock:
                    status = _thumb_status.get(iid, "pending")
                if status == "fail":
                    self.send_error(404)   # 永远失败，前端停止重试
                else:
                    # 仍在生成：返回 202 让前端稍后重试，避免黑屏闪烁
                    body = b"pending"
                    self.send_response(202)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Retry-After", "1")
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except Exception:
                        pass
            except Exception:
                self.send_error(400)
        elif p.startswith("/full?"):
            # 原分辨率图像（供点击放大查看）：仅内存解码后直接返回，绝不落盘
            q = parse_qs(urlparse(p).query)
            try:
                iid = int(q.get("id", ["-1"])[0])
                item = _BY_ID.get(iid)
                if item is None or item.get("missing"):
                    self.send_error(404)
                    return
                with _full_cache_lock:
                    data = _full_cache.get(iid)
                if data is None:
                    obj = _find_texture(item)
                    if obj is None:
                        self.send_error(404)
                        return
                    img = obj.read().image
                    if img is None:
                        self.send_error(404)
                        return
                    if img.mode not in ("RGB", "RGBA"):
                        img = img.convert("RGBA")
                    buf = io.BytesIO()
                    img.save(buf, "PNG")
                    data = buf.getvalue()
                    with _full_cache_lock:
                        _full_cache[iid] = data
                        _full_cache.move_to_end(iid)
                        while len(_full_cache) > _FULL_CACHE_MAX:
                            _full_cache.popitem(last=False)
                self._send(data, "image/png")
            except Exception:
                self.send_error(404)

        elif p == "/thumbstate":
            with _thumb_lock:
                self._send(json.dumps({
                    "done": _thumb_stat["done"], "fail": _thumb_stat["fail"],
                    "inflight": len(_thumb_inflight),
                    "total": len(ITEMS),
                    "phase": _pregen_phase,
                }).encode("utf-8"))
        elif p == "/progress":
            with _state_lock:
                self._send(json.dumps(EXP).encode("utf-8"))
        else:
            self.send_error(404)

    def do_POST(self):
        p = self.path
        if p == "/select":
            body = self._read_body()
            act = body.get("action")
            with _state_lock:
                if act == "set":
                    if body.get("checked"):
                        SELECTED.add(int(body["id"]))
                    else:
                        SELECTED.discard(int(body["id"]))
                elif act == "addmany":
                    SELECTED.update(int(x) for x in body.get("ids", []))
                elif act == "clearmany":
                    for x in body.get("ids", []):
                        SELECTED.discard(int(x))
                elif act == "clear":
                    SELECTED.clear()
                n = len(SELECTED)
            save_selection()
            self._send(json.dumps({"selected": n}).encode("utf-8"))
        elif p == "/extract":
            global OUTPUT_ROOT
            body = self._read_body()
            ids = [int(x) for x in body.get("ids", [])]
            out = body.get("out") or str(OUTPUT_ROOT)
            OUTPUT_ROOT = Path(out)
            threading.Thread(target=run_export, args=(ids, OUTPUT_ROOT), daemon=True).start()
            self._send(b'{"ok":true}')
        elif p == "/openout":
            out = self._read_body().get("out") or str(OUTPUT_ROOT)
            po = Path(out)
            po.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["explorer", str(po)])
            self._send(b'{"ok":true}')
        elif p == "/servdl":
            # 从服务器下载缺失项所需的 bundle 到 remote_q/，完成后转正并入缩略图
            def _dl():
                global ITEMS, _STEM_TABLE
                try:
                    find_server_version()
                    need = server_bundle_needs()
                    if not need:
                        log("没有需要下载的服务器 bundle。")
                        _after_download()
                        return
                    log(f"服务器下载开始：共 {len(need)} 个 bundle（约 {len(need)} 文件）。")
                    ok, fail = download_bundles(need)
                    _after_download()
                    rebuild_by_id(ITEMS)
                    log(f"服务器下载完成：成功 {ok}，失败 {fail}。")
                except Exception as exc:
                    log(f"服务器下载异常：{exc}")
            threading.Thread(target=_dl, daemon=True).start()
            self._send(b'{"ok":true}')
        elif p == "/servscan":
            # 重新检测服务器上新增、本机尚未下载的图片（仅列出，不下载/不改游戏文件）
            def _scan():
                global ITEMS, _BY_ID, _STEM_TABLE
                try:
                    with _state_lock:
                        merged = merge_server_missing(ITEMS, force=True)
                        ITEMS = merged
                    _STEM_TABLE = None
                    rebuild_by_id(ITEMS)
                    log(f"服务器补全：已列出 {sum(i.get('missing', 0) for i in ITEMS)} 张缺失新图。")
                except Exception as exc:
                    log(f"服务器补全检测失败：{exc}")
            threading.Thread(target=_scan, daemon=True).start()
            self._send(b'{"ok":true}')
        elif p == "/rescan":
            # 重新扫描游戏图集并重建索引（保留原 id，已生成缩略图/勾选不失效）
            def _rebuild():
                global ITEMS, _STEM_TABLE
                import traceback
                try:
                    log("重新扫描：开始重建本地索引...")
                    new_items = load_or_build_index(force=True, progress=None)
                    log("重新扫描：本地索引 " + str(len(new_items)) + " 张，开始合并服务器项...")
                    merged = merge_server_missing(new_items, force=True)
                    with _state_lock:
                        ITEMS = merged
                        _STEM_TABLE = None
                    rebuild_by_id(ITEMS)
                    clean_remote_q()
                    log("重新扫描完成：共 " + str(len(ITEMS)) + " 张，服务器缺失 " + str(sum(1 for i in ITEMS if i.get("missing"))) + " 张。")
                except Exception as exc:
                    log("重新扫描失败：" + str(exc))
                    print(traceback.format_exc(), flush=True)
            threading.Thread(target=_rebuild, daemon=True).start()
            self._send(b'{"ok":true}')
        else:
            self.send_error(404)


# ----------------------------------------------------------------------------
# 前端页面
# ----------------------------------------------------------------------------
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HBR 壁纸图库</title>
<style>
  :root{--bg:#14161a;--panel:#1d2128;--line:#2b3038;--fg:#e6e8ec;--dim:#9aa1ac;
        --acc:#4f8cff;--ok:#2fb98b;--h:#21262f;}
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;font-family:"Segoe UI","Microsoft YaHei",system-ui;background:var(--bg);
       color:var(--fg);overflow:hidden}
  #app{display:flex;height:100vh}
  aside{width:240px;min-width:240px;background:var(--panel);border-right:1px solid var(--line);
        display:flex;flex-direction:column}
  #cats{flex:1;overflow:auto;padding:8px}
  .cat{display:flex;justify-content:space-between;padding:7px 9px;border-radius:6px;
       cursor:pointer;margin-bottom:2px}
  .cat:hover{background:var(--h)}
  .cat.active{background:var(--acc);color:#fff}
  .cat .n{color:var(--dim);font-size:12px}
  .cat.active .n{color:#dfe9ff}
  main{flex:1;display:flex;flex-direction:column;min-width:0}
  #bar{display:flex;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line);
       background:var(--panel);flex-wrap:wrap}
  #bar input[type=text]{flex:0 1 240px;background:var(--bg);border:1px solid var(--line);
       color:var(--fg);border-radius:6px;padding:7px 10px;outline:none}
  #bar select,#bar button{background:var(--h);border:1px solid var(--line);color:var(--fg);
       border-radius:6px;padding:7px 11px;cursor:pointer}
  #bar button.acc{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
  #selinfo{color:var(--dim);margin-left:auto;white-space:nowrap}
  #wrap{flex:1;overflow:auto;position:relative;background:var(--bg)}
  #grid{position:relative}
  .cell{position:absolute;width:186px;padding:8px;border-radius:8px}
  .cell:hover{background:var(--h)}
  .cell .box{position:relative;height:130px;background:#000;border:1px solid var(--line);
       border-radius:6px;overflow:hidden}
  .cell img{width:100%;height:100%;object-fit:contain;display:block}
  .cell .load{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
       color:var(--dim);font-size:24px;animation:ps 1s ease-in-out infinite}
  @keyframes ps{50%{opacity:.35}}
  .cell .chk{position:absolute;top:4px;left:4px;transform:scale(1.4);accent-color:var(--acc)}
  .cell .dim{position:absolute;right:4px;bottom:4px;background:rgba(0,0,0,.7);
       color:#fff;font-size:10px;padding:1px 5px;border-radius:4px}
  .cell .nw{position:absolute;left:4px;bottom:4px;background:var(--acc);
       color:#fff;font-size:9px;padding:1px 5px;border-radius:4px;font-weight:700}
  .cell .ms{position:absolute;left:4px;bottom:4px;background:#a24b4b;color:#fff;
       font-size:9px;padding:1px 5px;border-radius:4px;font-weight:700}
  .mis{display:flex;flex-direction:column;align-items:center;justify-content:center;
       height:130px;color:#7b8392;background:#181c22;font-size:11px;line-height:1.6;text-align:center}
  #serbar{display:none;gap:10px;align-items:center;padding:8px 14px;background:#2a2410;
       border-bottom:1px solid #4a3a10;color:#ffd479;font-size:13px;flex-wrap:wrap}
  #serbar button{background:#5a4a00;border:1px solid #7a6400;color:#ffd479;
       border-radius:6px;padding:4px 11px;cursor:pointer;margin-left:auto}
  #helpbox{position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:120;display:none;
       align-items:center;justify-content:center;padding:20px}
  #helpbox .card{background:#1b2029;border:1px solid #313a48;border-radius:12px;max-width:640px;
       width:100%;max-height:82vh;overflow:auto;padding:22px 26px;color:#dfe6f2;box-shadow:0 10px 50px #000}
  #helpbox h3{margin:0 0 14px;color:#fff;font-size:17px;display:flex;justify-content:space-between;align-items:center}
  #helpbox .step{display:flex;gap:12px;margin:12px 0;line-height:1.7;font-size:13px}
  #helpbox .num{flex:0 0 26px;height:26px;border-radius:50%;background:#3a6ea5;color:#fff;
       display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px}
  #helpbox .k{color:#8fc7ff;font-weight:600}
  #helpbox .sub{color:#8b94a3;font-size:12px;margin-top:10px;line-height:1.7}
  #helpbox .x{background:#2a3340;border:1px solid #3a4657;color:#cfd8e6;border-radius:6px;
       cursor:pointer;padding:4px 12px;font-size:13px}
  #viewer{position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:100;overflow:hidden}
  #viewer.hide{display:none}
  #vimg{position:absolute;left:50%;top:50%;max-width:none;max-height:none;
       box-shadow:0 0 60px #000;cursor:grab}
  #vclose{position:fixed;top:14px;right:18px;z-index:110;background:rgba(0,0,0,.6);
       color:#fff;border:1px solid #555;border-radius:50%;width:38px;height:38px;font-size:19px;
       cursor:pointer;line-height:1}
  #vhint{position:fixed;left:50%;transform:translateX(-50%);bottom:22px;z-index:110;
       color:#dfe9ff;font-size:12px;background:rgba(0,0,0,.55);padding:6px 14px;border-radius:20px}
  .cell .nm{font-size:11px;color:var(--dim);margin-top:4px;white-space:nowrap;
       overflow:hidden;text-overflow:ellipsis}
  #hud{position:fixed;right:14px;bottom:14px;width:340px;max-height:36vh;background:var(--panel);
       border:1px solid var(--line);border-radius:10px;padding:10px;display:flex;flex-direction:column;
       box-shadow:0 6px 30px #000;z-index:50}
  #hud.hide{display:none}
  #hud h3{margin:0 0 8px;font-size:14px}
  #prog{border-radius:6px;height:12px;background:var(--bg);overflow:hidden}
  #prog>div{height:100%;background:var(--ok);width:0;transition:width .3s}
  #hlog{margin-top:8px;font:11px/1.5 Consolas,monospace;color:var(--dim);overflow:auto;flex:1;user-select:text}
  #phase{font-size:12px;margin-top:6px}
</style>
</head>
<body>
<div id="app">
<aside>
  <div style="padding:12px 10px;border-bottom:1px solid var(--line);font-weight:700">HBR 壁纸图库</div>
  <div style="padding:6px 10px;color:var(--dim);font-size:12px">分类（点击筛选）</div>
  <div id="cats"></div>
</aside>
<main>
  <div id="bar">
    <input id="q" type="text" placeholder="搜索文件名…">
    <label style="font-size:13px;display:flex;align-items:center;gap:4px">
      <input id="wp" type="checkbox"> 仅壁纸尺寸</label>
    <select id="sort">
      <option value="c">按名称</option>
      <option value="s">按体积从大到小</option>
      <option value="w">按分辨率从大到小</option>
    </select>
    <label style="font-size:13px;display:flex;align-items:center;gap:4px">
      <input id="nw" type="checkbox"> 仅本次新更新</label>
    <button id="selwrap">全选筛选结果</button>
    <button id="clearsi">清空已选</button>
    <button id="chdir">改输出目录…</button>
    <button id="openout">打开已输出</button>
    <button id="helpbtn" title="查看更新后的三步操作说明">使用说明</button>
    <button id="resc" title="游戏更新后重建索引">重新扫描</button>
    <button id="servs" title="检测服务器上新增、本机尚未下载的图片并列出">检测服务器新图</button>
    <button id="servdl" title="从官方CDN下载缺失bundle到本机，生成缩略图/可导出">服务器下载</button>
    <button id="go" class="acc">导出选中 (<span id="seln">0</span>)</button>
    <span id="selinfo"></span>
    <span id="thprog" style="display:none;color:#ffd479;font-size:12px;font-weight:600"></span>
  </div>
  <div id="serbar">服务器有 <b id="sern" style="color:#fff">0</b> 张新图
    （游戏更新后：先“重新扫描”→“检测服务器新图”→“服务器下载”；仅写入本工具目录）
    <button id="servs2">重新检测</button></div>
  <div id="helpbox"><div class="card">
    <h3>游戏更新后，如何让新图出现在这里？<button class="x" id="helpclose">关闭</button></h3>
    <div class="step"><div class="num">1</div><div><span class="k">重新扫描</span><br>
      重建本地索引：重读游戏目录里的资源。用于游戏已经把新图下载到本地的情况，让工具认回这些图。已生成的缩略图与勾选会保留。</div></div>
    <div class="step"><div class="num">2</div><div><span class="k">检测服务器新图</span><br>
      读取游戏缓存目录，找出“服务器上有、本地还没有”的新图并列出（只检测，不下载）。</div></div>
    <div class="step"><div class="num">3</div><div><span class="k">服务器下载</span><br>
      把上一步发现的缺失 bundle 从官方 CDN 拉到本工具的 remote_q 文件夹，自动生成缩略图、可放大、可导出。已下载过的不会重复下载。</div></div>
    <div class="sub">两个按钮的区别：<b>重新扫描</b> 管“游戏已经下载到本地”的图；<b>检测服务器 + 服务器下载</b> 管“服务器上有、本地还没有”的图。更新后按 1→2→3 顺序操作即可。<br>
    所有下载与产物只写入本工具文件夹，绝不修改游戏目录。</div>
  </div></div>
  <div id="wrap"><div id="grid"></div></div>
</main>
</div>
<div id="viewer" class="hide">
  <img id="vimg" alt="">
  <button id="vclose" title="关闭 (ESC)">×</button>
  <div id="vhint">滚轮缩放 · 拖拽平移 · ESC 关闭</div>
</div>
<div id="hud" class="hide">
  <h3>导出进度 <button id="hudclose" title="关闭">×</button></h3>
  <div id="prog"><div id="progf"></div></div>
  <div id="phase"></div>
  <div id="hlog"></div>
</div>
<script>
const CELL_W=204,CELL_H=168,VGAP=4;
let items=[],cats=[],selected=new Set(),out="";
let activeCat="全部",query="",onlyWp=false,onlyNew=false,sortKey="c";
const bCache={};              // 缩略图 blob URL 缓存（进程内只请求一次）
const imgEls=new Map();       // 已加载的 <img> 元素缓存（按插入序淘汰，滚动复用消除黑闪）
const viewer=document.getElementById('viewer'),vimg=document.getElementById('vimg');
let vscale=1,vx=0,vy=0,vdrag=null,openId=-1;
function resetV(){vscale=1;vx=0;vy=0;vimg.style.transform='translate(calc(-50% + 0px),calc(-50% + 0px)) scale(1)';}
function applyV(){vimg.style.transform=`translate(calc(-50% + ${vx}px),calc(-50% + ${vy}px)) scale(${vscale})`;}
function openView(id){viewer.classList.remove('hide');openId=id;resetV();
  // 先立刻显示缓存/缩略图，避免黑屏；随后异步换原图
  const th=bCache[id];vimg.src=th?th:('/thumb?id='+id+'&w=256');
  const f=new Image();f.onload=()=>{if(openId===id)vimg.src=f.src;};f.onerror=()=>{};f.src='/full?id='+id;
}
function evictImg(){while(imgEls.size>3000){const k=imgEls.keys().next().value;imgEls.delete(k);}}
function makeImg(id,box){
  const im=imgEls.get(id);
  if(im){im.style.opacity=1;box.prepend(im);const lx=box.querySelector('.load');if(lx)lx.style.display='none';return;}   // 已加载复用，秒出
  const ne=document.createElement('img');ne.alt='';ne.style.opacity=0;
  imgEls.set(id,ne);evictImg();box.prepend(ne);
  const ld=box.querySelector('.load');
  const apply=()=>{ne.style.opacity=1;if(ld)ld.style.display='none';};
  const tryTh=()=>{const u=bCache[id];if(u){ne.src=u;apply();return;}
    fetch('/thumb?id='+id+'&w=256').then(r=>{
      if(r.status===200)return r.blob().then(b=>{bCache[id]=URL.createObjectURL(b);ne.src=bCache[id];apply();});
      if(r.status===202){setTimeout(tryTh,900);}
      else if(ld)ld.textContent='×';
    }).catch(()=>setTimeout(tryTh,1500));};
  tryTh();
}
document.getElementById('vclose').onclick=()=>viewer.classList.add('hide');
window.addEventListener('keydown',e=>{if(e.key==='Escape')viewer.classList.add('hide');});
viewer.addEventListener('mousedown',e=>{vdrag={x:e.clientX-vx,y:e.clientY-vy};vimg.style.cursor='grabbing';e.preventDefault();});
window.addEventListener('mousemove',e=>{if(!vdrag)return;vx=e.clientX-vdrag.x;vy=e.clientY-vdrag.y;applyV();});
window.addEventListener('mouseup',()=>{vdrag=null;vimg.style.cursor='grab';});
viewer.addEventListener('wheel',e=>{e.preventDefault();vscale*=e.deltaY<0?1.15:0.87;vscale=Math.min(8,Math.max(.25,vscale));applyV();},{passive:false});
const grid=document.getElementById('grid'),wrap=document.getElementById('wrap');
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtByte=b=>{if(!b)return'';const u=['B','KB','MB','GB'];let i=0;b=+b;while(b>=1024&&i<3){b/=1024;i++;}return b.toFixed(i>1?1:0)+u[i];};
function wpHit(it){if(it.w&&it.h)return it.w>=1920&&it.h>=1080;return (it.s||0)>=524288;}
let _fKey=null,_fVal=null;
function filtered(){
  const k=activeCat+'\x00'+query+'\x00'+onlyWp+'\x00'+onlyNew+'\x00'+sortKey;
  if(_fKey===k)return _fVal;
  let l=items.filter(i=>{
    if(activeCat!=='全部'&&i.c!==activeCat)return false;
    if(query&&!i.p.toLowerCase().includes(query.toLowerCase()))return false;
    if(onlyWp&&!wpHit(i))return false;
    if(onlyNew&&!i.new)return false;
    return true;});
  if(sortKey==='s')l.sort((a,b)=>(b.s||0)-(a.s||0));
  else if(sortKey==='w')l.sort((a,b)=>(b.w||b.s||0)-(a.w||a.s||0));
  else l.sort((a,b)=>a.p<b.p?-1:a.p>b.p?1:0);
  _fKey=k;_fVal=l;return l;
}
function renderCats(){
  const el=document.getElementById('cats');
  const mk=(n,c)=>`<div class="cat ${n===activeCat?'active':''}" data-c="${esc(n)}"><span>${esc(n)}</span><span class="n">${c}</span></div>`;
  el.innerHTML=mk('全部',items.length)+cats.map(c=>mk(c.name,c.count)).join('');
  el.querySelectorAll('.cat').forEach(d=>d.onclick=()=>{activeCat=d.dataset.c;renderCats();layout();});
}
function layout(){
  const cols=Math.max(1,Math.floor(wrap.clientWidth/CELL_W));
  const f=filtered();
  const rows=Math.ceil(f.length/cols);
  grid.style.height=(rows*CELL_H+VGAP)+'px';grid.innerHTML='';
  const top=wrap.scrollTop,bot=top+wrap.clientHeight;
  const first=Math.max(0,Math.floor(top/CELL_H)-1);
  const last=Math.min(rows-1,Math.ceil(bot/CELL_H)+1);
  for(let r=first;r<=last;r++)for(let c=0;c<cols;c++){
    const idx=r*cols+c,it=f[idx];if(!it)continue;
    const cell=document.createElement('div');
    cell.className='cell';cell.style.left=(c*CELL_W+VGAP)+'px';cell.style.top=(r*CELL_H+VGAP)+'px';
    cell.style.width=(CELL_W-2*VGAP)+'px';
    const dim=(it.w&&it.h)?(it.w+'×'+it.h):(it.s?fmtByte(it.s):'…');
    const chk=selected.has(it.id)?'checked':'';
    const nm=`<div class="nm" title="${esc(it.p)}">${esc(it.p.split('/').pop())}</div>`;
    if(it.missing){
      cell.innerHTML=`<div class="box mis"><span class="ms">服务器缺失</span>下载待接通</div>${nm}`;
      grid.appendChild(cell);continue;
    }
    cell.innerHTML=`<div class="box"><span class="load">⋯</span><input type="checkbox" class="chk" ${chk}><span class="dim">${dim}</span>${it.new?'<span class="nw">新</span>':''}</div>${nm}`;
    makeImg(it.id,cell.querySelector('.box'));
    cell.querySelector('.box').addEventListener('click',e=>{
      if(e.target.closest('.chk'))return;        // 不拦截勾选框
      openView(it.id);
    });
    cell.querySelector('.chk').onchange=e=>{
      e.target.checked?selected.add(it.id):selected.delete(it.id);
      fetch('/select',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:'set',id:it.id,checked:e.target.checked})});
      updSel();};
    grid.appendChild(cell);
  }
}
let bus=false;
wrap.addEventListener('scroll',()=>{if(!bus){bus=true;requestAnimationFrame(()=>{layout();bus=false;})}},{passive:true});
window.addEventListener('resize',layout);
function updSel(){document.getElementById('seln').textContent=selected.size;
  document.getElementById('selinfo').textContent=filtered().length+' 显示，已选 '+selected.size+' 张';}
function setSelMany(add){const f=filtered().filter(i=>!i.missing).map(i=>i.id);
  if(add)f.forEach(i=>selected.add(i));else f.forEach(i=>selected.delete(i));
  fetch('/select',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(add?{action:'addmany',ids:f}:{action:'clearmany',ids:f})});
  updSel();layout();}
document.getElementById('q').oninput=e=>{query=e.target.value;layout();};
document.getElementById('wp').onchange=e=>{onlyWp=e.target.checked;layout();};
document.getElementById('nw').onchange=e=>{onlyNew=e.target.checked;layout();};
document.getElementById('sort').onchange=e=>{sortKey=e.target.value;layout();};
document.getElementById('selwrap').onclick=()=>setSelMany(true);
document.getElementById('clearsi').onclick=()=>{selected.clear();
  fetch('/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'clear'})});
  updSel();layout();};
document.getElementById('chdir').onclick=()=>{const d=prompt('输出目录',out);if(d)out=d.trim();};
document.getElementById('openout').onclick=()=>fetch('/openout',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({out})});
const hud=document.getElementById('hud'),progf=document.getElementById('progf');
const phaseEl=document.getElementById('phase'),hlog=document.getElementById('hlog');
document.getElementById('go').onclick=()=>{
  if(!selected.size){alert('尚未勾选任何图片');return;}
  if(!confirm('确认导出选中的 '+selected.size+' 张图片到：\n'+out+' ？'))return;
  fetch('/extract',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ids:[...selected],out})}).then(poll);
};
function poll(){
  hud.classList.remove('hide');
  const t=setInterval(async()=>{
    const s=await(await fetch('/progress')).json();
    const pct=s.total?Math.min(100,((s.ok+s.skipped+s.failed)/s.total)*100):0;
    progf.style.width=pct+'%';
    phaseEl.textContent=s.phase+(s.finished?' — '+s.ok+'成功 / '+s.skipped+'跳过 / '+s.failed+'失败':'');
    hlog.innerHTML=s.log.map(esc).join('<br>');hlog.scrollTop=hlog.scrollHeight;
    if(s.finished&&!s.running){clearInterval(t);setTimeout(()=>hud.classList.add('hide'),2500);}
  },500);
}
document.getElementById('hudclose').onclick=()=>hud.classList.add('hide');
(async()=>{
  const data=await(await fetch('/index')).json();
  const sel=await(await fetch('/selection')).json();
  items=data.items;cats=data.cats;out=sel.out;selected=new Set(sel.ids);
  if(data.missing){document.getElementById('sern').textContent=data.missing;
    document.getElementById('serbar').style.display='flex';}
  updSel();renderCats();layout();
})().catch(e=>alert('加载失败：'+e));
const doServ=()=>{if(!confirm('重新检测服务器上新增、本机尚未下载的图片并列出？（不下载、不修改任何文件）'))return;
  fetch('/servscan',{method:'POST'}).then(()=>{alert('已开始检测，完成后自动刷新…');setTimeout(()=>location.reload(),8000);});};
document.getElementById('servs').onclick=doServ;
document.getElementById('servs2').onclick=doServ;
document.getElementById('helpbtn').onclick=()=>{document.getElementById('helpbox').style.display='flex';};
document.getElementById('helpclose').onclick=()=>{document.getElementById('helpbox').style.display='none';};
document.getElementById('helpbox').addEventListener('click',ev=>{if(ev.target.id==='helpbox')ev.currentTarget.style.display='none';});
document.getElementById('servdl').onclick=e=>{fetch('/servdl',{method:'POST'}).then(()=>alert('已开始从服务器下载缺失bundle；完成后自动转正并生成缩略图，稍后刷新页面查看。'));};
const thprog=document.getElementById('thprog');
document.getElementById('resc').addEventListener('click',()=>{
  if(!confirm('将重新扫描游戏图集并重建索引（扫描完成后本页自动刷新，已生成的缩略图和勾选保留）。继续？'))return;
  fetch('/rescan',{method:'POST'}).then(()=>{alert('已开始重新扫描，完成后自动刷新…');setTimeout(()=>location.reload(),15000);});
});
function pollThumb(){
  fetch('/thumbstate').then(r=>r.json()).then(s=>{
    thprog.style.display='inline';
    if(s.phase==='init'){thprog.style.color='var(--dim)';
      thprog.textContent='正在准备缩略图预生成（约30秒）…';
    }else if(s.phase==='running'||s.inflight>0){thprog.style.color='#ffd479';
      thprog.textContent='缩略图生成中 完成 '+s.done+' · 失败 '+s.fail+' · 处理中 '+s.inflight;
    }else{thprog.style.color='var(--dim)';
      thprog.textContent='缩略图已生成 '+s.done+' · 失败 '+s.fail;
    }
  }).catch(()=>{});setTimeout(pollThumb,1500);
}
pollThumb();

</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
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
    global ITEMS, _thumb_pool
    TOOL_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    if not PIL_OK or not UNITYPY_OK:
        miss = []
        if not PIL_OK:
            miss.append("Pillow")
        if not UNITYPY_OK:
            miss.append("UnityPy")
        _fatal("缺少依赖：" + "、".join(miss) +
               "\n\n请在本文件夹打开命令行后执行：\n    python -m pip install -r requirements.txt")
    if not GAME_BUNDLE_DIR.is_dir() or not CATALOG_FILE.is_file():
        _fatal("未找到游戏资源目录：\n" + str(GAME_BUNDLE_DIR) +
               "\n\n请把游戏装好后重试；若游戏不在默认位置，"
               "请复制 hbr_config.example.json 为 hbr_config.json，"
               "并把 game_dir 改成实际的 StandaloneWindows64 目录。")

    force = "--rescan" in sys.argv
    state = {}
    state_lock = threading.Lock()
    label_holder = {}
    url_holder = {}

    def _index_thread(lbl):
        def pb(done, total):
            msg = f"正在扫描游戏索引…\n{done:,} / {total:,}  （首次运行较慢，请稍候）"
            if lbl:
                lbl.config(text=msg)
        if lbl:
            lbl.config(text="正在读取索引…")
        items = load_or_build_index(force=force, progress=pb)
        with state_lock:
            state["items"] = items
        if lbl:
            lbl.config(text="索引就绪，正在启动本地服务…")

    def _window():
        try:
            from tkinter import ttk
            import tkinter as tk
            root = tk.Tk()
            root.title("HBR 壁纸图库")
            root.geometry("460x260")
            root.configure(bg="#14161a")
            label = tk.Label(root, text="正在准备…", fg="#e6e8ec", bg="#14161a",
                             font=("Microsoft YaHei", 11), justify="left", anchor="nw",
                             wraplength=430)
            label.pack(padx=14, pady=12, fill="both", expand=True)
            status = tk.Label(root, text="", fg="#8fd3ff", bg="#14161a",
                              font=("Microsoft YaHei", 10), justify="left")
            status.pack(padx=14, anchor="w")
            ttk.Button(root, text="退出（关闭工具）", command=sys.exit).pack(pady=10)

            def _upd():
                u = url_holder.get("url")
                status.config(text="浏览器已打开：" + u if u else "正在启动本地服务…")
                root.after(500, _upd)
            root.after(500, _upd)
            label_holder["label"] = label
            threading.Thread(target=_index_thread, args=(label,), daemon=True).start()
            root.mainloop()
        except Exception:
            _index_thread(None)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass

    threading.Thread(target=_window, daemon=True).start()

    while "items" not in state:
        time.sleep(0.2)
    ITEMS = state["items"]
    if not ITEMS:
        raise SystemExit(f"未找到图片资产，请检查目录：{GAME_BUNDLE_DIR}")

    # 服务器补全：检测服务器上有、本地没有的新图并列出（仅检测，不下载）
    try:
        ITEMS = merge_server_missing(ITEMS)
    except Exception as exc:
        log(f"服务器补全检测失败：{exc}")
    rebuild_by_id(ITEMS)
    load_selection()

    _thumb_pool = ThreadPoolExecutor(max_workers=THUMB_WORKERS)
    def _pregen_guarded():
        # 兜底：任何未预期异常都不要静默杀死后台预生成线程，而是记入日志暴露出来
        try:
            pregen_all_thumbs()
        except Exception as exc:
            log(f"缩略图预生成异常终止：{exc}")

    s = socket.socket(AF_INET, SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    url_holder["url"] = url
    log(f"本地图库已启动：{url}")
    webbrowser.open(url)

    # 图库已可浏览；随后在主线程建多进程池（约20-30秒），池就绪后启动后台预生成
    _init_mp_pool()
    threading.Thread(target=_pregen_guarded, daemon=True).start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()