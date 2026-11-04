# ENVIRONMENT REPORT — TRINITY v3.1

- **Ngày kiểm tra:** 2026-11-04
- **Mục đích:** xác minh môi trường thật trước khi triển khai, không suy luận từ sự tồn tại của executable.
- **Phương pháp:** lệnh hệ thống, `--version`, health check HTTP loopback, kiểm tra đường dẫn; không đọc/ghi secrets.

## 1. Hệ điều hành và phần cứng

| Mục | Giá trị | Trạng thái |
|---|---|---|
| OS | Linux Mint 22.3 (Zena) | AVAILABLE |
| Kernel | Linux 7.0.0-42-generic | AVAILABLE |
| Kiến trúc | x86_64 | AVAILABLE |
| Termux | `PREFIX` rỗng; `/data/data/com.termux` không tồn tại | UNAVAILABLE (không phải Termux) |
| Thiết bị Android | Xiaomi `25057RN09G`, Android 16; Termux 0.118.3; ADB `ed2ec4c`; màn hình khóa | **BLOCKED** (không chạy được self-test) |
| CPU/RAM | 3.776 MB total, ~1.717 MB available tại thời điểm test, swap 22.033 MB | AVAILABLE |
| Ổ đĩa `/home` | 122 GB, còn 59 GB (~50%) | AVAILABLE |
| Network | github.com và api.github.com trả HTTP 200 | AVAILABLE |

## 2. Python và dependency

| Mục | Giá trị | Trạng thái |
|---|---|---|
| Python | 3.12.3 | AVAILABLE |
| PyYAML | 6.0.3 | AVAILABLE |
| pytest | Không cài trong môi trường hệ thống | Không cần — test dùng `unittest` |
| Virtualenv | Không bắt buộc cho lần build này | — |

`requirements.txt` chỉ chứa `PyYAML`. Toàn bộ thành phần còn lại dùng Python Standard Library (`sqlite3`, `subprocess`, `argparse`, `fcntl`, `urllib`, `unittest`).

## 3. Git và GitHub

| Mục | Giá trị | Trạng thái |
|---|---|---|
| Git | 2.43.0 | AVAILABLE |
| GitHub CLI (`gh`) | Không tìm thấy | UNAVAILABLE |
| Git credential helper | `store` (đã cấu hình sẵn) | AVAILABLE; chỉ dùng qua helper cho API/push, không hiển thị/ghi log/commit token |
| GitHub API | Truy cập được, tạo repo thành công | AVAILABLE |
| Repository đích | `vukiengiang2025-sys/trinity-v3` | Đã tạo, private, branch `main` |

Ghi chú: credential chỉ được đọc transient qua Git credential helper để gọi API/push; không được in, không ghi log, không đưa vào commit. Repo được tạo qua GitHub API bằng credential helper sẵn có.

## 4. OpenCode — LOGIC

| Mục | Giá trị | Trạng thái |
|---|---|---|
| Executable | `/home/vukgg04/.npm-global/bin/opencode` | AVAILABLE |
| Version | `opencode v2.2.5` | AVAILABLE |
| `opencode run --help` | Exit 0, hỗ trợ `--format default/json`, `--file`, `--model`, `--agent` | AVAILABLE |
| Server đang chạy | `opencode service status` → `http://127.0.0.1:49374` | AVAILABLE (health only) |
| `GET /` | HTTP 200 | AVAILABLE |
| `GET /health` | HTTP 200 | AVAILABLE |
| `GET /global/health` | HTTP 200 | AVAILABLE |
| `GET /api/health` | HTTP 401 | Cần auth — chưa xác minh |
| `GET /openapi.json` | HTTP 401 | Cần auth — chưa xác minh |
| HTTP execution endpoint | Chưa xác minh compatibility/auth | **UNVERIFIED** |
| Actual prompt run | Đã chạy read-only với agent project-local `trinity-readonly`; task `TRN-20261104-152925-1c79c9` COMPLETED; workspace/Git không đổi | **READ-ONLY VERIFIED** |
| Agent production (`build`/`general`) | Chưa chạy; quyền rộng | **NOT VERIFIED** |
| Permission audit | `opencode debug agents`: `build`/`general` wildcard allow; `plan` edit-deny nhưng shell qua wildcard; `architect` edit-deny, shell Git read-only, read/webfetch | **UNVERIFIED for unattended use** |

Quyết định: adapter dùng CLI subprocess (`mode: cli`), không dùng HTTP execution. Không sửa `opencode.json/jsonc`, provider, model, MCP, plugin hoặc skills. Không truyền `--auto`. v3.1 bắt buộc `adapters.opencode.agent` tường minh; thiếu agent → `DENIED`.

## 5. Hermes — BRAIN

| Mục | Giá trị | Trạng thái |
|---|---|---|
| `command -v hermes` | Không tìm thấy | UNAVAILABLE |
| `~/.hermes` | Không tồn tại | Không có memory path đã xác minh |
| CLI/profile | Không xác minh được | UNAVAILABLE |
| Memory path | Không mặc định `~/.hermes/memories/` | Chưa xác định |

Adapter trả `UNAVAILABLE`; không tạo, không ghi đè memory directory. Command template memory để trống cho đến khi operator xác minh CLI thật.

## 6. OpenClaw — MUSCLE

| Mục | Giá trị | Trạng thái |
|---|---|---|
| `command -v openclaw` | Không tìm thấy | UNAVAILABLE |
| `~/.cache/openclaw` | Tồn tại nhưng rỗng | Không có cài đặt đã xác minh |
| Gateway | Không khởi động, không probe vì `gateway_enabled: false` | UNAVAILABLE |
| CLI/version/help | Không xác minh được | UNAVAILABLE |

Adapter trả `UNAVAILABLE`; không tự khởi động gateway, không cấp quyền hệ thống, không tự sinh shell command.

## 7. Kết quả kiểm thử môi trường

| Kiểm tra | Kết quả |
|---|---|
| `python3 -m compileall` | PASS |
| `python3 trinity.py version` | PASS — `Trinity v3.1.0` |
| `python3 trinity.py config` | PASS — profile linux, không secrets |
| `python3 trinity.py --profile termux config` | PASS — profile termux, ngưỡng RAM 192 MB |
| `python3 trinity.py status` | PASS — hiển thị 3 adapter |
| `python3 trinity.py doctor` | PASS — 0 FAIL, 3 WARN (Hermes/OpenClaw unavailable; OpenCode chưa cấu hình agent) |
| `python3 trinity.py run --dry-run "..."` | PASS — không tạo DB/audit |
| Unit/integration tests v3.1 | PASS — 185 test, 0 failure, 0 error |
| GitHub Actions | PASS — Python 3.9 và 3.12 (v3.0 và v3.1) |

## 8. Phân loại tổng hợp

**AVAILABLE**

- Linux Mint host, Python 3.12.3, PyYAML, Git, GitHub API.
- OpenCode CLI v2.2.5 (`--version`, `run --help`) và health endpoint loopback.

**INSTALLED_BUT_UNVERIFIED**

- Không có mục nào thuộc nhóm này tại thời điểm build. OpenCode HTTP health sống nhưng endpoint thực thi không được xếp vào AVAILABLE.

**UNAVAILABLE**

- Hermes CLI, Hermes memory path.
- OpenClaw CLI, OpenClaw gateway.
- Termux runtime (host hiện tại là Linux Mint).
- GitHub CLI (`gh`) — không cần cho build vì dùng API/credential helper.

## 9. Cập nhật kiểm định v3.1

- CI v3.1: **success** cho cả `unittest (3.9)` và `unittest (3.12)` với 153 test (run 41860781598). CI 174 test PASS trên Python 3.9/3.12 (run 41866867803); local 185 test PASS trên Python 3.9.25/3.12; CI 185 queued.
- Bộ test v3.1: **185 test**, 0 failure, 0 error.
- Lock `fcntl`: phát hiện khả năng fail-open; đã sửa thành **fail closed** khi thiếu fcntl/non-POSIX/filesystem không hỗ trợ.
- Task resume: đã triển khai cùng Task ID, plan JSON, approval actor/time/scope, atomic claim.
- OpenCode: mặc định không dùng agent built-in; thiếu `agent` cấu hình → `DENIED`.
- OpenCode read-only thật: agent `trinity-readonly` (edit/bash/network/external deny); task COMPLETED; `/etc/passwd` bị deny; workspace/Git không đổi. Xem `docs/OPENCODE_SANDBOX.md`.
- Durability: 3/3 read-only run thật COMPLETED; agent không tồn tại và timeout 1s đều FAILED, workspace/Git không đổi.
- Frozen regression baseline: `tests/fixtures/opencode_readonly/` + hash/config rules + approval metadata.
- Crash recovery: orphan `RUNNING` task được đánh dấu `FAILED` khi lock free; CLI test đã chạy.
- Leakage audit: env secret redaction, DB/audit scan không có `/etc/passwd`/token mẫu; exception redaction test.
- Phát hiện và sửa `PWD` không đồng bộ `cwd`; thêm `standalone: true` để tránh service/cache UI.
- Termux: mới kiểm thử mô phỏng, chưa chạy trên Redmi 15 5G.

## 10. Giới hạn của báo cáo

- Không kiểm chứng được hành vi thực tế của Hermes/OpenClaw vì công cụ không tồn tại trên máy.
- Chỉ chạy prompt thật trong sandbox `/tmp` với agent `trinity-readonly`; không chạy agent production. Kết quả chi tiết: `docs/OPENCODE_SANDBOX.md`.
- Health endpoint 200 không chứng minh endpoint thực thi tương thích hoặc đã được cấp quyền.
- Môi trường Termux không có sẵn để kiểm thử trên thiết bị thật; profile Termux được kiểm thử ở mức cấu hình/logic.
