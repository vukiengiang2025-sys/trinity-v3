# AGENTS.md — TRINITY v3.1

Hướng dẫn cho agent làm việc trong repo này.

## 1. Phạm vi

- Repo: `trinity-v3` — Lightweight AI Agent Orchestrator.
- Chỉ sửa file trong repo này.
- Không sửa, xoá, commit các thư mục dự án khác trong `/home/vukgg04/repogithub/`.
- Không sửa cấu hình OpenCode global (`opencode.json`, `opencode.jsonc`, provider, model, MCP, plugin, Skills).
- Không cài/gỡ/nâng cấp OpenCode, Hermes, OpenClaw.
- Không đọc, hiển thị, ghi log hoặc commit API key/token/credential.

## 2. Kiến trúc bắt buộc

```text
trinity.py          CLI, không chứa logic adapter
core/router.py      phân loại + risk + plan, không cấp quyền
core/guard.py       path/command/git/safety, không thực thi
core/resource.py    RAM/disk/timeout/lock
core/executor.py    điều phối tuần tự, approval, task lifecycle
core/task.py        facade TaskManager
storage/memory.py   SQLite TaskManager + AuditLog
adapters/base.py    interface chung
adapters/*.py       chi tiết từng agent
config/*.yaml       dữ liệu profile, không chứa secrets
tests/              unittest, không pytest
```

Quy tắc:

- Không nhúng logic CLI của agent vào executor.
- Adapter mới phải kế thừa `BaseAdapter`, có `discover()` và `execute()`, timeout, error handling.
- Không chạy agent song song; giữ lock tuần tự.
- Không thêm dependency nặng nếu Standard Library đủ dùng.
- Không đổi schema SQLite/API công khai khi chưa có yêu cầu rõ.

## 3. An toàn

- Không dùng `shell=True`; luôn truyền argv list.
- Luôn kiểm tra path bằng `Guard` trước khi đọc/ghi.
- Không thêm lệnh vào allowlist nếu chưa cân nhắc rủi ro.
- Không nới `require_approval_for_risk` hoặc bỏ approval cho thao tác high risk.
- Không lưu prompt/result/error/audit mà chưa qua `redact()`/`truncate()`.
- Không tự tạo memory directory Hermes.
- Không tự khởi động OpenClaw gateway.
- Không chạy prompt thật qua OpenCode ngoài sandbox permission đã chứng minh; test thường dùng fake executable. Quy trình sandbox: `docs/OPENCODE_SANDBOX.md`.
- OpenCode phải có `agent` tường minh; không bật lại fallback agent mặc định.
- Giữ `standalone: true` và `PWD` đồng bộ `cwd` cho adapter OpenCode.
- Giữ baseline read-only và approval policy; không nới deny rule mà không review.
- Recovery chỉ đánh dấu task `RUNNING` orphan thành `FAILED` khi lock free; không đánh dấu `COMPLETED`.
- Không bỏ env-secret redaction hoặc dry-run recovery guard.
- Không nới lỏng `fcntl` fail-closed hoặc thêm fallback chạy không khóa.
- Resume phải giữ Task ID cũ, re-check Guard/Resource/adapter, dùng `claim_for_resume`; không tạo task trùng.
- Git: không `reset`, `clean`, `push --force`, `branch -D/-d`, `commit --amend`, `filter-repo`, không viết lại lịch sử.
- Không commit `.env`, `*.key`, credential, DB runtime, log runtime.

## 4. Lệnh kiểm tra

```bash
python3 -m compileall -q core adapters storage tests trinity.py
python3 -m unittest discover -s tests -t . -v
python3 trinity.py doctor
python3 trinity.py status
python3 trinity.py run --dry-run "sửa lỗi pytest trong module python"
python3 trinity.py resume <task_id> --dry-run
```

Test phải thực sự chạy; không tuyên bố pass nếu chưa chạy. Ghi rõ môi trường nào chưa kiểm chứng (Termux, Hermes, OpenClaw, agent production của OpenCode).

## 5. Adapter status hiện tại

| Adapter | Trạng thái | Ghi chú |
|---|---|---|
| OpenCode | AVAILABLE (CLI v2.2.5) | Đã test read-only thật với agent tạm `trinity-readonly`; execution fail-closed nếu thiếu `agent`; agent production/HTTP chưa xác minh |
| Hermes | UNAVAILABLE | Không có executable; không tạo memory dir |
| OpenClaw | UNAVAILABLE | Không có executable/gateway; cần `invoke_prefix` đã xác minh |

Không được đổi trạng thái trong tài liệu thành AVAILABLE nếu chưa kiểm chứng lại.

## 6. Git

- Remote: `https://github.com/vukiengiang2025-sys/trinity-v3.git`
- Branch: `main`
- Chỉ stage file thuộc repo này.
- Trước commit: `git status`, `git diff`, kiểm tra secrets, chạy test.
- Không force push. Nếu remote có commit mới, dừng và báo cáo; không reset.

## 7. Tài liệu

Cập nhật khi thay đổi hành vi:

- `README.md` — hướng dẫn người dùng.
- `docs/ARCHITECTURE.md` — thiết kế/luồng.
- `docs/SECURITY.md` — kiểm soát và giới hạn.
- `docs/ENVIRONMENT_REPORT.md` — môi trường thực tế.
- `docs/TEST_REPORT.md` — kết quả test thực tế.
- `docs/OPENCODE_SANDBOX.md` — quy trình kiểm chứng OpenCode read-only.
