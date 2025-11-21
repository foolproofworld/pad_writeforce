# 存储压力测试工具集

本仓库提供两个适用于 Windows 环境、通过 `adb` 驱动的 Python 脚本，用于对安卓平板执行长时间存储压力测试，并生成本地占位文件以配合测试。主脚本支持 7×24 小时多线程连续写入、自动清理空间、实时 CSV 落表和错误记录，并带有 Tk 图形界面显示推送进度、状态与日志，便于检视平板在多次写满场景下的稳定性。

## 文件说明

- `storage_stress.py`：多线程循环推送桌面上的大文件 `pad_test.iso`（默认 2GB 视频/压缩包占位文件）和 1000 个 100KB 文档（`.txt`），记录所有操作与错误到 CSV，并在空间不足或推送失败时自动分批删除旧文件，辅以图形化界面展示实时状态。
- `file_generator.py`：一键在桌面生成上述 `pad_test.iso` 和 1000 个 100KB 文档（默认目录 `~/Desktop/pad_small_files`，命名为 `doc_XXXX.txt`），参数可自定义路径、数量与大小。

## 环境与前置条件

- Windows，需安装 Python 3.10+，并确保 `adb` 已加入 `PATH`。
- 平板已开启 USB 调试，默认假设约 100 GB 可用空间。

## 使用方法

1. **准备 Python 环境（无需额外依赖）：**
   ```powershell
   python -m pip install --upgrade pip
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
   - 脚本会弹出简易 GUI，显示多线程推送成功/失败次数、累计清理次数、累计传输量以及实时事件日志，CSV 落表路径为 `logs/storage_stress_<timestamp>.csv`（UTC 时间）。
   - 默认运行 168 小时，循环推送桌面大文件和 1000 个小文件，当可用空间低于 5 GB 时触发清理，并会在 `adb` 断联时自动尝试等待重连。
   - 可在 `storage_stress.py` 的 `StressTestConfig` 中调整参数（如 `target_dir`、`free_space_threshold_mb`、`cleanup_batch_size`、`reconnect_delay_seconds`、`num_workers` 等）。

## 打包为 Windows 可执行文件

使用 PyInstaller 生成无需 Python 环境的可执行文件：
```powershell
python -m pip install pyinstaller
pyinstaller --onefile storage_stress.py
pyinstaller --onefile file_generator.py
```
生成的文件会出现在 `dist/` 目录下，可直接在目标机器运行。

### 关于「为什么用 Python」以及 exe 启动慢的缓解办法

- **选择 Python 的原因：**
  - 直接复用 `adb` 命令，逻辑清晰，便于在现场快速改参数、排查问题。
  - 依赖极少，脚本本身可读性高，方便后续扩展（如增加更多日志字段或自定义清理策略）。
- **PyInstaller 打包后初次启动卡顿的现象：** `--onefile` 模式会先在临时目录解压依赖，所以第一次运行会有较长的解包时间。
  - 若需要更快启动，可改用 `--onedir` 模式，避免每次解压：
    ```powershell
    pyinstaller --onedir --noconfirm --clean storage_stress.py
    pyinstaller --onedir --noconfirm --clean file_generator.py
    ```
  - 也可以直接使用系统已安装的 Python 解释器运行脚本，跳过打包过程，启动即执行。
  - 追求接近原生的启动速度时，可尝试 `nuitka` 编译（需要 VS Build Tools），通常比 `--onefile` 更快：
    ```powershell
    python -m pip install nuitka
    python -m nuitka --standalone --onefile --mingw64 storage_stress.py
    python -m nuitka --standalone --onefile --mingw64 file_generator.py
    ```
    Nuitka 会生成独立目录（或单文件），首次启动无需解压大体积依赖，冷启动体验更好。

## 实战提示

- 脚本会自动检测 `adb` 设备；如需固定设备，可在 `StressTestConfig` 中设置 `device_id`。
- CSV 日志包含每次推送、清理与错误信息，可实时导入 Excel/BI 做监控；字段包括时间戳、事件类型、消息、剩余空间与附加信息。
- 当空间不足或推送失败时，会按小批量删除最旧文件以腾出空间，确保在多次写满后仍能继续运行。
- 如设备 I/O 较慢，可适当增大 `poll_interval_seconds`，或调低 `num_workers` 以降低瞬时压力；若希望更猛的并发可增大 `num_workers`。
