# TRINITY AUTONOMOUS AUDIT — v3.1

- **Ngày:** 2026-11-04
- **Repository:** `vukiengiang2025-sys/trinity-v3`
- **Branch:** `main`
- **HEAD khi bắt đầu audit:** `9f8ba37`
- **HEAD code hoàn tất P0:** `2a56726`
- **Phạm vi:** P0 security/recovery, P1 Termux readiness, adapter status, test/CI, tài liệu.
- **Nguyên tắc:** không mở quyền agent, không sửa global OpenCode config, không cài Hermes/OpenClaw, không force push.

## 1. Trạng thái ban đầu

| Mục | Giá trị |
|---|---|
| Git | `main`, working tree sạch, remote khớp |
| Test local | 167 PASS (Python 3.9.25 và 3.12) |
| CI gần nhất đã xanh | 153 test |
| CI mới | PASS (174 test) |
| OpenCode | v2.2.5, CLI AVAILABLE |
| Hermes | UNAVAILABLE |
| OpenClaw | UNAVAILABLE |
| Termux | chưa chạy trên thiết bị |
| Agent production | chưa bật; chỉ `trinity-readonly` |

## 2. Phát hiện và xử lý

### 2.1 CI

- Các run cũ từng queued; run `41866867803` (commit `2a56726`, 174 test) đã **PASS** cả Python 3.9/3.12.
- GitHub Actions API báo Actions operational; không có runner được cấp.
- Không sửa code để “làm CI xanh”; không tuyên bố PASS cho 174 test trên CI.
- Đã xác minh local bằng Python 3.9.25 (`uv`) và Python 3.12: **174 test PASS**.

### 2.2 Termux

Thiết bị thực tế:

```text
adb devices  -> ed2ec4c device
model        -> 25057RN09G (Xiaomi)
Android      -> 16
Termux       -> com.termux 0.118.3
run-as       -> package not debuggable
su           -> not found
RUN_COMMAND  -> Requires com.termux.permission.RUN_COMMAND
lockscreen   -> mDreamingLockscreen=true
```

Kết luận: **BLOCKED** — thiết bị kết nối nhưng khóa màn hình; không thể vào Termux hoặc chạy self-test từ ADB mà không mở khóa/cấp quyền. Không grant quyền `RUN_COMMAND` tự động.

Đã tạo:

- `scripts/termux_self_test.py` — self-test một lệnh, không chạy agent.
- `make termux-self-test`.
- `tests/test_termux_self_test.py`.
- Hướng dẫn trong `docs/TERMUX_COMPATIBILITY.md`.

### 2.3 Recovery và lease

Đã bổ sung runner lease/heartbeat (commit `46c3126`):

- `runner_id`, `runner_pid`, `runner_host`, `heartbeat_at` trong SQLite.
- Heartbeat mỗi 5 giây khi task chạy.
- Recovery chỉ đánh dấu `RUNNING → FAILED` khi:
  - lock acquire được; và
  - heartbeat cũ hơn 60 giây hoặc chưa có.
- Heartbeat mới hoặc lock bận → không đụng task.
- Không tự retry task đã recovery.
- `PR_SET_PDEATHSIG` best-effort cho direct child.

Residual:

- Detached grandchildren/server có thể sống sót.
- Recovery không phải process supervisor.
- Xóa lock file khi đang chạy có thể phá mutual exclusion.
- Lease 60 giây có thể trì hoãn recovery ngay sau crash.

### 2.4 Baseline và leakage

- Baseline fixture `tests/fixtures/opencode_readonly/` có SHA-256, approval metadata, deny-rule mutation test.
- `APPROVAL.md` quy định cập nhật baseline phải review + approval + commit mới.
- Redaction đã kiểm tra prompt/output/exception/audit/env; DB/audit thật không có `/etc/passwd` hoặc token mẫu.
- Redaction **không** bảo đảm tuyệt đối với secret trong prompt/config, tên biến lạ, traceback trước redaction hoặc nội dung ngoài workspace.
- Baseline approval chưa có chữ ký mật mã; người có quyền sửa cả config/hash/metadata vẫn có thể vượt qua.

### 2.5 OpenCode

- `trinity-readonly` đã LIVE VERIFIED read-only trong sandbox.
- 3 run durability COMPLETED; workspace/Git không đổi.
- Post-lease live run `TRN-20261104-162444-0c3708` COMPLETED; runner lease/heartbeat được ghi nhận, workspace/Git không đổi.
- Agent không tồn tại, timeout, JSON rỗng/sai, error event exit 0 đều FAILED.
- `standalone: true` và PWD/cwd được sửa.
- Không chạy agent production.

### 2.6 Hermes/OpenClaw

- `CODE VERIFIED` + `MOCK VERIFIED` ở mức unit test.
- `LIVE VERIFIED`: **không**, vì CLI/gateway không tồn tại.
- Không tự cài.

## 3. Test

| Hạng mục | Kết quả |
|---|---|
| Full suite local Python 3.12 | **174 PASS** |
| Full suite local Python 3.9.25 | **174 PASS** |
| Failure/Error | 0 / 0 |
| `termux_self_test.py --quick` trên Linux | **9/9 PASS** |
| Security/recovery/baseline tests | PASS |
| CI 174 test | PASS |
| CI 174 test | **PASS** — run 41866867803 |

## 4. File thay đổi trong audit này

- `scripts/termux_self_test.py` (mới)
- `tests/test_termux_self_test.py` (mới)
- `Makefile`
- `README.md`
- `docs/TERMUX_COMPATIBILITY.md`
- `docs/ENVIRONMENT_REPORT.md`
- `docs/TEST_REPORT.md`
- `docs/OPENCODE_SANDBOX.md`
- `tests/fixtures/opencode_readonly/baseline.json`
- report này

## 5. Git/CI

- HEAD trước commit report: `9f8ba37`.
- Không force push, không reset/clean/amend.
- CI run 174 test: https://github.com/vukiengiang2025-sys/trinity-v3/actions/runs/41866867803
- Các commit mốc `b0ecdc5`, `bcd562b`, `680886d`, `972ce0f`, `fbedee7`, `46c3126`, `9f8ba37` được giữ nguyên.

## 6. Hướng dẫn chạy trên Android/Termux

```bash
pkg update
pkg install python git
git clone https://github.com/vukiengiang2025-sys/trinity-v3.git
cd trinity-v3
python3 -m pip install -r requirements.txt

export TRINITY_PROFILE=termux
python3 trinity.py doctor
python3 trinity.py status

python3 scripts/termux_self_test.py --quick
# hoặc đầy đủ:
python3 scripts/termux_self_test.py
```

Gửi lại `logs/termux-self-test-*.txt` để đối chiếu.

## 7. Kết luận

- P0 security/recovery: **PASS local**, có lease/heartbeat, baseline approval, leakage test.
- OpenCode read-only: **LIVE VERIFIED**.
- Termux: **BLOCKED** do thiết bị khóa/không có quyền chạy; self-test đã sẵn sàng.
- Hermes/OpenClaw: **UNAVAILABLE**.
- CI 174: **PASS** (3.9/3.12).
- Chưa bật `build/general`; chưa cho Trinity tự sửa repository.
