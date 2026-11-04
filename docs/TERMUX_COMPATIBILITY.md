# TERMUX COMPATIBILITY — TRINITY v3.1

## 1. Trạng thái

| Mục | Trạng thái |
|---|---|
| Máy build | Linux Mint 22.3 x86_64 |
| Thiết bị đích | Redmi 15 5G / Android Termux |
| Chạy trên Termux thật | **CHƯA XÁC MINH** |
| Mô phỏng trên Linux | Đã có test cho detection, fallback RAM, lock, timeout, SQLite |
| Kết luận | **Sẵn sàng cho checklist kiểm thử thiết bị**, không tuyên bố đã chạy thật |

Không được coi `config/termux.yaml` tồn tại là bằng chứng Termux hoạt động.

## 2. Ma trận tương thích

| Thành phần | Cơ chế | Mô phỏng/test | Thiết bị thật |
|---|---|---|---|
| Phát hiện Termux | `PREFIX` chứa `com.termux` hoặc `/data/data/com.termux/files/usr` | PASS (`test_config`) | Chưa |
| Profile Termux | `min_free_ram_mb=192`, disk=150, timeout=600/1800 | PASS (`test_config`) | Chưa |
| RAM | `/proc/meminfo` → `MemFree+Buffers+Cached` → `os.sysconf` | PASS (`test_resource`) | Chưa |
| RAM không đọc được | `available_mb=None`, cảnh báo, không coi là 0 | PASS | Chưa |
| Ổ đĩa | `shutil.disk_usage(workspace)` | PASS | Chưa |
| SQLite | `sqlite3`, WAL best-effort, fallback journal | PASS (temp DB, migration) | Chưa |
| Khóa tuần tự | `fcntl.flock`, non-blocking | PASS trên Linux ext4 | Chưa |
| Filesystem không hỗ trợ lock | **Fail closed** (`ResourceError`) | PASS (mock `OSError`) | Chưa |
| Non-POSIX | **Fail closed** | PASS (mock `os.name`) | Chưa |
| Subprocess | `Popen(argv)`, `start_new_session`, kill process group | PASS (timeout test) | Chưa |
| Signal | `SIGKILL` process group | PASS trên Linux | Chưa |
| Timeout | Wall-clock + `RLIMIT_CPU` best effort | PASS | Chưa |
| Đường dẫn | `expanduser`, resolve, workspace containment | PASS | Chưa |
| Python | 3.9+ | CI 3.9 và 3.12 pass | Chưa |

## 3. Rủi ro riêng của Termux/Android

1. **Shared storage (`/sdcard`, `/storage/emulated/0`) có thể không hỗ trợ `flock`.** Trinity sẽ từ chối chạy. Đặt repo và `~/.trinity` trong bộ nhớ riêng của Termux (`$HOME`), không dùng shared storage.
2. **SQLite WAL trên filesystem lạ.** Code bỏ qua lỗi WAL và dùng journal mặc định; cần xác nhận trên thiết bị.
3. **Quyền file `0600/0700` có thể không được honor trên FAT/exFAT.** Dùng bộ nhớ riêng.
4. **Android có thể kill tiến trình nền.** Trinity chạy on-demand; không có agent nền.
5. **RAM thấp.** Termux profile đặt ngưỡng 192 MB; nếu thiếu, task bị từ chối thay vì OOM.
6. **`termux-setup-storage`** chỉ cần nếu workspace nằm trong shared storage; không khuyến nghị cho DB/lock.
7. **`fcntl` có thể bị hạn chế trên một số kernel/FS.** Đã fail closed; không có fallback chạy không khóa.
8. **Không có Hermes/OpenClaw trên Termux** trừ khi người dùng cài riêng; adapter trả UNAVAILABLE.

## 4. Checklist chạy trên Redmi 15 5G

Chạy trong `$HOME`, không phải `/sdcard`:

```bash
pkg update
pkg install python git
python3 --version
git --version

git clone https://github.com/vukiengiang2025-sys/trinity-v3.git
cd trinity-v3
python3 -m pip install -r requirements.txt

export TRINITY_PROFILE=termux
python3 trinity.py version
python3 trinity.py config
python3 trinity.py doctor
python3 trinity.py status
```

Kỳ vọng:

- `config` hiển thị `profile: termux`, `min_free_ram_mb: 192`.
- `doctor` không có FAIL; Hermes/OpenClaw có thể WARN.
- `status` đọc được RAM và dung lượng ổ đĩa.
- `doctor` kiểm tra được quyền ghi DB/log trong `$HOME`.

Kiểm tra khóa tuần tự:

```bash
# Terminal 1: giữ lock 20 giây
python3 - <<'PY'
from pathlib import Path
from core.config import load_config
from core.resource import ResourceGuard
import time
g = ResourceGuard(load_config("termux"))
lock = g.acquire_lock()
print("lock held")
time.sleep(20)
lock.release()
PY
```

```bash
# Terminal 2: phải fail với thông báo lock, không chạy song song
python3 trinity.py --db "$HOME/.trinity/termux-test.db" run --dry-run "sửa lỗi python"
# dry-run không cần lock; dùng một task thật hoặc test FileLock:
python3 - <<'PY'
from core.config import load_config
from core.resource import ResourceGuard
from core.utils import ResourceError
try:
    ResourceGuard(load_config("termux")).acquire_lock()
    print("BAD: lock acquired twice")
except ResourceError as exc:
    print("OK: fail closed:", exc)
PY
```

Kiểm tra timeout/subprocess:

```bash
python3 -m unittest tests.test_utils.ProcessTests -v
```

Chạy toàn bộ test trên thiết bị:

```bash
python3 -m unittest discover -s tests -t . -v
```

Ghi lại:

- Số test pass/fail.
- `doctor` output.
- Filesystem của repo (`df -T .` nếu có).
- Lock có hoạt động hay fail closed.
- SQLite có tạo được trong `$HOME/.trinity`.
- Thời gian timeout thực tế.

## 5. Tiêu chí PASS trên thiết bị

- `doctor`: 0 FAIL.
- `status`: đọc RAM/disk, hiển thị đúng profile Termux.
- `run --dry-run`: không tạo DB/audit.
- Lock: tiến trình thứ hai bị từ chối hoặc fail closed rõ ràng.
- SQLite tạo/đọc/ghi task trong `$HOME`.
- Timeout kill được process con, không treo.
- Không có ghi ngoài workspace khi chạy task chỉ đọc.
- Không có secrets trong DB/audit.

## 6. Nếu lock fail closed trên thiết bị

Đây là hành vi an toàn, không phải lỗi code. Xử lý:

1. Chuyển repo và `~/.trinity` sang bộ nhớ riêng của Termux (`$HOME`).
2. Kiểm tra `df -T .`; tránh FAT/exFAT/shared storage.
3. Chạy lại `doctor` và test lock.
4. Không tắt kiểm tra lock hoặc thêm fallback chạy không khóa.

Nếu vẫn fail, ghi lại filesystem/kernel và mở issue; không dùng `--force` hoặc bỏ lock.
