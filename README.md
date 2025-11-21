# 存储压力测试工具集

本仓库提供两个适用于 Windows 环境、通过 **MTP 协议** 推送文件的 Python 脚本，用于对安卓平板执行长时间存储压力测试，并生成本地占位文件以配合测试。主脚本支持 7×24 小时多线程连续写入、自动清理空间、实时 CSV 落表和错误记录，并带有 Tk 图形界面显示推送进度、状态与日志，便于检视平板在多次写满场景下的稳定性。

## 技术要点速览

- **多线程流水线**：1 个 producer + 多个 worker 并行推送，随机小文件与定期大文件交错写入，队列容量可调以提升压力。
- **空间与错误自愈**：`df` 多路径检测、超阈值或写满即全量清空；推送失败按错误类型落表并触发互斥清理，确保持续运行。
- **校验与观测**：每次推送后 `stat` 校验远端尺寸，CSV 细分事件（空间/权限/超时/过热/卡死等），GUI 显示并行度、进度条与实时日志。
- **健康监测**：温度采样、长时间无成功推送卡死检测，异常时自动重置测试环境并记录。
- **错误专用日志**：除 CSV 外额外输出 `storage_errors_*.log`，实时汇总所有错误/异常事件，便于快速定位问题。

### 常见启动问题与解决（出现 “name 'detect_device' is not defined” 时）
- **确认脚本未被截断**：请确保你拉取的是最新版本，文件尾部保留 `if __name__ == "__main__": detect_device(cfg); build_ui(cfg)` 入口。如果脚本被拷贝缺行或仍是旧版本，就会报 `name 'detect_device' is not defined`。
- **安装依赖再运行**：新机器先安装 `pymtp`（必须）以及可选的 `adb`：`python -m pip install --upgrade pip pymtp`。若使用虚拟环境，可运行 `python -m venv .venv` 然后 `.\.venv\Scripts\pip install pymtp`。
- **在仓库根目录执行**：切到包含 `storage_stress.py` 的目录运行：`python storage_stress.py`。若在虚拟环境中，则用 `.\.venv\Scripts\python.exe storage_stress.py`。
- **打包后重新构建**：如你在旧版本上打过包，需按下文 “Windows 加速打包与分发” 重新执行 `build_windows.ps1` 或 PyInstaller，确保新的脚本（含 `detect_device`）被打入 exe 中。

## 文件说明

- `storage_stress.py`：多线程循环推送桌面上的大文件 `pad_test.iso`（默认 2GB 视频/压缩包占位文件）和 1000 个 100KB 文档（`.txt`），小文件随机分配到多个线程以模拟多文件同步，并按设定的间隔强制插入大文件写入；每次推送后会通过远端 `stat` 校验文件尺寸，记录所有操作与错误到 CSV，在空间不足或推送失败时自动分批删除旧文件，辅以图形化界面展示实时状态与累计运行时间进度条。
- `file_generator.py`：一键在桌面生成上述 `pad_test.iso` 和 1000 个 100KB 文档（默认目录 `~/Desktop/pad_small_files`，命名为 `doc_XXXX.txt`），参数可自定义路径、数量与大小。
- 额外加入温度/卡死监测：定期采样 thermal/battery 温度，高于阈值或长时间无成功推送时自动全量清空并落表，以暴露更多潜在异常。

## 环境与前置条件

- Windows，需安装 Python 3.10+。请在设备管理器为平板安装 WinUSB/libusb 兼容驱动，使其以 **MTP 设备** 正常枚举（资源管理器能看到存储）。
- 需要 `pymtp` 依赖：`python -m pip install pymtp`。若希望继续采样温度，可额外准备好 `adb`（可选）。
- 平板需打开 MTP 传输功能，默认假设约 100 GB 可用空间。

## 使用方法

1. **准备 Python 环境（安装 MTP 依赖）：**
   ```powershell
   python -m pip install --upgrade pip pymtp
   ```

2. **生成测试文件（必需）：**
   ```powershell
   python file_generator.py
   ```
   - 默认会在桌面生成 `pad_test.iso`（约 2GB，占位视频或压缩包）以及 `pad_small_files/` 下的 1000 个 100KB 文档（`.txt`，包含重复文字内容方便识别）。
   - 如需只生成某一类或调整路径，可用：
     - 仅生成大文件：`python file_generator.py --mode iso --iso-path D:\pad_test.iso --iso-size-gb 2`
     - 仅生成小文件包：`python file_generator.py --mode small --small-dir D:\pad_small_files --small-count 1000 --small-size-kb 100`

3. **运行存储压力测试：**
   ```powershell
   python storage_stress.py
   ```
    - 脚本会弹出简易 GUI，显示多线程推送成功/失败次数、累计清理次数、累计传输量、实时事件日志、活跃线程数，以及累计运行时长进度条（按配置的运行时长 100% 累进）。CSV 落表路径为 `logs/storage_stress_<timestamp>.csv`（UTC 时间），所有错误实时汇总到 `logs/storage_errors_<timestamp>.log` 便于快速定位异常。
   - 默认运行 168 小时，随机将小文件分配给多个线程并周期性插入大文件推送，持续制造并发写入；当可用空间低于 5 GB 时触发清理，MTP 传输失败或超时时会自动重建连接并清空后继续。
  - 可在 `storage_stress.py` 的 `StressTestConfig` 中调整参数（如 `target_dir`、`free_space_threshold_mb`、`cleanup_batch_size`、`reconnect_delay_seconds`、`num_workers` 等）；也可在启动前通过环境变量快速提高并发与队列深度：
    - `STRESS_NUM_WORKERS`：worker 线程数（默认 8，适合强调并发写入压力）
    - `STRESS_LARGE_INTERVAL`：大文件插入间隔（默认 5，越小越频繁写入 2GB 文件）
    - `STRESS_QUEUE_MULT`：任务队列容量系数（默认 8，队列容量 = worker * 系数）
    - `STRESS_TARGET_DIR`：MTP 目标目录（默认 `storage_stress`，如需写到“此电脑\\CPad_8.7\\内部共享存储空间\\Download”，可设为 `Download/storage_stress` 或直接 `Download`）
    - `STRESS_STORAGE_ID`：固定使用的 MTP 存储分区 ID（仅当设备暴露多个分区时需要，例如外置 SD 卡）

## 运行逻辑与并发策略（中文详解）

### 推送顺序与并发模型

- 默认启动 8 个 worker 线程（`num_workers` 可调，亦可用环境变量设置）**再加 1 个 producer 线程**负责持续填充任务队列：
  - producer 以 `large_push_interval` 为节奏强插大文件，空档随机抽取小文件，并为每个任务生成独立时间戳/队列深度前缀，确保大文件在长跑中穿插出现。
  - worker 线程从队列中并行抢占任务，独立执行：检测剩余空间→落表 `push_begin`→MTP 推送→校验/清理→回写状态，无需等待其他线程完成，真正并行发起传输。
  - 并发可视化：
    - GUI 增加 “并行中的推送” 计数（`inflight_pushes`）和活跃线程数，可直观看到是否存在多条推送同时执行。
    - CSV/GUI 的 `worker_start`、`push_begin`、`push_success`/`push_failed` 时间戳可交叉验证同一时间窗口是否有传输重叠。
    - 任务队列容量为 `num_workers * task_queue_multiplier`（默认系数 8），既保证随时有任务可抢，又避免一次性排太多旧时间戳的文件名；必要时可用 `STRESS_QUEUE_MULT` 提升排队深度，扩大瞬时并行机会。
    - MTP 并发说明：每个 worker 都独立创建 MTP 会话并并行发送，但部分固件仍会在设备端串行化写入；若看到时间戳重叠但带宽未叠加，属于设备端 MTP 栈限制。

#### MTP 并行限制与可能的“缓解”思路

- **本质限制**：部分设备的 MTP 协议栈会串行处理同一主机的请求，带宽不一定叠加。
- **可尝试的缓解**（受设备支持程度影响）：
  - 同时保持多条 MTP 连接（脚本已按 worker 独立连接）；如设备仍串行，可考虑并行运行多份脚本绑定不同存储分区/目录，观察是否能分摊到多路控制器。
  - 在可 root 场景下，仍可手动选择 adb/shell 本地写入或挂载不同分区验证并发，但当前脚本专注于 MTP 推送。
  - 留意 USB 线材与端口速率，必要时尝试 USB 3.0 端口或更短线材以减少瓶颈。

### 成功判定与校验机制

- 每次 MTP 发送后会通过远端 metadata 读取文件大小，与本地尺寸严格比对：
  - 匹配则写入 `verify_success` 事件，更新成功计数与累计流量。
  - 不匹配或 `stat` 失败则回滚一次成功计数、增加失败计数，记录 `verify_failed`，并触发一次全量清空以暴露潜在写入异常或文件系统损坏。
- MTP 发送异常（权限、IO、超时等）会记录细分事件（如 `push_nospace`、`push_readonly`、`push_permission`、`push_ioerror`、`push_timeout`），并触发对应的清理：
  - 检测到 “No space left” 时立即整目录清空、重建后继续写入，确保每次写满后都能重新开始。
  - 其他错误则按线程互斥清理，方便下一次尝试继续跑压测并把错误落表。
  - 推送命令超时会记为 `push_timeout` 并立即全量清空，避免因卡死/阻塞而长时间无法写入。

### 空间管理与清理

- 每轮都会调用 `df -k` 同时查询目标目录、`/sdcard`、`/storage/emulated/0`，最大程度拿到可用空间数值；解析失败会落表 `free_space_unknown` 便于排查 df 输出异常。
- 当剩余空间低于 `free_space_threshold_mb`（默认 5 GB）或推送失败提示空间耗尽时，直接执行全量清空（`wipe_all`），随后重建目标目录，保证在多次写满后也能立刻恢复写入。常规错误则保持原有批量删除（`cleanup_deleted`）策略。
- 清理/清空过程均会记录成功或失败事件（`cleanup_failed`、`cleanup_error`、`wipe_all_error`），避免单次异常导致测试停机且便于追踪。

### 设备连接与重试

- 启动时脚本会用 `pymtp` 连接首个 MTP 存储并创建目标目录；每个 worker 独立持有一个连接，发送异常或超时会自动重建连接后继续。
- 若设备在连接初期未返回存储列表（`get_storage` 返回 `None`），脚本会自动视为无可用存储并提示检查 MTP 连接，避免再次出现“argument of type 'NoneType' is not iterable”类错误。
- 若 MTP 返回错误/不可达，会记录 `device_retry`/`device_missing`，并在清理后重建目标目录再继续压测，测试不中断。

### 设备健康/卡死监测

- 温度采样：独立线程每 `health_check_interval` 秒读取 `/sys/class/thermal/thermal_zone*/temp` 或 `dumpsys battery` 温度；成功时写入 `thermal_sample`，高于 `thermal_limit_c`（默认 65℃）记为 `thermal_high`，帮助关联“过热导致中断/降速”类问题。
- 卡死/停滞检测：若连续 `stall_timeout_minutes` 分钟（默认 10 分钟）无任何成功推送，会写入 `stall_detected` 并触发一次全量清空重置环境，避免长时间卡死掩盖错误。

### 日志与可观测性

- 所有关键事件写入 CSV（UTC 时间），字段：`timestamp`、`event`、`message`、`free_space_mb`、`extra`；典型事件值包含 `start`、`push_success`、`verify_failed`、`push_nospace`、`push_readonly`、`push_timeout`、`thermal_high`、`stall_detected`、`free_space_unknown`、`wipe_all`、`device_retry`、`worker_error`、`complete` 等，可直接按事件过滤定位“空间不足/推送超时/过热/卡死”等问题来源。
- 错误专用日志：`logs/storage_errors_<timestamp>.log` 以秒级追加方式汇总推送失败、校验失败、清理异常、设备离线、温度过高、卡死触发等错误，便于直接打开文本快速查看异常原因。
- GUI 实时拉取状态：
  - 文本状态栏显示最近事件描述（线程编号、校验结果等）。
  - 统计栏展示推送成功/失败、清理次数、累计流量。
  - 「累计运行时长」进度条按配置的 `run_hours` 计算总秒数，持续累进，直观观察长跑测试覆盖度。
  - 日志滚动窗口附加所有状态消息，便于现场观察和截图留存。

## 打包为 Windows 可执行文件（启动加速版）

以下方法均在 Windows 10/11、64 位 Python 上验证。为避免 `--onefile` 解压导致的“首启卡顿”，推荐使用 `--onedir` 模式。

### 方式 1：脚本一键打包（推荐）
1. PowerShell 进入仓库根目录，执行：
   ```powershell
   .\build_windows.ps1
   ```
   - 首次运行会自动创建 `.venv` 并安装 PyInstaller。
   - 生成的目录：`dist/storage_stress/` 和 `dist/file_generator/`，连同目录整体复制到新电脑即可运行。

2. 新电脑上直接运行 `dist/storage_stress/storage_stress.exe` 或 `dist/file_generator/file_generator.exe`，无需 Python。`--onedir` 不会在启动时解压大包，冷启动更快。

### 方式 2：手动命令
1. 安装依赖：
   ```powershell
   python -m pip install --upgrade pip pyinstaller
   ```

2. 在仓库根目录执行：
   ```powershell
   pyinstaller --noconfirm --onedir --clean --name storage_stress storage_stress.py
   pyinstaller --noconfirm --onedir --clean --name file_generator file_generator.py
   ```

### 启动/编译加速技巧与排查
- **首启卡顿**：避免 `--onefile`；给 `dist/` 目录加杀毒/Defender 例外；确保从本地盘运行。
- **构建变慢**：复用 `.venv`，脚本会跳过重复安装；必要时删除 `.venv`、`build/`、`dist/` 重新打包。
- **缺少 Tk/GUI**：请使用官方 64 位 Python，安装时勾选 “tcl/tk and IDLE”；若缺少可 `python -m pip install tk` 后重试。
- **杀软误报**：将生成的 EXE 加入白名单或签名，脚本仅依赖标准库和 ADB。

### 高级：更快的可执行文件
若希望更接近原生启动速度，可使用 Nuitka（需要 VS Build Tools/C 编译器）：
```powershell
python -m pip install nuitka
nuitka --onefile --windows-console-mode=disable --follow-imports storage_stress.py
nuitka --onefile --windows-console-mode=disable --follow-imports file_generator.py
```
Nuitka 打包时间更长，但运行时启动更快；适合在稳定环境复用产物。

## 实战提示

- 脚本会自动连接首个 MTP 存储；如需固定分区，可在 `StressTestConfig` 中设置 `mtp_storage_id` 或调整 `target_dir`。
- CSV 日志包含每次推送、清理与错误信息，并记录远端尺寸校验是否通过，可实时导入 Excel/BI 做监控；字段包括时间戳、事件类型、消息、剩余空间与附加信息。
- 当空间不足或推送失败提示磁盘写满时，会整目录清空后重建；其他错误仍按小批量删除最新文件释放空间，确保多线程不会互相踩踏并能继续运行。
- 建议在 CSV 中按 `event` 字段筛选（如 `push_timeout`、`push_nospace`、`thermal_high`、`stall_detected`），可快速定位错误来源、复盘是否与过热/卡死/空间不足相关。
- 如设备 I/O 较慢，可适当增大 `poll_interval_seconds`，或调低 `num_workers` 以降低瞬时压力；若希望更猛的并发可增大 `num_workers`、`task_queue_multiplier`，并缩小 `large_push_interval`（或通过环境变量设置）。
