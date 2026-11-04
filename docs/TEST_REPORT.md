# TEST REPORT — TRINITY v3.1

- **Ngày:** 2026-11-04
- **Môi trường:** Linux Mint 22.3, Python 3.12.3, OpenCode v2.2.5
- **Lệnh:** `python3 -m unittest discover -s tests -t . -v`
- **Kết quả:** **139 test — 0 failure — 0 error — OK**
- **Trước v3.1:** 115 test
- **Sau v3.1:** 139 test (+24)
- **CI GitHub Actions:** PASS — Python 3.9 và Python 3.12 (run 41853332251)
- **Không dùng pytest**: test chạy bằng Standard Library `unittest`.

## 1. Tổng hợp theo module

| File | Số test | Nội dung |
|---|---:|---|
| `test_config.py` | 9 | Profile Linux/Termux, auto-detect, profile sai, allowed roots, mask secrets |
| `test_router.py` | 11 | MEMORY/CODING/AUTOMATION/MIXED/GENERAL, risk, plan, Task ID, normalize |
| `test_guard.py` | 20 | Path, symlink, file nhạy cảm, command allowlist, git safety, injection, interpreter |
| `test_resource.py` | 11 | RAM đọc được/không đọc được, sysconf fallback, RAM/disk thấp, lock, timeout |
| `test_task.py` | 12 | Task lifecycle, transition, event, permission, missing DB, redaction, audit |
| `test_adapters.py` | 22 | Missing tool, discovery, fake CLI, timeout, injection, JSON, permission, OpenClaw/Hermes, default-agent deny |
| `test_executor.py` | 12 | Tuần tự, approval, fail-stop, resource/lock failure, redaction |
| `test_resume.py` | 16 | Resume cùng Task ID, approval record, atomic claim, mismatch, migration, lock fail-closed |
| `test_cli.py` | 17 | version/status/config/tasks/task/run/resume, dry-run, profile, exit code, default-agent deny |
| `test_utils.py` | 9 | Redaction, truncate, mask mapping, subprocess timeout/output |
| **Tổng** | **139** | |

## 2. Kết quả CI

Workflow: `.github/workflows/tests.yml`

| Job | Kết quả |
|---|---|
| `unittest (3.9)` | **success** |
| `unittest (3.12)` | **success** |

v3.1 code commit `2298aba`; run: <https://github.com/vukiengiang2025-sys/trinity-v3/actions/runs/41853332251>.

## 3. Đối chiếu yêu cầu kiểm thử

| # | Yêu cầu | Test tương ứng | Kết quả |
|---|---|---|---|
| 1 | Router phân loại đúng nhóm | `test_router.py` | PASS |
| 2 | Guard từ chối lệnh nguy hiểm | `test_guard.py::test_dangerous_commands_denied`, `test_git_force...` | PASS |
| 3 | Guard chặn ngoài workspace | `test_guard.py` path/symlink/traversal | PASS |
| 4 | Resource xử lý RAM không đọc được | `test_resource.py` unknown/fallback | PASS |
| 5 | Task Manager tạo/cập nhật Task ID | `test_task.py` | PASS |
| 6 | SQLite không lưu credentials | `test_task.py`, `AuditLogTests` | PASS |
| 7 | Adapter xử lý tool không tồn tại | `test_adapters.py` missing binaries | PASS |
| 8 | Adapter có timeout | `test_adapters.py::test_timeout_is_enforced`, `test_utils.py` | PASS |
| 9 | CLI lệnh cơ bản | `test_cli.py` | PASS |
| 10 | Dry-run không thay đổi dữ liệu | `test_cli.py`, `test_executor.py`, `test_resume.py` | PASS |
| 11 | Git safety không force/reset | `test_guard.py::test_git_force_and_history_rewrite_denied` | PASS |
| 12 | Profile Linux/Termux | `test_config.py`, `test_cli.py` | PASS |

## 4. Kiểm định OpenCode Adapter

Đã kiểm tra bằng code + fake CLI:

- Tạo subprocess bằng argv list, không `shell=True`.
- Prompt được truyền thành một argument; prompt bắt đầu `-`/chứa NUL bị từ chối.
- Timeout và kill process group được test.
- Exit code khác 0 → `FAILED`; output được redact.
- JSON output được kiểm tra `json_valid`.
- Lỗi permission/approval được đánh dấu `permission_required`; không dùng `--auto`.
- **Default agent fail closed**: thiếu `adapters.opencode.agent` → `DENIED`.
- ACL: `opencode debug agents` cho thấy:
  - `build`/`general`: wildcard allow (edit + shell).
  - `plan`: edit deny, nhưng wildcard vẫn cho shell.
  - `architect`: edit deny, shell chỉ Git read-only, nhưng vẫn có read/glob/grep/webfetch.
  - Không có agent built-in vừa deny edit vừa deny shell.
- Vì vậy **không chạy prompt thật** qua OpenCode; chỉ kiểm thử bằng fake CLI. Đây là giới hạn có chủ đích, không phải test pass giả.

## 5. Task Resume

Đã triển khai và kiểm thử:

- Giữ nguyên Task ID, plan JSON, prompt đã redact, risk, profile.
- `resume <id>` không có `--approve` → vẫn `WAITING_APPROVAL`, không chạy adapter.
- `resume <id> --dry-run` → không ghi dữ liệu, không đổi trạng thái.
- `resume <id> --approve` → claim atomic, chạy plan, lưu `approved_by`, `approved_at`, `approval_scope`, `resume_count`.
- Hai lần claim cùng task: lần hai bị `StorageError`.
- Sai profile/workspace/plan thiếu → từ chối.
- Adapter unavailable / resource denied / lock busy → task vẫn `WAITING_APPROVAL`, có event `resume_blocked`.
- Task v3.0 thiếu `plan_json`/`workspace` bị từ chối resume.
- Migration tự động thêm cột v3.1 vào DB v3.0.
- Ghi nhận `approved_by` từ user hệ thống, thời điểm, scope hash plan.

Trạng thái: **IMPLEMENTED & TESTED** ở mức executor + CLI dry-run; chưa chạy resume thật qua OpenCode vì default-agent/permission chưa được chứng minh.

## 6. Termux Compatibility

Đã kiểm tra mô phỏng trên Linux:

- `PREFIX` detection → Termux profile.
- Ngưỡng RAM 192 MB / disk 150 MB.
- `/proc/meminfo` fallback `os.sysconf`.
- SQLite tạo/đọc/ghi, migration.
- `fcntl` lock; filesystem không hỗ trợ → **fail closed**.
- Non-POSIX → **fail closed**.
- Subprocess timeout + kill process group.

**Chưa chạy trên Redmi 15 5G / Termux thật.** Checklist và tiêu chí PASS: `docs/TERMUX_COMPATIBILITY.md`.

## 7. Smoke test thủ công

| Lệnh | Kết quả |
|---|---|
| `python3 -m compileall -q ...` | PASS |
| `python3 trinity.py version` | PASS |
| `python3 trinity.py config` | PASS — profile linux |
| `python3 trinity.py config --profile termux` | PASS — RAM 192 MB |
| `python3 trinity.py status` | PASS — OpenCode AVAILABLE; execution cần agent |
| `python3 trinity.py doctor` | PASS — 0 FAIL, 3 WARN (Hermes, OpenClaw, OpenCode agent chưa cấu hình) |
| `python3 trinity.py run --dry-run "sửa lỗi pytest"` | PASS — không tạo DB |
| `python3 trinity.py run "xóa dữ liệu production"` | PASS — WAITING_APPROVAL, exit 3 |
| `python3 trinity.py resume <id> --dry-run` | PASS — không đổi task |
| `python3 trinity.py resume <id>` (không approve) | PASS — vẫn WAITING_APPROVAL |
| `python3 trinity.py resume <hermes-task> --approve` | PASS — adapter UNAVAILABLE, task vẫn WAITING_APPROVAL |

## 8. Lỗi đã phát hiện và sửa trong v3.1

| Mức | Lỗi | Sửa |
|---|---|---|
| High | OpenCode dùng agent mặc định có thể edit/shell tự động | `require_explicit_agent: true`, thiếu agent → DENIED |
| Medium | Lock fail-open khi fcntl/FS không hỗ trợ | Fail closed + test |
| Medium | Resume tạo task mới, mất plan | Resume cùng Task ID, lưu plan/workspace |
| Medium | Hai tiến trình resume cùng task | Conditional SQLite claim + lock |
| Medium | Không lưu approval actor/time/scope | Thêm cột + event |
| Low | JSON output chưa kiểm tra | `json_valid` metadata |
| Low | Permission error không phân loại | `permission_required` metadata |
| Low | DB v3.0 không có cột resume | Migration ALTER TABLE + test |

## 9. Những gì CHƯA kiểm chứng

1. **OpenCode prompt thật**: chưa có agent built-in đáp ứng edit-deny + shell-deny; không chạy để tránh hành vi ngoài kiểm soát.
2. **Hermes**: UNAVAILABLE; chưa kiểm chứng memory thật.
3. **OpenClaw**: UNAVAILABLE; chưa kiểm chứng exec/gateway thật.
4. **OpenCode HTTP execution**: health 200 nhưng auth/endpoint thực thi chưa xác minh; mặc định tắt.
5. **Termux trên thiết bị thật**: chưa có Redmi 15 5G trong môi trường build.
6. **Resume chạy thật qua OpenCode**: chỉ test executor/CLI với fake adapter; default-agent fail-closed đã test.
7. **CI v3.1**: PASS trên Python 3.9 và 3.12 (run 41853332251).

## 10. Cách tái lập

```bash
cd trinity-v3
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -t . -v
python3 trinity.py doctor
python3 trinity.py run --dry-run "sửa lỗi pytest trong module python"
```
