# TEST REPORT — TRINITY v3.1

- **Ngày:** 2026-11-04
- **Môi trường:** Linux Mint 22.3, Python 3.12.3, OpenCode v2.2.5
- **Lệnh:** `python3 -m unittest discover -s tests -t . -v`
- **Kết quả:** **165 test — 0 failure — 0 error — OK**
- **Trước v3.1:** 115 test
- **Sau v3.1:** 165 test (+50)
- **CI:** v3.1 (153 test) PASS; bộ 165 test chờ push
- **Không dùng pytest**: test chạy bằng Standard Library `unittest`.

## 1. Tổng hợp theo module

| File | Số test | Nội dung |
|---|---:|---|
| `test_config.py` | 9 | Profile Linux/Termux, auto-detect, profile sai, allowed roots, mask secrets |
| `test_router.py` | 13 | Phân loại, risk, plan, Task ID, negation-aware routing |
| `test_guard.py` | 20 | Path, symlink, file nhạy cảm, command allowlist, Git safety, injection |
| `test_resource.py` | 11 | RAM đọc được/không đọc được, sysconf fallback, RAM/disk thấp, lock, timeout |
| `test_task.py` | 12 | Task lifecycle, transition, event, permission, missing DB, redaction, audit |
| `test_adapters.py` | 27 | Missing tool, fake CLI, timeout, JSON empty/invalid/error event, permission, OpenClaw/Hermes, default-agent deny, standalone |
| `test_opencode_baseline.py` | 9 | Frozen config hash, deny rules, durability records, failure modes, no secrets |
| `test_executor.py` | 13 | Tuần tự, approval, fail-stop, resource/lock failure, redaction |
| `test_resume.py` | 16 | Resume cùng Task ID, approval record, atomic claim, mismatch, migration, lock fail-closed |
| `test_cli.py` | 18 | version/status/config/tasks/task/run/resume, dry-run, exit code |
| `test_utils.py` | 11 | Redaction, truncate, mask mapping, subprocess timeout, PWD/cwd consistency |
| `test_recovery.py` | 6 | Orphan RUNNING → FAILED, lock busy, idempotent, dry-run, CLI recovery |
| **Tổng** | **165** | |

## 2. CI

| Job | Kết quả |
|---|---|
| `unittest (3.9)` | **success** (v3.1, 153 test) |
| `unittest (3.12)` | **success** (v3.1, 153 test) |

Run 153 test: <https://github.com/vukiengiang2025-sys/trinity-v3/actions/runs/41860781598>

## 3. OpenCode Adapter — kiểm chứng end-to-end

### 3.1 Read-only sandbox

Đã tạo **project-local** agent `trinity-readonly` trong `/tmp/trinity-opencode-sandbox` (không sửa global config):

- deny: `edit`, `bash`, `webfetch`, `websearch`, `external_directory`, `task`, `skill`, `todowrite`, `question`.
- allow: `read`, `glob`, `grep`, `list` trong workspace.
- deny đọc trực tiếp `/etc`, `/home`, `/root`, `/proc`, `/sys`, `*.env`.

Kết quả test permission trực tiếp:

| Hành động | Kết quả |
|---|---|
| Đọc `sample.py` | **THÀNH CÔNG** |
| Edit/tạo file | **KHÔNG CÓ TOOL** (bị permission ẩn) |
| Bash `touch` | **KHÔNG CÓ TOOL** |
| Git commit | **KHÔNG CÓ SHELL** |
| Đọc `/etc/passwd` | **DENIED** — `permission.rejected / external_directory` |
| Workspace/Git sau test | Không đổi |

### 3.2 Trinity Adapter thật

Cấu hình Executor tạm trỏ workspace sandbox, `agent=trinity-readonly`, `standalone=true`.

| Mục | Kết quả |
|---|---|
| Task ID | `TRN-20261104-152925-1c79c9` |
| Status | **COMPLETED** |
| Adapter | `opencode` |
| Result | 4.644 ký tự JSON output |
| Nội dung | Tóm tắt đúng `add(a, b)` |
| Workspace/Git | Không đổi |
| Audit | `task_started` → `adapter_finished SUCCESS` → `task_completed` |

Đã phát hiện và sửa lỗi thực tế:

- **`PWD` không đồng bộ `cwd`** → OpenCode đọc nhầm project config, báo `Agent not found`. Sửa trong `run_process` + test.
- **Service OpenCode UI dùng chung** → thêm `standalone: true` mặc định để mỗi run dùng server riêng.
- **Router coi “không chạy shell” là AUTOMATION** → thêm negation-aware routing + test.

### 3.3 Frozen baseline và durability

Fixture hồi quy:

- `tests/fixtures/opencode_readonly/opencode.json`
- `tests/fixtures/opencode_readonly/baseline.json`
- `tests/test_opencode_baseline.py`

Baseline: OpenCode v2.2.5, agent `trinity-readonly`, config SHA-256
`519b6c3936027c0f9f3b8999ca41c2e2e85b4b0cda73275f37435fec017d6f59`.

Ba run thật:

| Run | Task ID | Status | Result chars |
|---|---|---|---:|
| README | `TRN-20261104-154042-7e4461` | COMPLETED | 3.135 |
| sample.py | `TRN-20261104-154054-3d8a4c` | COMPLETED | 5.331 |
| list workspace | `TRN-20261104-154103-f1bc57` | COMPLETED | 13.811 |

Failure modes thật:

| Case | Task ID | Kết quả |
|---|---|---|
| Agent không tồn tại | `TRN-20261104-154124-357dd5` | FAILED — Agent not found |
| Timeout 1s | `TRN-20261104-154130-9432bd` | FAILED — TIMEOUT |

Sau tất cả: workspace/Git sandbox không đổi; không còn standalone server treo.

### 3.4 Leakage audit

- Prompt/result/error/event/audit đã được redact; DB/audit thật không chứa token mẫu.
- Biến môi trường có tên nhạy cảm (`*_SECRET`, `*_TOKEN`, `*_PASSWORD`,...) được redact khỏi stdout/stderr của subprocess.
- Test exception chứa `password=`/token: task error chỉ còn `REDACTED`.
- Quét DB/audit của các run thật: không có `root:x:0:0` hoặc nội dung `/etc/passwd`.
- Baseline permission test: đọc ngoài workspace bị `external_directory` deny.

### 3.5 Baseline approval

- `tests/fixtures/opencode_readonly/APPROVAL.md` quy định quy trình cập nhật.
- `baseline.json` có `approved_by`, `approved_at`, `reason`, `policy`.
- Test phát hiện mutation `deny → allow` và hash cấu hình thay đổi.
- Cập nhật baseline phải commit config + baseline + approval note; không amend lịch sử cũ.

## 4. Task Resume

Đã kiểm thử:

- Giữ Task ID, plan JSON, prompt redact, risk, profile, workspace.
- Không approve → vẫn `WAITING_APPROVAL`.
- Dry-run → không ghi dữ liệu.
- Approve → atomic claim, lưu `approved_by`, `approved_at`, `approval_scope`, `resume_count`.
- Hai claim cùng task: lần hai bị `StorageError`.
- Sai profile/workspace/plan thiếu → từ chối.
- Adapter/resource/lock chưa sẵn sàng → task vẫn `WAITING_APPROVAL`.
- Migration DB v3.0 → v3.1.
- Task v3.0 thiếu plan/workspace bị từ chối resume.

### 4.1 Crash recovery

- Task `RUNNING` bị bỏ lại sau khi Trinity dừng được đánh dấu `FAILED` khi khởi động lại, **chỉ khi lock có thể acquire**.
- Không đánh dấu `COMPLETED` nếu chưa có kết quả adapter xác nhận.
- Recovery idempotent; lock đang bận thì không đụng task.
- Test CLI: task orphan `RUNNING` → CLI `run` recovery → `FAILED`, sau đó task mới `WAITING_APPROVAL`.
- `PR_SET_PDEATHSIG` best-effort cho direct child trên Linux; không phải sandbox process tree.

## 5. Termux compatibility

Đã mô phỏng trên Linux:

- `PREFIX` detection → Termux profile.
- Ngưỡng RAM 192 MB / disk 150 MB.
- `/proc/meminfo` fallback `os.sysconf`.
- SQLite tạo/đọc/ghi, migration.
- `fcntl` lock; filesystem không hỗ trợ → fail closed.
- Non-POSIX → fail closed.
- Subprocess timeout + kill process group.

**Chưa chạy trên Redmi 15 5G / Termux thật**: `adb devices` không có thiết bị attached trong phiên P0. Checklist: `docs/TERMUX_COMPATIBILITY.md`.

## 6. Smoke test thủ công

| Lệnh | Kết quả |
|---|---|
| `python3 -m compileall -q ...` | PASS |
| `python3 trinity.py version` | PASS — `Trinity v3.1.0` |
| `python3 trinity.py config --profile termux` | PASS — RAM 192 MB |
| `python3 trinity.py status` | PASS — OpenCode AVAILABLE; execution cần agent |
| `python3 trinity.py doctor` | PASS — 0 FAIL, 3 WARN |
| `python3 trinity.py run --dry-run ...` | PASS — không tạo DB |
| `python3 trinity.py run "xóa dữ liệu production"` | PASS — WAITING_APPROVAL, exit 3 |
| `resume <id> --dry-run` | PASS — không đổi task |
| `resume <id>` (không approve) | PASS — vẫn WAITING_APPROVAL |
| OpenCode read-only thật | PASS — task `TRN-20261104-152925-1c79c9` |

## 7. Lỗi phát hiện và sửa trong v3.1

| Mức | Lỗi | Sửa |
|---|---|---|
| High | OpenCode dùng agent mặc định có thể edit/shell | `require_explicit_agent: true`, thiếu agent → DENIED |
| Medium | `PWD` không đồng bộ `cwd` → sai project config | Set `PWD`/`OLDPWD` + test |
| Medium | Shared OpenCode service/cache config | `standalone: true` + test |
| Medium | Router coi phủ định là hành động | Negation-aware `_hits` + test |
| Medium | Lock fail-open | Fail closed + test |
| Medium | Resume tạo task mới, mất plan | Resume cùng Task ID, plan/workspace |
| Medium | Hai tiến trình resume cùng task | Conditional SQLite claim + lock |
| Medium | Không lưu approval actor/time/scope | Thêm cột + event |
| Low | JSON output chưa kiểm tra | `json_valid` |
| Low | Permission error không phân loại | `permission_required` |

## 8. Những gì CHƯA kiểm chứng

1. **Agent production OpenCode (`build`/`general`)**: chưa chạy; quyền rộng hơn agent test.
2. **Hermes**: UNAVAILABLE; chưa kiểm chứng memory thật.
3. **OpenClaw**: UNAVAILABLE; chưa kiểm chứng exec/gateway thật.
4. **OpenCode HTTP execution**: health 200 nhưng auth/endpoint thực thi chưa xác minh; mặc định tắt.
5. **Termux trên thiết bị thật**: chưa có Redmi 15 5G.
6. **Resume chạy thật qua OpenCode**: đã test executor/CLI; chưa chạy resume thật với agent production.
7. **CI 165 test**: chờ chạy sau push; CI 153 test trước đó PASS.

## 9. Cách tái lập

```bash
cd trinity-v3
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -t . -v
python3 trinity.py doctor
```

Sandbox OpenCode chi tiết: `docs/OPENCODE_SANDBOX.md`.
