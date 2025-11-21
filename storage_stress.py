"""MTP 存储压力测试脚本。

- 使用 pymtp 将本地生成的测试文件推送到平板的 Download 目录。
- 支持高并发推送、超时保护、自动重连与低空间清理，能够 7×24 小时运行。
- 事件实时落表（CSV），错误另存日志，结束生成摘要报告。
"""

import argparse
import csv
import importlib
import importlib.util as importlib_util
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


# ----------------------------- 配置与状态 -----------------------------


@dataclass
class StressTestConfig:
    target_dir: str = "Download"
    log_dir: Path = Path("logs")
    large_payload: Path = Path.home() / "Desktop" / "pad_test.iso"
    small_payload_dir: Path = Path.home() / "Desktop" / "pad_small_files"
    run_hours: int = 24 * 7
    worker_count: int = 12
    queue_size: int = 200
    free_space_threshold_mb: int = 10_000  # 触发清理的剩余空间下限
    cleanup_batch: int = 150  # 每次清理删除的文件数量
    cleanup_margin_mb: int = 30_000  # 清理后希望保留的空间
    push_timeout: int = 900  # 单个文件推送超时时间（秒）
    stall_minutes: int = 15  # 多久无成功即视为卡死
    health_interval: int = 60
    large_interval: int = 5  # 每隔 N 个任务插入一次大文件
    verify_transfer: bool = True  # 上传后校验大小
    storage_keywords: Tuple[str, ...] = ("cpad", "内部共享", "shared", "internal")
    reconnect_delay: int = 8
    max_consecutive_failures: int = 20
    max_reconnect_attempts: int = 3


@dataclass
class StressStats:
    pushes: int = 0
    success: int = 0
    failures: int = 0
    cleanups: int = 0
    bytes_sent: int = 0
    last_success_ts: float = field(default_factory=lambda: time.time())
    last_error: str = ""
    consecutive_failures: int = 0


# ----------------------------- MTP 封装 -----------------------------


class MTPSession:
    """对 pymtp 的薄封装，提供路径创建、推送、清理能力。"""

    def __init__(self, config: StressTestConfig):
        try:
            find_spec = importlib_util.find_spec
        except AttributeError:
            raise RuntimeError(
                "标准库 importlib.util 不可用，可能被第三方同名包覆盖，请卸载 pip 包 `importlib` 再重试"
            )

        if find_spec("pymtp") is None:
            raise RuntimeError("缺少依赖 pymtp，请先执行: pip install pymtp")

        try:
            self._pymtp = importlib.import_module("pymtp")
        except AttributeError:
            raise RuntimeError(
                "内置 importlib 被第三方覆盖，导致 import_module 不可用；请移除 pip 包 `importlib` 或重建虚拟环境"
            )
        self.config = config
        self.device = self._pymtp.MTP()
        self.storage_id: Optional[int] = None
        self.target_folder_id: Optional[int] = None
        self.connect()

    def connect(self) -> None:
        self.device.connect()
        storages_raw = self.device.get_storage()
        if storages_raw is None:
            raise RuntimeError("设备未正确枚举为 MTP 存储，请检查数据线或驱动后重试")
        storages = list(storages_raw)
        if not storages:
            raise RuntimeError("未检测到任何 MTP 存储，请确认设备以 MTP 模式连接")
        target = self._pick_storage(storages)
        self.storage_id = target.id
        self.device.set_default_storage(self.storage_id)
        self.target_folder_id = self.ensure_remote_path(self.config.target_dir)

    def _pick_storage(self, storages: Iterable) -> object:
        # 优先匹配关键词，否则回落首个
        for st in storages:
            label_parts = [getattr(st, "description", ""), getattr(st, "volume_label", "")]
            label = " ".join([p or "" for p in label_parts]).lower()
            if any(key in label for key in self.config.storage_keywords):
                return st
        return list(storages)[0]

    def ensure_remote_path(self, path: str) -> int:
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        parent = 0
        folder_list = self.device.get_folder_list() or []
        for part in parts:
            match = None
            for f in folder_list:
                if f.storage_id == self.storage_id and f.parent_id == parent and f.name == part:
                    match = f.folder_id
                    break
            if match is None:
                match = self.device.create_folder(part, parent, self.storage_id)
                folder_list = self.device.get_folder_list() or []
            parent = match
        return parent

    def free_space_mb(self) -> Optional[int]:
        storages = self.device.get_storage() or []
        for st in storages:
            if getattr(st, "id", None) == self.storage_id:
                try:
                    return int(st.free_space_in_bytes) // (1024 * 1024)
                except Exception:
                    return None
        return None

    def list_target_files(self) -> List[Tuple[int, str, int]]:
        listing = self.device.get_filelisting() or []
        files: List[Tuple[int, str, int]] = []
        for item in listing:
            try:
                parent_id = getattr(item, "parent_id", getattr(item, "parent", 0))
                if parent_id != self.target_folder_id:
                    continue
                obj_id = getattr(item, "id", getattr(item, "item_id", None))
                size = getattr(item, "filesize", getattr(item, "size", 0))
                if obj_id is not None:
                    files.append((obj_id, item.filename, int(size)))
            except Exception:
                continue
        return files

    def delete_objects(self, obj_ids: List[int]) -> None:
        for obj_id in obj_ids:
            self.device.delete_object(obj_id)

    def send_file(self, local_path: Path, remote_name: str) -> None:
        self.device.send_file_from_file(str(local_path), self.target_folder_id, remote_name)

    def stat_file(self, remote_name: str) -> Optional[int]:
        for obj_id, name, size in self.list_target_files():
            if name == remote_name:
                return size
        return None

    def reconnect(self) -> None:
        try:
            self.device.disconnect()
        except Exception:
            pass
        self.device = self._pymtp.MTP()
        self.connect()

    def close(self) -> None:
        try:
            self.device.disconnect()
        except Exception:
            pass


# ----------------------------- 日志与事件 -----------------------------


def record_event(event: str, message: str, free_space: Optional[int], extra: Optional[str] = None) -> Dict[str, str]:
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "event": event,
        "message": message,
        "free_space_mb": free_space if free_space is not None else "unknown",
        "extra": extra or "",
    }


class EventLogger:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        self.events_path = self.log_dir / f"storage_stress_{ts}.csv"
        self.error_path = self.log_dir / f"storage_errors_{ts}.log"
        self.summary_path = self.log_dir / f"storage_summary_{ts}.txt"
        self._event_file = self.events_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(
            self._event_file, fieldnames=["timestamp", "event", "message", "free_space_mb", "extra"]
        )
        self._writer.writeheader()
        self._lock = threading.Lock()
        self._error_file = self.error_path.open("w", encoding="utf-8")

    def log_event(self, event: Dict[str, str]) -> None:
        with self._lock:
            self._writer.writerow(event)
            self._event_file.flush()

    def log_error(self, message: str) -> None:
        line = f"{datetime.utcnow().isoformat()} | {message}\n"
        with self._lock:
            self._error_file.write(line)
            self._error_file.flush()

    def write_summary(self, summary: str) -> None:
        self.summary_path.write_text(summary, encoding="utf-8")

    def close(self) -> None:
        try:
            self._event_file.close()
        finally:
            self._error_file.close()


# ----------------------------- 核心逻辑 -----------------------------


def load_payloads(config: StressTestConfig) -> Tuple[Path, List[Path]]:
    if not config.large_payload.exists():
        raise RuntimeError(f"缺少大文件: {config.large_payload}，请先运行 file_generator.py 生成")
    small_files = sorted(config.small_payload_dir.glob("doc_*.txt"))
    if not small_files:
        raise RuntimeError(f"未找到小文件包: {config.small_payload_dir}/doc_*.txt，请先生成测试文件")
    return config.large_payload, small_files


def enqueue_tasks(
    task_queue: "queue.Queue[Tuple[Path, str, str]]",
    stop_event: threading.Event,
    config: StressTestConfig,
    small_files: List[Path],
    large_file: Path,
) -> None:
    small_idx = 0
    push_count = 0
    while not stop_event.is_set():
        if task_queue.full():
            time.sleep(0.05)
            continue
        push_count += 1
        if push_count % config.large_interval == 0:
            payload = large_file
            tag = "large"
        else:
            payload = small_files[small_idx % len(small_files)]
            small_idx += 1
            tag = "small"
        remote_name = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}_{tag}_{payload.name}"
        task_queue.put((payload, remote_name, tag))
    # 通知所有 worker 退出
    for _ in range(config.worker_count):
        task_queue.put((Path(), "", "stop"))


def cleanup_space(
    session: MTPSession,
    config: StressTestConfig,
    logger: EventLogger,
    reason: str,
    stats: Optional[StressStats] = None,
    stats_lock: Optional[threading.Lock] = None,
) -> None:
    files = session.list_target_files()
    files.sort(key=lambda x: x[2], reverse=True)  # 先删除大文件，加速释放
    to_delete = [obj_id for obj_id, _, _ in files[: config.cleanup_batch]]
    if not to_delete:
        logger.log_event(record_event("cleanup_skip", "没有可清理的文件", session.free_space_mb(), reason))
        return
    session.delete_objects(to_delete)
    if stats and stats_lock:
        with stats_lock:
            stats.cleanups += 1
    logger.log_event(record_event("cleanup", f"删除 {len(to_delete)} 个文件", session.free_space_mb(), reason))


def ensure_space(
    session: MTPSession,
    config: StressTestConfig,
    logger: EventLogger,
    stats: Optional[StressStats] = None,
    stats_lock: Optional[threading.Lock] = None,
) -> None:
    free_mb = session.free_space_mb()
    if free_mb is None:
        logger.log_error("无法获取剩余空间，跳过清理")
        return
    if free_mb >= config.free_space_threshold_mb:
        return
    cleanup_space(session, config, logger, f"low_space({free_mb}MB)", stats, stats_lock)
    refreshed = session.free_space_mb()
    if refreshed is not None and refreshed < config.cleanup_margin_mb:
        cleanup_space(session, config, logger, "post_cleanup_margin", stats, stats_lock)


def send_with_timeout(session: MTPSession, local_path: Path, remote_name: str, timeout: int) -> Optional[str]:
    result: Dict[str, Optional[str]] = {"error": None}

    def _send() -> None:
        try:
            session.send_file(local_path, remote_name)
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)

    thread = threading.Thread(target=_send, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        return "push timeout"
    return result["error"]


def worker_loop(
    worker_id: int,
    config: StressTestConfig,
    task_queue: "queue.Queue[Tuple[Path, str, str]]",
    stop_event: threading.Event,
    stats: StressStats,
    stats_lock: threading.Lock,
    logger: EventLogger,
) -> None:
    session: Optional[MTPSession] = None
    try:
        session = MTPSession(config)
        logger.log_event(record_event("worker_start", f"worker {worker_id} ready", session.free_space_mb()))
        while not stop_event.is_set():
            try:
                payload, remote_name, tag = task_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if tag == "stop":
                task_queue.task_done()
                break

            with stats_lock:
                stats.pushes += 1
            try:
                ensure_space(session, config, logger, stats, stats_lock)
                err = send_with_timeout(session, payload, remote_name, config.push_timeout)
                if err is None:
                    if config.verify_transfer:
                        size_ok = session.stat_file(remote_name) == payload.stat().st_size
                        if not size_ok:
                            raise RuntimeError("size mismatch")
                    with stats_lock:
                        stats.success += 1
                        stats.bytes_sent += payload.stat().st_size
                        stats.last_success_ts = time.time()
                        stats.consecutive_failures = 0
                    logger.log_event(
                        record_event("push_ok", f"worker{worker_id}:{remote_name}", session.free_space_mb(), tag)
                    )
                else:
                    raise RuntimeError(err)
            except Exception as exc:  # noqa: BLE001
                with stats_lock:
                    stats.failures += 1
                    stats.consecutive_failures += 1
                    stats.last_error = str(exc)
                logger.log_event(record_event("push_fail", str(exc), session.free_space_mb(), remote_name))
                logger.log_error(f"worker{worker_id} push error: {exc}")
                if stats.consecutive_failures >= config.max_consecutive_failures:
                    stop_event.set()
                reconnect_attempts = 0
                while reconnect_attempts < config.max_reconnect_attempts and not stop_event.is_set():
                    reconnect_attempts += 1
                    try:
                        time.sleep(config.reconnect_delay)
                        session.reconnect()
                        logger.log_event(record_event("reconnect", "MTP 重连成功", session.free_space_mb()))
                        break
                    except Exception as recon_err:  # noqa: BLE001
                        logger.log_error(f"worker{worker_id} reconnect failed: {recon_err}")
                task_queue.task_done()
                continue
            task_queue.task_done()
    except Exception as fatal:  # noqa: BLE001
        logger.log_error(f"worker{worker_id} fatal: {fatal}")
        stop_event.set()
    finally:
        if session:
            session.close()


def health_monitor(
    session: MTPSession,
    config: StressTestConfig,
    stats: StressStats,
    stats_lock: threading.Lock,
    stop_event: threading.Event,
    logger: EventLogger,
) -> None:
    while not stop_event.is_set():
        time.sleep(config.health_interval)
        free_mb = None
        try:
            free_mb = session.free_space_mb()
        except Exception as exc:  # noqa: BLE001
            logger.log_error(f"health free_space error: {exc}")
        logger.log_event(record_event("heartbeat", "health check", free_mb))

        with stats_lock:
            idle_minutes = (time.time() - stats.last_success_ts) / 60
        if idle_minutes >= config.stall_minutes:
            logger.log_event(record_event("stall", f"{idle_minutes:.1f}min no success", free_mb))
            try:
                cleanup_space(session, config, logger, "stall_cleanup", stats, stats_lock)
            except Exception as exc:  # noqa: BLE001
                logger.log_error(f"stall cleanup failed: {exc}")
            with stats_lock:
                stats.consecutive_failures = 0


# ----------------------------- 运行入口 -----------------------------


def run(config: StressTestConfig) -> None:
    large_file, small_files = load_payloads(config)
    logger = EventLogger(config.log_dir)
    stats = StressStats()
    stats_lock = threading.Lock()
    stop_event = threading.Event()
    task_queue: "queue.Queue[Tuple[Path, str, str]]" = queue.Queue(maxsize=config.queue_size)
    start_ts = time.time()

    controller_session = MTPSession(config)
    logger.log_event(record_event("start", "测试启动", controller_session.free_space_mb()))

    producer = threading.Thread(
        target=enqueue_tasks, args=(task_queue, stop_event, config, small_files, large_file), daemon=True
    )
    producer.start()

    workers = [
        threading.Thread(
            target=worker_loop,
            args=(i + 1, config, task_queue, stop_event, stats, stats_lock, logger),
            daemon=True,
        )
        for i in range(config.worker_count)
    ]
    for t in workers:
        t.start()

    health = threading.Thread(
        target=health_monitor,
        args=(controller_session, config, stats, stats_lock, stop_event, logger),
        daemon=True,
    )
    health.start()

    end_time = datetime.utcnow() + timedelta(hours=config.run_hours)
    try:
        while datetime.utcnow() < end_time and not stop_event.is_set():
            time.sleep(1)
    finally:
        stop_event.set()
        for _ in workers:
            task_queue.put((Path(), "", "stop"))
        producer.join(timeout=5)
        task_queue.join()
        for t in workers:
            t.join(timeout=10)
        health.join(timeout=5)
        controller_session.close()
        summary = build_summary(stats, config, logger, start_ts)
        logger.write_summary(summary)
        logger.log_event(record_event("complete", "测试完成", None))
        logger.close()
        print(summary)


def build_summary(stats: StressStats, config: StressTestConfig, logger: EventLogger, start_ts: float) -> str:
    duration = timedelta(seconds=int(time.time() - start_ts))
    lines = [
        "存储压力测试报告",
        "================",
        f"目标运行时长: {config.run_hours} 小时",
        f"实际运行: {duration}",
        f"成功: {stats.success}",
        f"失败: {stats.failures}",
        f"总推送: {stats.pushes}",
        f"清理次数: {stats.cleanups}",
        f"总传输: {stats.bytes_sent / 1024 / 1024:.2f} MB",
        "",
        f"事件日志: {logger.events_path}",
        f"错误日志: {logger.error_path}",
        f"摘要文件: {logger.summary_path}",
    ]
    if stats.last_error:
        lines.append(f"最后错误: {stats.last_error}")
    return "\n".join(lines)


def parse_args() -> StressTestConfig:
    parser = argparse.ArgumentParser(description="安卓平板 MTP 存储压力测试")
    parser.add_argument("--run-hours", type=int, default=StressTestConfig.run_hours)
    parser.add_argument("--workers", type=int, default=StressTestConfig.worker_count)
    parser.add_argument("--target-dir", type=str, default=StressTestConfig.target_dir)
    parser.add_argument("--free-space-mb", type=int, default=StressTestConfig.free_space_threshold_mb)
    parser.add_argument("--cleanup-batch", type=int, default=StressTestConfig.cleanup_batch)
    parser.add_argument("--large-interval", type=int, default=StressTestConfig.large_interval)
    parser.add_argument("--queue-size", type=int, default=StressTestConfig.queue_size)
    args = parser.parse_args()

    return StressTestConfig(
        run_hours=max(1, args.run_hours),
        worker_count=max(1, args.workers),
        target_dir=args.target_dir,
        free_space_threshold_mb=max(1024, args.free_space_mb),
        cleanup_batch=max(10, args.cleanup_batch),
        large_interval=max(1, args.large_interval),
        queue_size=max(10, args.queue_size),
    )


if __name__ == "__main__":
    try:
        run(parse_args())
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"Fatal error: {exc}\n")
        sys.exit(1)
