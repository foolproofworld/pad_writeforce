import csv
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from dataclasses import dataclass
from datetime import datetime, timedelta
from random import choice
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class StressTestConfig:
    device_id: Optional[str] = None
    mtp_storage_id: Optional[int] = None
    target_dir: str = "storage_stress"
    local_log_dir: str = "logs"
    large_payload: Path = Path.home() / "Desktop" / "pad_test.iso"
    small_payload_dir: Path = Path.home() / "Desktop" / "pad_small_files"
    free_space_threshold_mb: int = 5_000
    run_hours: int = 24 * 7
    push_timeout: int = 600
    poll_interval_seconds: int = 1
    cleanup_batch_size: int = 5
    reconnect_delay_seconds: int = 10
    num_workers: int = 8
    large_push_interval: int = 5
    health_check_interval: int = 60
    thermal_limit_c: int = 65
    stall_timeout_minutes: int = 10
    task_queue_multiplier: int = 8


@dataclass
class StressStats:
    pushes: int = 0
    success: int = 0
    failures: int = 0
    cleanups: int = 0
    bytes_sent: int = 0
    last_event: str = ""
    last_success_ts: float = 0.0
    active_workers: int = 0
    inflight_pushes: int = 0


class MTPClient:
    """轻量封装 pymtp，提供路径创建、推送、清理等操作。"""

    def __init__(self, config: StressTestConfig):
        try:
            import pymtp  # type: ignore
        except ImportError as exc:  # noqa: BLE001
            raise RuntimeError("缺少依赖 pymtp，请先 pip install pymtp") from exc

        self._pymtp = pymtp
        self.device = pymtp.MTP()
        self.device.connect()
        storages = self.device.get_storage()
        if not storages:
            raise RuntimeError("未检测到 MTP 存储，请检查设备是否以 MTP 模式连接")

        if config.mtp_storage_id:
            target_storage = next((s for s in storages if s.id == config.mtp_storage_id), storages[0])
        else:
            target_storage = storages[0]
        self.storage_id = target_storage.id
        self.device.set_default_storage(self.storage_id)
        self.target_folder_id = self.ensure_folder_path(config.target_dir)

    def ensure_folder_path(self, path: str) -> int:
        segments = [seg for seg in path.replace("\\", "/").split("/") if seg]
        parent_id = 0  # root
        folder_list = self.device.get_folder_list() or []
        for seg in segments:
            folder_id = None
            for folder in folder_list:
                if folder.storage_id == self.storage_id and folder.parent_id == parent_id and folder.name == seg:
                    folder_id = folder.folder_id
                    break
            if folder_id is None:
                folder_id = self.device.create_folder(seg, parent_id, self.storage_id)
                folder_list = self.device.get_folder_list() or []
            parent_id = folder_id
        return parent_id

    def free_space_mb(self) -> Optional[int]:
        storages = self.device.get_storage()
        for st in storages:
            if st.id == self.storage_id:
                try:
                    return int(st.free_space_in_bytes) // (1024 * 1024)
                except Exception:  # noqa: BLE001
                    return None
        return None

    def list_files(self) -> List[Tuple[int, str, int]]:
        files = []
        listing = self.device.get_filelisting() or []
        for item in listing:
            try:
                parent_id = getattr(item, "parent_id", getattr(item, "parent", 0))
                if parent_id == self.target_folder_id:
                    obj_id = getattr(item, "id", getattr(item, "item_id", None))
                    if obj_id is None:
                        continue
                    files.append((obj_id, item.filename, getattr(item, "filesize", getattr(item, "size", 0))))
            except Exception:  # noqa: BLE001
                continue
        return files

    def send_file(self, local_path: Path, remote_name: str) -> None:
        self.device.send_file_from_file(str(local_path), self.target_folder_id, remote_name)

    def delete_object(self, obj_id: int) -> None:
        self.device.delete_object(obj_id)

    def stat_file(self, remote_name: str) -> Optional[int]:
        for obj_id, name, size in self.list_files():
            if name == remote_name:
                return size
        return None

    def close(self) -> None:
        try:
            self.device.disconnect()
        except Exception:
            pass


def ensure_target_dir(config: StressTestConfig) -> None:
    client = MTPClient(config)
    client.close()


def ensure_local_logs(config: StressTestConfig) -> None:
    os.makedirs(config.local_log_dir, exist_ok=True)


def detect_device(config: StressTestConfig) -> None:
    """提前校验 MTP 连接与必需文件，便于在启动 GUI 前快速报错。"""

    # 确认大/小文件存在
    load_payloads(config)

    # 确认日志目录可写
    ensure_local_logs(config)

    # 确认 MTP 设备可连接、目标目录可创建
    client = MTPClient(config)
    try:
        client.free_space_mb()
    finally:
        client.close()


def load_payloads(config: StressTestConfig) -> List[Path]:
    if not config.large_payload.exists():
        raise RuntimeError(f"缺少大文件: {config.large_payload}")

    small_files = sorted(config.small_payload_dir.glob("doc_*.txt"))
    if not small_files:
        raise RuntimeError(
            f"未找到小文件包，请在 {config.small_payload_dir} 下生成 doc_*.txt"
        )

    return [config.large_payload] + small_files


def cleanup_device_storage(
    client: MTPClient, config: StressTestConfig, csv_writer: csv.DictWriter, writer_lock: threading.Lock, error_log, reason: str
) -> None:
    try:
        files = client.list_files()
    except Exception as exc:  # noqa: BLE001
        with writer_lock:
            csv_writer.writerow(record_event("cleanup_failed", str(exc), config, None, reason))
            log_error_line(error_log, f"cleanup_failed: {exc}")
        return

    removed = 0
    for obj_id, name, _ in files:
        if removed >= config.cleanup_batch_size:
            break
        try:
            client.delete_object(obj_id)
            removed += 1
            with writer_lock:
                csv_writer.writerow(record_event("cleanup_deleted", "", config, None, name))
        except Exception as exc:  # noqa: BLE001
            with writer_lock:
                csv_writer.writerow(record_event("cleanup_error", str(exc), config, None, name))
                log_error_line(error_log, f"cleanup_error: {exc} ({name})")


def wipe_device_storage(
    client: MTPClient, config: StressTestConfig, csv_writer: csv.DictWriter, writer_lock: threading.Lock, error_log, reason: str
) -> None:
    """清空目标目录下所有文件，适用于磁盘写满或校验失败后的快速恢复。"""
    errors: List[str] = []
    try:
        files = client.list_files()
        for obj_id, name, _ in files:
            try:
                client.delete_object(obj_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {exc}")
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))

    with writer_lock:
        if errors:
            csv_writer.writerow(
                record_event("wipe_all_error", "; ".join(errors), config, client.free_space_mb(), reason)
            )
            log_error_line(error_log, f"wipe_all_error: {'; '.join(errors)}")
        else:
            csv_writer.writerow(record_event("wipe_all", "已清空目标目录", config, client.free_space_mb(), reason))


def check_free_space(client: MTPClient) -> Optional[int]:
    return client.free_space_mb()


def sample_device_temperature(config: StressTestConfig) -> Tuple[Optional[float], str, str]:
    """MTP 模式下无法直接读取温度，若系统存在 adb 则尝试辅助采样。"""
    import shutil
    adb_path = shutil.which("adb")
    if not adb_path:
        return None, "unsupported", "adb not available for temp sampling"

    def run_local_adb(args: List[str]) -> Tuple[int, str, str]:
        process = subprocess.Popen([adb_path] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            return -1, "", "timeout"
        return process.returncode, out.strip(), err.strip()

    code, out, err = run_local_adb(["shell", "for f in /sys/class/thermal/thermal_zone*/temp; do [ -f \"$f\" ] && cat \"$f\"; done"])
    raw_output = out or err
    if code == 0 and raw_output:
        temps: List[float] = []
        for line in raw_output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = float(line)
                temps.append(value / 1000 if value > 200 else value)
            except ValueError:
                continue
        if temps:
            return max(temps), "thermal_zone", raw_output

    b_code, b_out, b_err = run_local_adb(["shell", "dumpsys", "battery"])
    battery_raw = b_out or b_err
    if b_code == 0 and battery_raw:
        for line in battery_raw.splitlines():
            if "temperature" in line.lower():
                parts = line.split(":")
                if len(parts) == 2:
                    try:
                        value = float(parts[1].strip())
                        return value / 10, "battery", battery_raw
                    except ValueError:
                        break
    return None, "unknown", battery_raw or raw_output or ""


def record_event(event_type: str, message: str, config: StressTestConfig, free_space_mb: Optional[int], extra: Optional[str] = None) -> dict:
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "event": event_type,
        "message": message,
        "free_space_mb": free_space_mb if free_space_mb is not None else "unknown",
        "extra": extra if extra else "",
    }


def log_error_line(log_file, message: str) -> None:
    log_file.write(f"{datetime.utcnow().isoformat()} | {message}\n")
    log_file.flush()


def push_file(
    client: MTPClient,
    config: StressTestConfig,
    local_path: Path,
    remote_name: str,
    csv_writer: csv.DictWriter,
    writer_lock: threading.Lock,
    error_log,
    stats: StressStats,
    stats_lock: threading.Lock,
    worker_id: int,
) -> Tuple[bool, str, bool]:
    with stats_lock:
        stats.pushes += 1
    result: Dict[str, Optional[str]] = {"error": None}

    def do_send() -> None:
        try:
            client.send_file(local_path, remote_name)
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)

    sender = threading.Thread(target=do_send)
    sender.start()
    sender.join(timeout=config.push_timeout)
    if sender.is_alive():
        error_message = "push timeout"
        with stats_lock:
            stats.failures += 1
            stats.last_event = f"[线程{worker_id}] 推送超时"
            if stats.inflight_pushes > 0:
                stats.inflight_pushes -= 1
        with writer_lock:
            csv_writer.writerow(
                record_event(
                    "push_timeout",
                    error_message,
                    config,
                    check_free_space(client),
                    f"worker={worker_id}, path={remote_name}",
                )
            )
            log_error_line(error_log, f"push_timeout: {remote_name}")
        return False, error_message, False

    if result["error"] is None:
        size = local_path.stat().st_size
        with stats_lock:
            stats.success += 1
            stats.bytes_sent += size
            stats.last_event = f"[线程{worker_id}] 推送成功: {remote_name}"
            stats.last_success_ts = time.time()
            if stats.inflight_pushes > 0:
                stats.inflight_pushes -= 1
        with writer_lock:
            csv_writer.writerow(
                record_event(
                    "push_success",
                    "mtp push ok",
                    config,
                    check_free_space(client),
                    f"worker={worker_id}, path={remote_name}",
                )
            )
        return True, "", True

    error_message = result["error"] or "unknown push error"
    event_label = "push_failed"
    lowered = error_message.lower()
    if "no space" in lowered:
        event_label = "push_nospace"
    elif "read-only" in lowered:
        event_label = "push_readonly"
    elif "permission" in lowered:
        event_label = "push_permission"
    elif "i/o" in lowered or "ioerror" in lowered:
        event_label = "push_ioerror"

    with stats_lock:
        stats.failures += 1
        stats.last_event = f"[线程{worker_id}] 推送失败: {error_message}"
        if stats.inflight_pushes > 0:
            stats.inflight_pushes -= 1
    with writer_lock:
        csv_writer.writerow(
            record_event(
                event_label,
                error_message,
                config,
                check_free_space(client),
                f"worker={worker_id}, path={remote_name}",
            )
        )
        log_error_line(error_log, f"{event_label}: {error_message} ({remote_name})")
    return False, error_message, not sender.is_alive()




def verify_remote_size(client: MTPClient, remote_name: str, expected_size: int) -> Tuple[bool, str]:
    size = client.stat_file(remote_name)
    if size is None:
        return False, "stat failed"
    if size != expected_size:
        return False, f"size mismatch remote={size} local={expected_size}"
    return True, ""


def run_stress_test(config: StressTestConfig, ui_queue: queue.Queue, start_time: datetime) -> None:
    ensure_local_logs(config)
    payloads = load_payloads(config)
    ensure_target_dir(config)
    stats = StressStats()
    stats.last_success_ts = time.time()
    stats_lock = threading.Lock()
    writer_lock = threading.Lock()
    cleanup_lock = threading.Lock()
    payload_lock = threading.Lock()
    task_queue: queue.Queue[Tuple[Optional[Path], str]] = queue.Queue(
        maxsize=max(2, config.num_workers * config.task_queue_multiplier)
    )

    log_path = os.path.join(config.local_log_dir, f"storage_stress_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv")
    error_log_path = os.path.join(config.local_log_dir, f"storage_errors_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.log")

    with open(log_path, "w", newline="", encoding="utf-8") as csvfile, open(
        error_log_path, "w", encoding="utf-8"
    ) as error_log:
        fieldnames = ["timestamp", "event", "message", "free_space_mb", "extra"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        end_time = start_time + timedelta(hours=config.run_hours)
        stop_event = threading.Event()
        startup_client = MTPClient(config)
        writer.writerow(record_event("start", "测试开始", config, check_free_space(startup_client)))
        startup_client.close()
        ui_queue.put({"type": "status", "message": "测试已启动", "stats": stats})

        small_payloads = payloads[1:]
        large_payload = payloads[0]
        pushes_since_large = config.large_push_interval

        def producer() -> None:
            nonlocal pushes_since_large
            while True:
                if stop_event.is_set():
                    break
                if datetime.utcnow() >= end_time:
                    stop_event.set()
                    break
                if task_queue.full():
                    time.sleep(0.05)
                    continue
                with payload_lock:
                    if pushes_since_large >= config.large_push_interval:
                        payload = large_payload
                        pushes_since_large = 0
                    else:
                        payload = choice(small_payloads)
                        pushes_since_large += 1
                remote_name = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_q{task_queue.qsize()}_{payload.name}"
                task_queue.put((payload, remote_name))
            # 结束时给每个 worker 一个停止标识
            for _ in range(config.num_workers):
                task_queue.put((None, ""))

        def worker(worker_id: int) -> None:
            client: Optional[MTPClient] = None
            try:
                client = MTPClient(config)
                with stats_lock:
                    stats.active_workers += 1
                    stats.last_event = f"线程 {worker_id} 已启动"
                with writer_lock:
                    writer.writerow(record_event("worker_start", f"worker {worker_id} ready", config, check_free_space(client)))
                ui_queue.put({"type": "status", "message": stats.last_event, "stats": stats})
                while not stop_event.is_set():
                    try:
                        payload, file_name = task_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue

                    if payload is None:
                        task_queue.task_done()
                        break

                    try:
                        free_space = check_free_space(client)
                        if free_space is not None and free_space < config.free_space_threshold_mb:
                            with stats_lock:
                                stats.cleanups += 1
                                stats.last_event = "剩余空间不足，执行全量清理"
                            with writer_lock:
                                writer.writerow(
                                    record_event("cleanup_needed", "剩余空间低于阈值，执行全量清空", config, free_space)
                                )
                                log_error_line(error_log, "cleanup_needed: free space below threshold")
                            with cleanup_lock:
                                wipe_device_storage(client, config, writer, writer_lock, error_log, "low_free_space")
                            ui_queue.put({"type": "status", "message": stats.last_event, "stats": stats})
                            continue
                        elif free_space is None:
                            with writer_lock:
                                writer.writerow(record_event("free_space_unknown", "无法读取 MTP 空间", config, None))
                                log_error_line(error_log, "free_space_unknown: mtp free space unavailable")

                        with writer_lock:
                            writer.writerow(
                                record_event(
                                    "push_begin",
                                    f"worker={worker_id} -> {file_name}",
                                    config,
                                    check_free_space(client),
                                )
                            )
                        with stats_lock:
                            stats.last_event = f"[线程{worker_id}] 正在推送 {payload.name}"
                            stats.inflight_pushes += 1
                        ui_queue.put({"type": "status", "message": stats.last_event, "stats": stats})

                        success, push_err, client_ok = push_file(
                            client, config, payload, file_name, writer, writer_lock, error_log, stats, stats_lock, worker_id
                        )
                        if success:
                            expected = payload.stat().st_size
                            ok, reason = verify_remote_size(client, file_name, expected)
                            if not ok:
                                with stats_lock:
                                    if stats.success > 0:
                                        stats.success -= 1
                                    stats.failures += 1
                                    stats.last_event = f"[线程{worker_id}] 校验失败: {reason}"
                                with writer_lock:
                                    writer.writerow(
                                        record_event(
                                            "verify_failed",
                                            reason,
                                            config,
                                            check_free_space(client),
                                            f"worker={worker_id}, path={file_name}",
                                        )
                                    )
                                    log_error_line(error_log, f"verify_failed: {reason} ({file_name})")
                                with cleanup_lock:
                                    wipe_device_storage(client, config, writer, writer_lock, error_log, "verify_failure")
                                success = False
                            else:
                                with writer_lock:
                                    writer.writerow(
                                        record_event(
                                            "verify_success",
                                            "remote size matched",
                                            config,
                                            check_free_space(client),
                                            f"worker={worker_id}, path={file_name}",
                                        )
                                    )
                        if not success:
                            if "no space" in push_err.lower():
                                with stats_lock:
                                    stats.last_event = "存储已满，执行全量清理"
                                with cleanup_lock:
                                    wipe_device_storage(client, config, writer, writer_lock, error_log, "no_space")
                            elif "timeout" in push_err.lower():
                                with stats_lock:
                                    stats.last_event = "推送超时，执行全量清理并重试"
                                with cleanup_lock:
                                    wipe_device_storage(client, config, writer, writer_lock, error_log, "push_timeout")
                                client_ok = False
                            else:
                                with cleanup_lock:
                                    cleanup_device_storage(client, config, writer, writer_lock, error_log, "push_failure")
                        ui_queue.put({"type": "status", "message": stats.last_event, "stats": stats})
                        if not client_ok:
                            try:
                                client.close()
                            except Exception:
                                pass
                            client = MTPClient(config)
                    finally:
                        task_queue.task_done()
            except Exception as exc:  # noqa: BLE001
                with writer_lock:
                    writer.writerow(record_event("worker_error", str(exc), config, check_free_space(client) if client else None))
                    log_error_line(error_log, f"worker_error: {exc}")
                with stats_lock:
                    stats.last_event = f"线程 {worker_id} 异常: {exc}"
                ui_queue.put({"type": "status", "message": stats.last_event, "stats": stats})
            finally:
                if client:
                    client.close()
                with stats_lock:
                    if stats.active_workers > 0:
                        stats.active_workers -= 1

        producer_thread = threading.Thread(target=producer, daemon=True)
        producer_thread.start()

        threads: List[threading.Thread] = []
        for idx in range(config.num_workers):
            t = threading.Thread(target=worker, args=(idx,), daemon=True)
            threads.append(t)
            t.start()

        def monitor_health() -> None:
            last_stall_report = 0.0
            health_client: Optional[MTPClient] = None
            try:
                health_client = MTPClient(config)
                while not stop_event.is_set():
                    # 热状况采样
                    try:
                        temp_c, source, raw = sample_device_temperature(config)
                        event_type = "thermal_sample"
                        message = f"{source}: {temp_c}C" if temp_c is not None else f"{source}: unknown ({raw})"
                        if temp_c is not None and temp_c >= config.thermal_limit_c:
                            event_type = "thermal_high"
                        with writer_lock:
                            writer.writerow(record_event(event_type, message, config, check_free_space(health_client), raw))
                            if event_type != "thermal_sample":
                                log_error_line(error_log, f"{event_type}: {message}")
                    except Exception as exc:  # noqa: BLE001
                        with writer_lock:
                            writer.writerow(record_event("health_unknown", str(exc), config, check_free_space(health_client)))
                            log_error_line(error_log, f"health_unknown: {exc}")

                    # 长时间无成功推送判定为卡死/停滞
                    now_ts = time.time()
                    with stats_lock:
                        last_ok = stats.last_success_ts
                    stall_seconds = config.stall_timeout_minutes * 60
                    if now_ts - last_ok > stall_seconds and now_ts - last_stall_report > stall_seconds:
                        last_stall_report = now_ts
                        with writer_lock:
                            writer.writerow(
                                record_event(
                                    "stall_detected",
                                    f"{config.stall_timeout_minutes}分钟无成功推送，触发全量清空以解卡",
                                    config,
                                    check_free_space(health_client),
                                )
                            )
                            log_error_line(
                                error_log, f"stall_detected: no success for {config.stall_timeout_minutes}分钟"
                            )
                        with stats_lock:
                            stats.last_event = "检测到长时间无成功推送，执行全量清空"
                        with cleanup_lock:
                            wipe_device_storage(health_client, config, writer, writer_lock, error_log, "stall_detected")
                        ui_queue.put({"type": "status", "message": stats.last_event, "stats": stats})

                    for _ in range(config.health_check_interval):
                        if stop_event.is_set():
                            break
                        time.sleep(1)
            finally:
                if health_client:
                    health_client.close()

        health_thread = threading.Thread(target=monitor_health, daemon=True)
        health_thread.start()

        while not stop_event.is_set():
            if datetime.utcnow() >= end_time:
                stop_event.set()
                break
            time.sleep(1)

        task_queue.join()

        for t in threads:
            t.join(timeout=5)
        producer_thread.join(timeout=5)
        health_thread.join(timeout=5)

        with writer_lock:
            completion_client = None
            try:
                completion_client = MTPClient(config)
                writer.writerow(record_event("complete", "测试完成", config, check_free_space(completion_client)))
            finally:
                if completion_client:
                    completion_client.close()
        ui_queue.put({"type": "status", "message": "测试已完成", "stats": stats})


def build_ui(config: StressTestConfig) -> None:
    ui_queue: queue.Queue = queue.Queue()

    root = tk.Tk()
    root.title("存储压力测试监控")
    root.geometry("560x360")

    start_time = datetime.utcnow()
    total_seconds = config.run_hours * 3600

    status_var = tk.StringVar(value="等待开始...")
    progress_var = tk.StringVar(value="已推送 0/0 成功/失败")
    cleanup_var = tk.StringVar(value="清理次数: 0")
    bytes_var = tk.StringVar(value="累计传输: 0 MB")
    time_var = tk.StringVar(value=f"累计运行: 0.00h / {config.run_hours}h")
    worker_var = tk.StringVar(value="活跃线程: 0")
    inflight_var = tk.StringVar(value="并行中的推送: 0")

    tk.Label(root, textvariable=status_var, anchor="w").pack(fill="x", padx=10, pady=5)
    tk.Label(root, textvariable=progress_var, anchor="w").pack(fill="x", padx=10, pady=5)
    tk.Label(root, textvariable=cleanup_var, anchor="w").pack(fill="x", padx=10, pady=5)
    tk.Label(root, textvariable=bytes_var, anchor="w").pack(fill="x", padx=10, pady=5)
    tk.Label(root, textvariable=time_var, anchor="w").pack(fill="x", padx=10, pady=5)
    tk.Label(root, textvariable=worker_var, anchor="w").pack(fill="x", padx=10, pady=5)
    tk.Label(root, textvariable=inflight_var, anchor="w").pack(fill="x", padx=10, pady=5)
    progress_bar = ttk.Progressbar(root, mode="determinate", maximum=total_seconds)
    progress_bar.pack(fill="x", padx=10, pady=5)

    log_box = tk.Text(root, height=10)
    log_box.pack(fill="both", expand=True, padx=10, pady=5)
    log_box.configure(state="disabled")

    def update_ui() -> None:
        try:
            while True:
                event = ui_queue.get_nowait()
                stats: StressStats = event.get("stats", StressStats())
                status_var.set(event.get("message", ""))
                progress_var.set(f"推送: {stats.success} 成功 / {stats.failures} 失败 (总尝试 {stats.pushes})")
                cleanup_var.set(f"清理次数: {stats.cleanups}")
                bytes_var.set(f"累计传输: {stats.bytes_sent // 1024 // 1024} MB")
                elapsed = max(0.0, (datetime.utcnow() - start_time).total_seconds())
                progress_bar["value"] = min(elapsed, total_seconds)
                time_var.set(
                    f"累计运行: {elapsed / 3600:.2f}h / {config.run_hours}h"
                )
                worker_var.set(f"活跃线程: {stats.active_workers} / {config.num_workers}")
                inflight_var.set(f"并行中的推送: {stats.inflight_pushes}")

                log_box.configure(state="normal")
                log_box.insert("end", f"{datetime.utcnow().strftime('%H:%M:%S')} - {event.get('message','')}\n")
                log_box.see("end")
                log_box.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(500, update_ui)

    worker = threading.Thread(target=run_stress_test, args=(config, ui_queue, start_time), daemon=True)
    worker.start()
    root.after(500, update_ui)
    root.mainloop()


if __name__ == "__main__":
    try:
        cfg = StressTestConfig()
        env_workers = os.getenv("STRESS_NUM_WORKERS")
        env_interval = os.getenv("STRESS_LARGE_INTERVAL")
        env_queue_mult = os.getenv("STRESS_QUEUE_MULT")

        if env_workers and env_workers.isdigit():
            cfg.num_workers = max(1, int(env_workers))
        if env_interval and env_interval.isdigit():
            cfg.large_push_interval = max(1, int(env_interval))
        if env_queue_mult and env_queue_mult.isdigit():
            cfg.task_queue_multiplier = max(2, int(env_queue_mult))

        detect_device(cfg)
        build_ui(cfg)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"Fatal error: {exc}\n")
        sys.exit(1)
