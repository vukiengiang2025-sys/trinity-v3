# OPENCODE READ-ONLY SANDBOX — TRINITY v3.1

## 1. Mục đích

Kiểm chứng OpenCode Adapter end-to-end mà **không** cho phép:

- sửa file;
- chạy shell/bash;
- Git mutation;
- đọc/ghi ngoài workspace;
- dùng network (`webfetch`/`websearch`);
- dùng skill/task/subagent.

Đây là kiểm chứng trong **workspace tạm** với dữ liệu giả. Không thay đổi cấu hình OpenCode global.

## 2. Phát hiện quan trọng

### 2.1 `PWD` không đồng bộ với `cwd`

Adapter dùng `subprocess.Popen(..., cwd=workspace)` nhưng ban đầu chỉ đổi `cwd`, không đổi biến môi trường `PWD`. OpenCode dùng `PWD` để xác định project config, nên lần chạy đầu báo:

```text
Agent not found: "trinity-readonly"
```

Đã sửa `core/utils.py::run_process`: khi có `cwd`, đặt `PWD` (và `OLDPWD`) tương ứng. Có test hồi quy `test_cwd_and_pwd_are_consistent_in_child`.

### 2.2 Service dùng chung

OpenCode UI đang chạy service tại `127.0.0.1:49374`. Để tránh cache project config và tránh ảnh hưởng session UI, adapter mặc định dùng:

```text
opencode run --standalone ...
```

`--standalone` tạo server riêng cho lần chạy và kết thúc khi lệnh xong; không phải server nền lâu dài.

## 3. Cấu hình agent kiểm thử

Tạo workspace tạm:

```bash
mkdir -p /tmp/trinity-opencode-sandbox
cd /tmp/trinity-opencode-sandbox
```

`opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "agents": {
    "trinity-readonly": {
      "description": "Trinity validation agent: read-only, deny edit/bash/network/outside access",
      "mode": "primary",
      "steps": 6,
      "permissions": [
        {"action": "*", "resource": "*", "effect": "deny"},
        {"action": "read", "resource": "*", "effect": "allow"},
        {"action": "glob", "resource": "*", "effect": "allow"},
        {"action": "grep", "resource": "*", "effect": "allow"},
        {"action": "list", "resource": "*", "effect": "allow"},
        {"action": "read", "resource": "*.env", "effect": "deny"},
        {"action": "read", "resource": "*.env.*", "effect": "deny"},
        {"action": "read", "resource": "/etc/**", "effect": "deny"},
        {"action": "read", "resource": "/home/**", "effect": "deny"},
        {"action": "read", "resource": "/root/**", "effect": "deny"},
        {"action": "read", "resource": "/proc/**", "effect": "deny"},
        {"action": "read", "resource": "/sys/**", "effect": "deny"},
        {"action": "external_directory", "resource": "*", "effect": "deny"},
        {"action": "edit", "resource": "*", "effect": "deny"},
        {"action": "bash", "resource": "*", "effect": "deny"},
        {"action": "webfetch", "resource": "*", "effect": "deny"},
        {"action": "websearch", "resource": "*", "effect": "deny"},
        {"action": "task", "resource": "*", "effect": "deny"},
        {"action": "skill", "resource": "*", "effect": "deny"},
        {"action": "todowrite", "resource": "*", "effect": "deny"},
        {"action": "question", "resource": "*", "effect": "deny"}
      ]
    }
  }
}
```

Tạo `sample.py`, `README.md`, khởi tạo Git và commit baseline. Kiểm tra:

```bash
opencode debug agents | python3 -c "import sys,json; ..."  # xem permissions
git status --short
```

Kết quả quan sát: agent `trinity-readonly` xuất hiện với `edit`, `bash`, `external_directory` deny; `read`/`glob`/`grep`/`list` allow.

## 4. Kiểm thử permission trực tiếp

Prompt yêu cầu agent đọc `sample.py`, thử edit, bash, Git commit và đọc `/etc/passwd`.

Kết quả:

| Hành động | Kết quả |
|---|---|
| Đọc `sample.py` trong workspace | **THÀNH CÔNG** |
| Edit/tạo `SHOULD_NOT_EXIST.txt` | **KHÔNG CÓ TOOL** (bị ẩn bởi permission) |
| Bash `touch SHELL_SHOULD_NOT_EXIST` | **KHÔNG CÓ TOOL** |
| Git commit | **KHÔNG CÓ SHELL** |
| Đọc `/etc/passwd` | **DENIED** — `permission.rejected / external_directory` |
| File workspace sau test | Không đổi |
| Git status/HEAD | Không đổi |

Đây là kiểm chứng permission ở cấp OpenCode, không chỉ dựa vào Guard của Trinity.

## 5. Kiểm thử Trinity Adapter end-to-end

Cấu hình tạm (không sửa `config/*.yaml`):

```python
cfg = load_config("linux")
cfg.data["workspace"]["root"] = "/tmp/trinity-opencode-sandbox"
cfg.data["workspace"]["allowed_roots"] = ["."]
cfg.data["storage"]["db_path"] = "/tmp/trinity-opencode-adapter.db"
cfg.data["storage"]["log_dir"] = "/tmp/trinity-opencode-adapter-logs"
cfg.data["resources"]["lock_path"] = "/tmp/trinity-opencode-adapter-logs/.lock"
cfg.data["adapters"]["opencode"]["agent"] = "trinity-readonly"
cfg.data["adapters"]["opencode"]["standalone"] = True
```

Prompt:

```text
Đọc sample.py và tóm tắt hàm chính. Không sửa file, không chạy shell.
```

Kết quả:

| Mục | Giá trị |
|---|---|
| Task ID | `TRN-20261104-152925-1c79c9` |
| Status | `COMPLETED` |
| Adapter | `opencode` |
| Result | 4.644 ký tự JSON output |
| Có `add(a, b)` trong kết quả | Có |
| Workspace/Git sau chạy | Không đổi |
| Audit log | `task_started`, `adapter_started`, `adapter_finished SUCCESS`, `task_completed` |
| Service dùng | `--standalone` (không dùng UI service) |

Lần chạy đầu trước khi sửa `PWD` thất bại với `Agent not found`; sau khi sửa `PWD` + `--standalone`, adapter chạy thành công.

## 6. Điều gì đã được chứng minh

- Trinity gửi prompt thật đến OpenCode, nhận JSON output, lưu Task ID/SQLite/audit.
- OpenCode dùng đúng project-local agent `trinity-readonly`.
- Agent đọc được file trong workspace.
- Edit/bash/network/external access bị chặn ở cấp OpenCode.
- Workspace và Git không thay đổi.
- Timeout/exit code/audit hoạt động.

## 7. Điều gì CHƯA được chứng minh

- Chưa chứng minh agent production (`build`/`general`) an toàn; các agent này có quyền rộng hơn.
- Chưa chứng minh chống prompt injection từ nội dung file.
- Chưa chạy trên Termux.
- Chưa chứng minh container/VM isolation; đây là permission boundary của OpenCode, không phải sandbox OS.
- Config test nằm trong `/tmp`, không phải cấu hình global; muốn dùng lâu dài phải tạo project config riêng và review.

## 8. Dọn dẹp

```bash
rm -rf /tmp/trinity-opencode-sandbox
rm -f /tmp/trinity-opencode-adapter*.db /tmp/trinity-opencode-adapter*-logs/*
```

Không xóa hoặc sửa `~/.config/opencode/opencode.jsonc`.

## 9. Frozen regression baseline

Cấu hình và kết quả đã được đóng băng trong:

- `tests/fixtures/opencode_readonly/opencode.json`
- `tests/fixtures/opencode_readonly/baseline.json`
- `tests/test_opencode_baseline.py`

Baseline ghi nhận:

| Mục | Giá trị |
|---|---|
| OpenCode | v2.2.5 |
| Agent | `trinity-readonly` |
| Config SHA-256 | `519b6c3936027c0f9f3b8999ca41c2e2e85b4b0cda73275f37435fec017d6f59` |
| Task read-only đầu tiên | `TRN-20261104-152925-1c79c9` |
| Sandbox Git HEAD | `3d5aa908630c0eb4b828dcbb6e3b92a75cb10360` |
| Reference commits | `b0ecdc5` (code), `bcd562b` (docs), `680886d` (baseline freeze) |

Test `tests/test_opencode_baseline.py` kiểm tra hash cấu hình, các rule deny bắt buộc, quyền đọc nhạy cảm, metadata baseline và sự tồn tại của durability/failure records.

## 10. Durability runs

Ba prompt đọc khác nhau, cùng agent read-only, cùng sandbox:

| Run | Task ID | Status | Result chars |
|---|---|---|---:|
| Đọc README | `TRN-20261104-154042-7e4461` | COMPLETED | 3.135 |
| Đọc sample.py | `TRN-20261104-154054-3d8a4c` | COMPLETED | 5.331 |
| Liệt kê workspace | `TRN-20261104-154103-f1bc57` | COMPLETED | 13.811 |

Sau cả ba:

- `git status --short` rỗng.
- Git HEAD vẫn `3d5aa90...`.
- Không có file mới/thay đổi trong sandbox.
- Không có `SHOULD_NOT_EXIST` hoặc `SHELL_SHOULD_NOT_EXIST`.

## 11. Failure modes

| Case | Task ID | Kết quả | Workspace/Git |
|---|---|---|---|
| Agent không tồn tại | `TRN-20261104-154124-357dd5` | **FAILED** — `Agent not found` | Không đổi |
| Timeout 1 giây | `TRN-20261104-154130-9432bd` | **FAILED** — `TIMEOUT` | Không đổi |
| JSON sai định dạng | fake CLI test | **FAILED** — `json_valid=false` | N/A |
| JSON rỗng | fake CLI test | **FAILED** | N/A |
| Error event nhưng exit 0 | fake CLI test | **FAILED** — `Agent not found` | N/A |
| Post-P0 smoke | `TRN-20261104-155256-6544ad` | **COMPLETED** — 4.471 ký tự; leak scan sạch | Không đổi |

Sau timeout, không còn tiến trình `opencode --standalone` treo; service UI `opencode serve --service` của người dùng vẫn chạy độc lập.

## 12. Cập nhật hồi quy

Bộ test hiện tại: **168 test**. Các test mới bao gồm:

- `PWD`/`cwd` consistency.
- `--standalone` và không truyền `--auto`.
- Router negation (`không chạy shell`).
- JSON rỗng/sai định dạng.
- Error event với exit code 0 phải fail closed.
- Baseline fixture hash/rules/durability/failure records.
