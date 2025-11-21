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
    poll_interval_seconds: int = 5
    cleanup_batch_size: int = 5
    reconnect_delay_seconds: int = 10
    num_workers: int = 3
    large_push_interval: int = 10


@dataclass
class StressStats:
    pushes: int = 0
    success: int = 0
    failures: int = 0
    cleanups: int = 0
    bytes_sent: int = 0
    last_event: str = ""


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


def parse_free_space_mb(df_output: str, mount_path: str) -> Optional[int]:
    for line in df_output.splitlines():
        if mount_path in line:
            parts = line.split()
            if len(parts) >= 4:
                try:
                    available_kb = int(parts[3])
                    return available_kb // 1024
                except ValueError:
                    return None
    return None


def check_free_space(config: StressTestConfig) -> Optional[int]:
    code, out, err = run_adb_command(config, ["shell", "df", "-k", config.target_dir])
    if code != 0:
        return None
    return parse_free_space_mb(out, config.target_dir)


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
) -> bool:
    remote_path = posixpath.join(config.target_dir.rstrip("/\\"), remote_name)
    code, out, err = run_adb_command(config, ["push", str(local_path), remote_path], timeout=config.push_timeout)
    with stats_lock:
        stats.pushes += 1
    if code == 0:
        size = local_path.stat().st_size
        with stats_lock:
            stats.success += 1
            stats.bytes_sent += size
            stats.last_event = f"[线程{worker_id}] 推送成功: {remote_path}"
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
        return True
    with stats_lock:
        stats.failures += 1
        stats.last_event = f"[线程{worker_id}] 推送失败: {err or out}"
    with writer_lock:
        csv_writer.writerow(
            record_event(
                "push_failed",
                err or out,
                config,
                check_free_space(config),
                f"worker={worker_id}, path={remote_path}",
            )
        )
    return False


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
    stats_lock = threading.Lock()
    writer_lock = threading.Lock()
    cleanup_lock = threading.Lock()
    payload_lock = threading.Lock()

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

        def next_payload() -> Path:
            nonlocal pushes_since_large
            with payload_lock:
                if pushes_since_large >= config.large_push_interval:
                    pushes_since_large = 0
                    return large_payload
                pushes_since_large += 1
            return choice(small_payloads)

        def worker(worker_id: int) -> None:
            try:
                while not stop_event.is_set():
                    if datetime.utcnow() >= end_time:
                        stop_event.set()
                        break

                    free_space = check_free_space(config)
                    if free_space is not None and free_space < config.free_space_threshold_mb:
                        with stats_lock:
                            stats.cleanups += 1
                            stats.last_event = "剩余空间不足，执行清理"
                        with writer_lock:
                            writer.writerow(
                                record_event("cleanup_needed", "剩余空间低于阈值，开始清理", config, free_space)
                            )
                        with cleanup_lock:
                            cleanup_device_storage(config, writer, writer_lock, "low_free_space")
                        ui_queue.put({"type": "status", "message": stats.last_event, "stats": stats})
                        time.sleep(config.poll_interval_seconds)
                        continue

                    if not ensure_device_ready(config, writer, writer_lock, stats, stats_lock):
                        ui_queue.put({"type": "status", "message": "等待设备重连失败，稍后重试", "stats": stats})
                        time.sleep(config.reconnect_delay_seconds)
                        continue

                    payload = next_payload()
                    file_name = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_w{worker_id}_{payload.name}"

                    success = push_file(config, payload, file_name, writer, writer_lock, stats, stats_lock, worker_id)
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
                        with cleanup_lock:
                            cleanup_device_storage(config, writer, writer_lock, "push_failure")
                    ui_queue.put({"type": "status", "message": stats.last_event, "stats": stats})
                    time.sleep(config.poll_interval_seconds)
            except Exception as exc:  # noqa: BLE001
                with writer_lock:
                    writer.writerow(record_event("worker_error", str(exc), config, check_free_space(config)))
                with stats_lock:
                    stats.last_event = f"线程 {worker_id} 异常: {exc}"
                ui_queue.put({"type": "status", "message": stats.last_event, "stats": stats})

        threads: List[threading.Thread] = []
        for idx in range(config.num_workers):
            t = threading.Thread(target=worker, args=(idx,), daemon=True)
            threads.append(t)
            t.start()

        while not stop_event.is_set():
            if datetime.utcnow() >= end_time:
                stop_event.set()
                break
            time.sleep(1)

        for t in threads:
            t.join(timeout=5)

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

    tk.Label(root, textvariable=status_var, anchor="w").pack(fill="x", padx=10, pady=5)
    tk.Label(root, textvariable=progress_var, anchor="w").pack(fill="x", padx=10, pady=5)
    tk.Label(root, textvariable=cleanup_var, anchor="w").pack(fill="x", padx=10, pady=5)
    tk.Label(root, textvariable=bytes_var, anchor="w").pack(fill="x", padx=10, pady=5)
    tk.Label(root, textvariable=time_var, anchor="w").pack(fill="x", padx=10, pady=5)
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
        detect_device(cfg)
        build_ui(cfg)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"Fatal error: {exc}\n")
        sys.exit(1)
