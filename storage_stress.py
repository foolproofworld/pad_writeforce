import csv
import os
import posixpath
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
    target_dir: str = "/sdcard/storage_stress"
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


def adb_base_args(device_id: Optional[str]) -> List[str]:
    args = ["adb"]
    if device_id:
        args.extend(["-s", device_id])
    return args


def run_adb_command(config: StressTestConfig, command: List[str], timeout: int = 30) -> Tuple[int, str, str]:
    full_cmd = adb_base_args(config.device_id) + command
    process = subprocess.Popen(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        return -1, "", f"Timeout running {' '.join(full_cmd)}"
    return process.returncode, stdout.strip(), stderr.strip()


def ensure_target_dir(config: StressTestConfig) -> None:
    code, _, err = run_adb_command(config, ["shell", "mkdir", "-p", config.target_dir])
    if code != 0:
        raise RuntimeError(f"Failed to create target directory: {err}")


def ensure_local_logs(config: StressTestConfig) -> None:
    os.makedirs(config.local_log_dir, exist_ok=True)


def load_payloads(config: StressTestConfig) -> List[Path]:
    if not config.large_payload.exists():
        raise RuntimeError(f"缺少大文件: {config.large_payload}")

    small_files = sorted(config.small_payload_dir.glob("doc_*.txt"))
    if not small_files:
        raise RuntimeError(
            f"未找到小文件包，请在 {config.small_payload_dir} 下生成 doc_*.txt"
        )

    return [config.large_payload] + small_files


def list_device_files(config: StressTestConfig) -> List[str]:
    code, out, err = run_adb_command(config, ["shell", "ls", "-t", config.target_dir])
    if code != 0:
        raise RuntimeError(f"Failed to list device files: {err}")
    files = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            files.append(os.path.join(config.target_dir, line))
    return files


def cleanup_device_storage(
    config: StressTestConfig, csv_writer: csv.DictWriter, writer_lock: threading.Lock, reason: str
) -> None:
    try:
        files = list_device_files(config)
    except Exception as exc:  # noqa: BLE001
        with writer_lock:
            csv_writer.writerow(record_event("cleanup_failed", str(exc), config, None, reason))
        return

    removed = 0
    for path in files:
        if removed >= config.cleanup_batch_size:
            break
        code, _, err = run_adb_command(config, ["shell", "rm", "-f", path])
        if code == 0:
            removed += 1
            with writer_lock:
                csv_writer.writerow(record_event("cleanup_deleted", "", config, None, path))
        else:
            with writer_lock:
                csv_writer.writerow(record_event("cleanup_error", err, config, None, path))


def wipe_device_storage(
    config: StressTestConfig, csv_writer: csv.DictWriter, writer_lock: threading.Lock, reason: str
) -> None:
    """清空目标目录下所有文件，适用于磁盘写满或校验失败后的快速恢复。"""
    code, _, err = run_adb_command(config, ["shell", "rm", "-rf", posixpath.join(config.target_dir, "*")])
    with writer_lock:
        if code == 0:
            csv_writer.writerow(
                record_event("wipe_all", "已清空目标目录", config, check_free_space(config), reason)
            )
        else:
            csv_writer.writerow(
                record_event("wipe_all_error", err or "wipe failed", config, check_free_space(config), reason)
            )

    ensure_target_dir(config)


def parse_free_space_mb(df_output: str, mount_path: str) -> Optional[int]:
    for line in df_output.splitlines():
        # Android df 输出通常为：Filesystem 1K-blocks Used Available Use% Mounted on
        # 可能出现目标目录不在 Mounted on 列，只能匹配包含的路径前缀。
        if mount_path in line or mount_path.rstrip("/") == line.split()[-1]:
            parts = line.split()
            if len(parts) >= 4:
                try:
                    available_kb = int(parts[3])
                    return available_kb // 1024
                except ValueError:
                    return None
    return None


def check_free_space(config: StressTestConfig) -> Optional[int]:
    # 一次性查询多个常见挂载点，尽量避免解析失败导致的 "unknown"
    code, out, err = run_adb_command(
        config,
        [
            "shell",
            "df",
            "-k",
            config.target_dir,
            "/sdcard",
            "/storage/emulated/0",
            "/data",
        ],
    )
    if code != 0:
        return None

    for mount in (config.target_dir, "/sdcard", "/storage/emulated/0", "/data"):
        value = parse_free_space_mb(out, mount)
        if value is not None:
            return value

    # 回退: 使用 stat -f 解析 bavail
    s_code, s_out, _ = run_adb_command(
        config,
        ["shell", "stat", "-f", "-c", "%a %S", config.target_dir],
    )
    if s_code == 0:
        try:
            avail_blocks, block_size = s_out.split()
            return (int(avail_blocks) * int(block_size)) // (1024 * 1024)
        except Exception:  # noqa: BLE001
            return None
    return None


def sample_device_temperature(config: StressTestConfig) -> Tuple[Optional[float], str, str]:
    """尝试读取设备温度，优先 thermal_zone，退化到 battery 温度。"""
    # 读取 thermal_zone，可能返回多行摄氏乘以 1000 的值
    code, out, err = run_adb_command(
        config,
        [
            "shell",
            "for f in /sys/class/thermal/thermal_zone*/temp; do [ -f \"$f\" ] && cat \"$f\"; done",
        ],
        timeout=15,
    )
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

    # 回退到 dumpsys battery temperature（以 0.1 摄氏度为单位）
    b_code, b_out, b_err = run_adb_command(config, ["shell", "dumpsys", "battery"], timeout=15)
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


def push_file(
    config: StressTestConfig,
    local_path: Path,
    remote_name: str,
    csv_writer: csv.DictWriter,
    writer_lock: threading.Lock,
    stats: StressStats,
    stats_lock: threading.Lock,
    worker_id: int,
) -> Tuple[bool, str]:
    remote_path = posixpath.join(config.target_dir.rstrip("/\\"), remote_name)
    code, out, err = run_adb_command(config, ["push", str(local_path), remote_path], timeout=config.push_timeout)
    with stats_lock:
        stats.pushes += 1
        stats.inflight_pushes += 1
    if code == 0:
        size = local_path.stat().st_size
        with stats_lock:
            stats.success += 1
            stats.bytes_sent += size
            stats.last_event = f"[线程{worker_id}] 推送成功: {remote_path}"
            stats.last_success_ts = time.time()
            if stats.inflight_pushes > 0:
                stats.inflight_pushes -= 1
        with writer_lock:
            csv_writer.writerow(
                record_event(
                    "push_success",
                    out,
                    config,
                    check_free_space(config),
                    f"worker={worker_id}, path={remote_path}",
                )
            )
        return True, ""
    error_message = err or out or "unknown push error"
    event_label = "push_failed"
    lowered = error_message.lower()
    if "no space" in lowered:
        event_label = "push_nospace"
    elif "read-only" in lowered:
        event_label = "push_readonly"
    elif "permission denied" in lowered:
        event_label = "push_permission"
    elif "i/o error" in lowered or "ioerror" in lowered:
        event_label = "push_ioerror"
    elif code == -1 or "timeout" in lowered:
        event_label = "push_timeout"

    with stats_lock:
        stats.failures += 1
        stats.last_event = f"[线程{worker_id}] 推送失败: {error_message}"
    with writer_lock:
        csv_writer.writerow(
            record_event(
                event_label,
                error_message,
                config,
                check_free_space(config),
                f"worker={worker_id}, path={remote_path}",
            )
        )
    with stats_lock:
        if stats.inflight_pushes > 0:
            stats.inflight_pushes -= 1
    return False, error_message


def detect_device(config: StressTestConfig) -> None:
    code, out, err = run_adb_command(config, ["devices"])
    if code != 0:
        raise RuntimeError(f"adb not working: {err}")
    lines = [line for line in out.splitlines() if "\tdevice" in line]
    if not lines:
        raise RuntimeError("未检测到可用的安卓设备 (adb devices 为空)")
    if config.device_id:
        if not any(line.startswith(config.device_id) for line in lines):
            raise RuntimeError(f"Device {config.device_id} not found in adb devices list")
    else:
        config.device_id = lines[0].split("\t")[0]


def ensure_device_ready(
    config: StressTestConfig,
    writer: csv.DictWriter,
    writer_lock: threading.Lock,
    stats: StressStats,
    stats_lock: threading.Lock,
) -> bool:
    code, out, err = run_adb_command(config, ["get-state"], timeout=10)
    if code == 0 and out.strip() == "device":
        return True
    with writer_lock:
        writer.writerow(record_event("device_retry", err or out or "device not ready", config, check_free_space(config)))
    with stats_lock:
        stats.last_event = "等待设备重新连接"
    time.sleep(config.reconnect_delay_seconds)
    wait_code, wait_out, wait_err = run_adb_command(config, ["wait-for-device"], timeout=120)
    if wait_code == 0:
        return True
    with writer_lock:
        writer.writerow(record_event("device_missing", wait_err or wait_out, config, check_free_space(config)))
    return False


def verify_remote_size(config: StressTestConfig, remote_path: str, expected_size: int) -> Tuple[bool, str]:
    code, out, err = run_adb_command(config, ["shell", "stat", "-c", "%s", remote_path], timeout=30)
    if code != 0:
        return False, err or out or "stat failed"
    try:
        remote_size = int(out.strip())
    except ValueError:
        return False, f"unexpected stat output: {out}"
    if remote_size != expected_size:
        return False, f"size mismatch remote={remote_size} local={expected_size}"
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

    with open(log_path, "w", newline="", encoding="utf-8") as csvfile:
        fieldnames = ["timestamp", "event", "message", "free_space_mb", "extra"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        end_time = start_time + timedelta(hours=config.run_hours)
        stop_event = threading.Event()
        writer.writerow(record_event("start", "测试开始", config, check_free_space(config)))
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
            try:
                with stats_lock:
                    stats.active_workers += 1
                    stats.last_event = f"线程 {worker_id} 已启动"
                with writer_lock:
                    writer.writerow(record_event("worker_start", f"worker {worker_id} ready", config, check_free_space(config)))
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
                        free_space = check_free_space(config)
                        if free_space is not None and free_space < config.free_space_threshold_mb:
                            with stats_lock:
                                stats.cleanups += 1
                                stats.last_event = "剩余空间不足，执行全量清理"
                            with writer_lock:
                                writer.writerow(
                                    record_event("cleanup_needed", "剩余空间低于阈值，执行全量清空", config, free_space)
                                )
                            with cleanup_lock:
                                wipe_device_storage(config, writer, writer_lock, "low_free_space")
                            ui_queue.put({"type": "status", "message": stats.last_event, "stats": stats})
                            continue
                        elif free_space is None:
                            with writer_lock:
                                writer.writerow(record_event("free_space_unknown", "无法解析 df 输出", config, None))

                        if not ensure_device_ready(config, writer, writer_lock, stats, stats_lock):
                            ui_queue.put({"type": "status", "message": "等待设备重连失败，稍后重试", "stats": stats})
                            continue

                        with writer_lock:
                            writer.writerow(
                                record_event(
                                    "push_begin",
                                    f"worker={worker_id} -> {file_name}",
                                    config,
                                    check_free_space(config),
                                )
                            )
                        with stats_lock:
                            stats.last_event = f"[线程{worker_id}] 正在推送 {payload.name}"
                        ui_queue.put({"type": "status", "message": stats.last_event, "stats": stats})

                        success, push_err = push_file(config, payload, file_name, writer, writer_lock, stats, stats_lock, worker_id)
                        if success:
                            expected = payload.stat().st_size
                            remote_path = posixpath.join(config.target_dir.rstrip("/\\"), file_name)
                            ok, reason = verify_remote_size(config, remote_path, expected)
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
                                            check_free_space(config),
                                            f"worker={worker_id}, path={remote_path}",
                                        )
                                    )
                                with cleanup_lock:
                                    wipe_device_storage(config, writer, writer_lock, "verify_failure")
                                success = False
                            else:
                                with writer_lock:
                                    writer.writerow(
                                        record_event(
                                            "verify_success",
                                            "remote size matched",
                                            config,
                                            check_free_space(config),
                                            f"worker={worker_id}, path={remote_path}",
                                        )
                                    )
                        if not success:
                            if "no space" in push_err.lower():
                                with stats_lock:
                                    stats.last_event = "存储已满，执行全量清理"
                                with cleanup_lock:
                                    wipe_device_storage(config, writer, writer_lock, "no_space")
                            elif "timeout" in push_err.lower():
                                with stats_lock:
                                    stats.last_event = "推送超时，执行全量清理并重试"
                                with cleanup_lock:
                                    wipe_device_storage(config, writer, writer_lock, "push_timeout")
                            else:
                                with cleanup_lock:
                                    cleanup_device_storage(config, writer, writer_lock, "push_failure")
                        ui_queue.put({"type": "status", "message": stats.last_event, "stats": stats})
                    finally:
                        task_queue.task_done()
            except Exception as exc:  # noqa: BLE001
                with writer_lock:
                    writer.writerow(record_event("worker_error", str(exc), config, check_free_space(config)))
                with stats_lock:
                    stats.last_event = f"线程 {worker_id} 异常: {exc}"
                ui_queue.put({"type": "status", "message": stats.last_event, "stats": stats})
            finally:
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
            while not stop_event.is_set():
                # 热状况采样
                try:
                    temp_c, source, raw = sample_device_temperature(config)
                    event_type = "thermal_sample"
                    message = f"{source}: {temp_c}C" if temp_c is not None else f"{source}: unknown ({raw})"
                    if temp_c is not None and temp_c >= config.thermal_limit_c:
                        event_type = "thermal_high"
                    with writer_lock:
                        writer.writerow(record_event(event_type, message, config, check_free_space(config), raw))
                except Exception as exc:  # noqa: BLE001
                    with writer_lock:
                        writer.writerow(record_event("health_unknown", str(exc), config, check_free_space(config)))

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
                                check_free_space(config),
                            )
                        )
                    with stats_lock:
                        stats.last_event = "检测到长时间无成功推送，执行全量清空"
                    with cleanup_lock:
                        wipe_device_storage(config, writer, writer_lock, "stall_detected")
                    ui_queue.put({"type": "status", "message": stats.last_event, "stats": stats})

                for _ in range(config.health_check_interval):
                    if stop_event.is_set():
                        break
                    time.sleep(1)

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
            writer.writerow(record_event("complete", "测试完成", config, check_free_space(config)))
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
