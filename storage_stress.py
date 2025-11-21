import csv
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple


@dataclass
class StressTestConfig:
    device_id: Optional[str] = None
    target_dir: str = "/sdcard/storage_stress"
    local_log_dir: str = "logs"
    file_sizes_mb: List[int] = field(default_factory=lambda: [100, 500, 1024])
    free_space_threshold_mb: int = 5_000
    run_hours: int = 24 * 7
    push_timeout: int = 600
    poll_interval_seconds: int = 5
    cleanup_batch_size: int = 5


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


def cleanup_device_storage(config: StressTestConfig, csv_writer: csv.DictWriter, reason: str) -> None:
    try:
        files = list_device_files(config)
    except Exception as exc:  # noqa: BLE001
        csv_writer.writerow(record_event("cleanup_failed", str(exc), config, None, reason))
        return

    removed = 0
    for path in files:
        if removed >= config.cleanup_batch_size:
            break
        code, _, err = run_adb_command(config, ["shell", "rm", "-f", path])
        if code == 0:
            removed += 1
            csv_writer.writerow(record_event("cleanup_deleted", "", config, None, path))
        else:
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


def generate_temp_file(size_mb: int) -> str:
    fd, path = tempfile.mkstemp(suffix=".bin")
    os.close(fd)
    with open(path, "wb") as handle:
        handle.truncate(size_mb * 1024 * 1024)
    return path


def push_file(config: StressTestConfig, local_path: str, remote_name: str, csv_writer: csv.DictWriter) -> bool:
    remote_path = os.path.join(config.target_dir, remote_name)
    code, out, err = run_adb_command(
        config, ["push", local_path, remote_path], timeout=config.push_timeout
    )
    if code == 0:
        csv_writer.writerow(record_event("push_success", out, config, check_free_space(config), remote_path))
        return True
    csv_writer.writerow(record_event("push_failed", err or out, config, check_free_space(config), remote_path))
    return False


def detect_device(config: StressTestConfig) -> None:
    code, out, err = run_adb_command(config, ["devices"])
    if code != 0:
        raise RuntimeError(f"adb not working: {err}")
    lines = [line for line in out.splitlines() if "\tdevice" in line]
    if not lines:
        raise RuntimeError("No Android devices detected via adb")
    if config.device_id:
        if not any(line.startswith(config.device_id) for line in lines):
            raise RuntimeError(f"Device {config.device_id} not found in adb devices list")
    else:
        config.device_id = lines[0].split("\t")[0]


def run_stress_test(config: StressTestConfig) -> None:
    ensure_local_logs(config)
    ensure_target_dir(config)
    log_path = os.path.join(config.local_log_dir, f"storage_stress_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv")

    with open(log_path, "w", newline="", encoding="utf-8") as csvfile:
        fieldnames = ["timestamp", "event", "message", "free_space_mb", "extra"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        end_time = datetime.utcnow() + timedelta(hours=config.run_hours)
        writer.writerow(record_event("start", "test started", config, check_free_space(config)))

        while datetime.utcnow() < end_time:
            free_space = check_free_space(config)
            if free_space is not None and free_space < config.free_space_threshold_mb:
                writer.writerow(record_event("cleanup_needed", "free space below threshold", config, free_space))
                cleanup_device_storage(config, writer, "low_free_space")
                time.sleep(config.poll_interval_seconds)
                continue

            size_mb = random.choice(config.file_sizes_mb)
            temp_path = generate_temp_file(size_mb)
            file_name = datetime.utcnow().strftime("%Y%m%d_%H%M%S") + "_" + "".join(random.choices(string.ascii_lowercase, k=6)) + ".bin"

            try:
                success = push_file(config, temp_path, file_name, writer)
                if not success:
                    cleanup_device_storage(config, writer, "push_failure")
            finally:
                os.remove(temp_path)

            time.sleep(config.poll_interval_seconds)

        writer.writerow(record_event("complete", "test finished", config, check_free_space(config)))


if __name__ == "__main__":
    try:
        cfg = StressTestConfig()
        detect_device(cfg)
        run_stress_test(cfg)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"Fatal error: {exc}\n")
        sys.exit(1)
