"""
runtime — 所有 handler 共用的行程層級基礎設施。

- logging：console（systemd journal）+ hermes_tool_filter.log
- config.yaml → CONFIG / PORT_MAP / DEFAULT_UPSTREAM / BIND_HOST / BIND_PORT
- crash debug：SIGUSR2 thread dump、每 30 秒 health dump + RSS watchdog
- 記憶體保護（請求入口 RSS 檢查、body 上限）與共用 aiohttp session
"""

import asyncio
import logging
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# ── Logging ───────────────────────────────────────────────
# Production default INFO. Set TOOL_FILTER_LOG_LEVEL=DEBUG for deep tracing.
# DEBUG on a long-lived SSE proxy can retain huge format args and flood journald.
_LOG_LEVEL = getattr(logging, os.environ.get("TOOL_FILTER_LOG_LEVEL", "INFO").upper(), logging.INFO)

# ── 雙重日誌：console + file ──
# 將關鍵錯誤和除錯資訊寫入 .log 文件，不依賴 systemd journal
LOG_FILE = Path(__file__).parent / "hermes_tool_filter.log"

# Console handler (systemd journal)
console_handler = logging.StreamHandler()
console_handler.setLevel(_LOG_LEVEL)
console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

# File handler (persistent log for debugging)
file_handler = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)  # 文件記錄所有 DEBUG 級別
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s"))

# Root logger
logging.basicConfig(
    level=logging.DEBUG,
    handlers=[console_handler, file_handler],
)
logger = logging.getLogger("tool-filter")
logger.setLevel(logging.DEBUG)

# ── Crash Debug: SIGUSR2 thread dump ────────────────────────
# 當 process 卡死時，發送 SIGUSR2 會立刻 dump 所有執行緒堆疊到 log 檔案。
# 用法: kill -USR2 <pid>
# 這是在 D state 時唯一能拿到現場資料的方法（D state 下 Python 回調可能跑不起來）。

def _thread_dump_handler(signum, frame):
    """SIGUSR2 handler: dump all thread stacks to log file."""
    dump_lines = ["=" * 80, "CRASH DUMP triggered by SIGUSR2 at", time.strftime("%Y-%m-%d %H:%M:%S"), "=" * 80]
    
    # 1. Process status
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if any(line.startswith(k) for k in ["Name", "State", "Threads", "VmRSS", "VmSize", "VmPeak", "voluntary", "involuntary"]):
                    dump_lines.append(line.rstrip())
    except Exception as e:
        dump_lines.append(f"[ERROR reading /proc/self/status] {e}")
    
    # 2. Thread stacks
    dump_lines.append("")
    dump_lines.append(f"--- {threading.active_count()} active threads ---")
    frames = sys._current_frames()
    for tid, frame in frames.items():
        t = None
        for t in threading.enumerate():
            if t.ident == tid:
                break
        tname = t.name if t else f"tid={tid}"
        dump_lines.append(f"\n### Thread: {tname} (tid={tid}) ###")
        stack = traceback.format_stack(frame)
        dump_lines.extend(stack)
    
    # 3. asyncio task info
    try:
        loop = asyncio.get_event_loop()
        tasks = asyncio.all_tasks(loop)
        dump_lines.append(f"\n--- asyncio tasks: {len(tasks)} ---")
        for task in list(tasks)[:20]:  # 最多 20 個
            dump_lines.append(f"  Task: {task.get_name() if hasattr(task, 'get_name') else repr(task)}")
            if task.done():
                dump_lines.append(f"    Status: DONE")
            else:
                dump_lines.append(f"    Status: {'CANCELLED' if task.cancelled() else 'PENDING/RUNNING'}")
    except Exception as e:
        dump_lines.append(f"[ERROR reading asyncio tasks] {e}")
    
    dump_text = "\n".join(dump_lines)
    # 直接寫檔案（不經過 logging，因為 D state 下 logging 可能也卡住）
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(dump_text + "\n")
    except Exception:
        pass


# Register SIGUSR2 handler (only in main thread)
try:
    signal.signal(signal.SIGUSR2, _thread_dump_handler)
except (OSError, ValueError):
    pass  # Not main thread or signal unavailable


# ── Crash Debug: Periodic health dump ──────────────────────
# 背景任務：每 30 秒 dump 一次關鍵指標到 log 檔案。
# 包含：RSS、buffer size、active tools、asyncio tasks、thread count。

_health_dump_interval = 30  # seconds
_health_dump_task = None

# ── RSS watchdog 閾值（2026-08-07 新增）──
# stale stream 曾吃到 3.2G RSS + 1G swap；3GB 主動退出，比 systemd
# MemoryMax=4G 硬殺更早，避免 swap thrashing。Restart=always 會重啟。
_RSS_WATCHDOG_BYTES = 3 * 1024 * 1024 * 1024  # 3GB


async def _health_dump_loop():
    """Periodic health dump task — runs in background."""
    global _health_dump_task
    _health_dump_task = asyncio.current_task()
    while True:
        await asyncio.sleep(_health_dump_interval)
        try:
            rss_kb = 0
            with open("/proc/self/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                        break
            
            # ── RSS watchdog：超過上限直接自殺讓 systemd 重啟 ──
            # 歷史教訓（2026-08-07）：stale stream 20 分鐘吃到 3.2G RSS +
            # 1G swap，swap thrashing 會拖垮整台機器（先前 1.4TB 讀取事件）。
            # 與其等 systemd MemoryMax=4G 硬殺，不如在 3G 就主動退出——
            # Restart=always 會拉起來，Open WebUI 重試即可。
            if rss_kb * 1024 >= _RSS_WATCHDOG_BYTES:
                logger.critical(
                    f"[rss-watchdog] RSS={rss_kb}kB >= 3GB limit. "
                    f"Self-terminating to prevent swap thrashing. "
                    f"threads={threading.active_count()} "
                    f"asyncio-tasks={len(asyncio.all_tasks())}"
                )
                # flush log 後退出（exit code 1 → systemd Restart=always 重啟）
                for handler in logger.handlers:
                    try:
                        handler.flush()
                    except Exception:
                        pass
                os._exit(1)
            
            # Count active streams (approximate via open file descriptors to 127.0.0.1)
            task_count = len(asyncio.all_tasks()) if asyncio.get_event_loop().is_running() else 0
            
            logger.info(
                f"[health-dump] RSS={rss_kb}kB threads={threading.active_count()} "
                f"asyncio-tasks={task_count}"
            )
        except Exception as e:
            logger.warning(f"[health-dump] error: {e}")


async def start_health_dump():
    """Start the periodic health dump background task."""
    asyncio.create_task(_health_dump_loop())
    logger.info(f"[health-dump] Started periodic dump every {_health_dump_interval}s")

# ── Configuration ──────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.yaml"
CONFIG: Dict[str, Any] = {}

def _load_config() -> Dict[str, Any]:
    """
    載入配置：config.yaml，再以環境變數（BIND_PORT / BIND_HOST）覆寫。
    """
    cfg: Dict[str, Any] = {}

    # 1. 載入 config.yaml
    if CONFIG_PATH.exists() and HAS_YAML:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                yaml_cfg = yaml.safe_load(f) or {}
            logger.info(f"Loaded config from {CONFIG_PATH}")
            cfg.update(yaml_cfg)
        except Exception as e:
            logger.warning(f"Failed to load config.yaml: {e}")
    elif CONFIG_PATH.exists():
        logger.warning("config.yaml exists but PyYAML is not installed. Install with: pip install pyyaml")

    # 2. .env 環境變數覆蓋
    if os.environ.get("BIND_PORT"):
        cfg["bind_port"] = int(os.environ["BIND_PORT"])
    if os.environ.get("BIND_HOST"):
        cfg["bind_host"] = os.environ["BIND_HOST"]

    return cfg

CONFIG = _load_config()

# ── Upstream table ─────────────────────────────────────────

BIND_HOST = CONFIG.get("bind_host", "0.0.0.0")
BIND_PORT = CONFIG.get("bind_port", 9099)

# Port routing table: path prefix -> upstream base URL
# 優先使用 config.yaml 的 upstreams，fallback 到硬編碼預設值
_DEFAULT_PORT_MAP: Dict[str, str] = {
    "30000": "http://127.0.0.1:30000",
    "30001": "http://127.0.0.1:30001",
    "30002": "http://127.0.0.1:30002",
    "30003": "http://127.0.0.1:30003",
}

# 從 config.yaml 載入 upstreams（如果有的話）
_config_upstreams = CONFIG.get("upstreams", {})
if _config_upstreams:
    PORT_MAP: Dict[str, str] = dict(_DEFAULT_PORT_MAP)
    PORT_MAP.update(_config_upstreams)
    logger.info(f"PORT_MAP loaded from config.yaml: {PORT_MAP}")
else:
    PORT_MAP = _DEFAULT_PORT_MAP

# Default upstream if no port prefix matched
DEFAULT_UPSTREAM = PORT_MAP.get("30000", "http://127.0.0.1:30000")

# ── Shared aiohttp session ────────────────────────────────

_http_session: Optional[aiohttp.ClientSession] = None


# ── Memory Self-Protection ────────────────────────────────
# 歷史教訓：Open WebUI 帶大量 tool 結果的對話歷史請求可以輕鬆吃掉數百 MB
# （body 讀入 → json 解析 → sanitize 多份拷貝 → 重新序列化）。
# MemoryMax=2G 只計 RSS 不計 swap-out，無限 swap thrashing 會讓進程凍結。
# 這裡在請求入口主動檢查 RSS：逼近上限時立刻拒接 + gc，讓 Open WebUI 重試，
# 而不是讓整個進程被 swap 拖死。systemd MemorySwapMax=256M 是最後防線。

_MEM_GUARD_BYTES = 1300 * 1024 * 1024  # 1.3GB，留 700MB 給 MemoryMax=2G 緩衝
MAX_REQUEST_BODY = 128 * 1024 * 1024   # 128MB 請求體上限（防超大歷史）
_mem_guard_last_gc = 0.0


def _rss_bytes() -> int:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


def mem_guard_reject() -> bool:
    """
    記憶體壓力檢查。回傳 True 表示應該拒接請求（503）。
    超過閾值時先 gc.collect() 一次，仍超過才拒接。
    """
    global _mem_guard_last_gc
    if _rss_bytes() < _MEM_GUARD_BYTES:
        return False
    now = time.monotonic()
    if now - _mem_guard_last_gc > 5.0:
        _mem_guard_last_gc = now
        import gc
        collected = gc.collect()
        logger.warning(f"[mem-guard] RSS exceeded {_MEM_GUARD_BYTES//(1024*1024)}MB, "
                       f"gc.collect() freed {collected} objects")
    return _rss_bytes() > _MEM_GUARD_BYTES


async def get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        # NO lifetime cap (total=None): long agent tasks (10min+ tool loops)
        # must survive. Dead-upstream protection lives elsewhere:
        #   - streaming: transform_stream STALE_STREAM_TIMEOUT=120s
        #     (gateway emits ": keepalive" every 10s, so 120s idle = dead)
        #   - non-streaming: per-request timeout in _passthrough_non_streaming
        timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=None)
        # read_bufsize: single SSE line soft-cap is handled in transform_stream
        # (readuntil max_size=6MB). Keep session buffer modest — gateway must
        # redact multimodal base64 from hermes.tool.progress so we never need
        # 20MB×N headroom here (that would thrash RAM under MemoryMax).
        # high_water = read_bufsize * 2 = 12MB (above 6MB line cap).
        
        # ✅ 關鍵修復：設定 auto_decompress=False 避免額外的解壓縮開銷
        # 並確保 timer_host 正確設定以支援 backpressure
        _http_session = aiohttp.ClientSession(
            timeout=timeout,
            read_bufsize=6 * 1024 * 1024,
            auto_decompress=False,  # 避免不必要的解壓縮
        )
    return _http_session
