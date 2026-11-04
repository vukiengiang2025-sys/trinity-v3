# ARCHITECTURE — TRINITY v3.1

## 1. Mục tiêu thiết kế

Trinity là **lightweight orchestrator** bằng Python, chạy trên Linux Mint và Android Termux. Nó không nhúng mã của Hermes/OpenCode/OpenClaw; mỗi agent được bọc trong một adapter độc lập.

Ưu tiên:

1. Đúng và an toàn hơn số lượng tính năng.
2. On-demand, tuần tự, không agent nền.
3. Core nhỏ, adapter thay thế được.
4. Không phụ thuộc framework nặng.
5. Dữ liệu lịch sử rõ ràng, không chứa credentials.

## 2. Sơ đồ tổng thể

```text
┌──────────────────────────────────────────────────────────────┐
│                         trinity.py CLI                       │
│  status | doctor | run | tasks | task | config | version     │
└──────────────────────────────┬───────────────────────────────┘
                               │
                     ┌─────────▼─────────┐
                     │    Task Router    │  rule-based, no AI
                     │ type + risk + plan│
                     └─────────┬─────────┘
                               │
                     ┌─────────▼─────────┐
                     │   Safety Guard    │  path / command / git
                     └─────────┬─────────┘
                               │
                     ┌─────────▼─────────┐
                     │  Resource Guard   │  RAM / disk / timeout
                     │ + fcntl task lock │
                     └─────────┬─────────┘
                               │
                     ┌─────────▼─────────┐
                     │     Executor      │  strictly sequential
                     └───┬────────┬──────┘
             ┌───────────┘        └────────────┐
    ┌────────▼────────┐   ┌────────▼────────┐   ┌────────▼────────┐
    │ Hermes Adapter  │   │ OpenCode Adapter│   │ OpenClaw Adapter│
    │     BRAIN       │   │      LOGIC      │   │     MUSCLE      │
    └─────────────────┘   └─────────────────┘   └─────────────────┘
             │                    │                    │
             └────────────────────┴────────────────────┘
                                  │
                 ┌────────────────┴────────────────┐
                 │                                 │
        ┌────────▼────────┐               ┌────────▼────────┐
        │ SQLite tasks    │               │ audit.jsonl     │
        │ tasks + events  │               │ redacted JSONL  │
        └─────────────────┘               └─────────────────┘
```

## 3. Thành phần

### 3.1 CLI — `trinity.py`

- Nạp profile tự động hoặc thủ công.
- Không bắt buộc Internet cho `version`, `config`, `status`, `doctor`, `tasks`.
- Hiển thị lỗi tiếng Việt, không in secrets.
- `run --dry-run` chỉ lập kế hoạch; không ghi SQLite, không ghi audit, không gọi adapter.
- Exit code: `0` thành công, `1` thất bại, `2` lỗi dùng/config, `3` chờ phê duyệt.

### 3.2 Task Router — `core/router.py`

Rule-based, không gọi thêm AI để phân loại. Chuẩn hóa tiếng Việt (bỏ dấu, `đ→d`) rồi so khớp keyword có ranh giới từ.

Đầu ra:

```json
{
  "task_id": "TRN-20261104-143120-bce909",
  "task_type": "MIXED",
  "selected_adapter": "multi",
  "execution_plan": [
    {"step": "1", "adapter": "hermes", "role": "BRAIN", "action": "memory_query"},
    {"step": "2", "adapter": "opencode", "role": "LOGIC", "action": "code_task"},
    {"step": "3", "adapter": "openclaw", "role": "MUSCLE", "action": "automation_task"}
  ],
  "risk_level": "medium",
  "approval_required": false
}
```

Phân loại:

| Loại | Dấu hiệu | Adapter |
|---|---|---|
| `MEMORY` | memory, ghi nhớ, truy xuất, skill, context | Hermes |
| `CODING` | code, refactor, bug, test, python, module | OpenCode |
| `AUTOMATION` | chạy lệnh, shell, git, backup, cron, deploy | OpenClaw |
| `MIXED` | từ 2 nhóm trở lên | nhiều adapter, tuần tự |
| `GENERAL` | không khớp nhóm nào | OpenCode |

Thứ tự plan cố định: **BRAIN → LOGIC → MUSCLE**. Router chỉ trả risk/plan; **không cấp quyền thực thi**. Executor kiểm tra lại approval.

Router bỏ qua keyword bị phủ định trong cùng mệnh đề (ví dụ “không chạy shell”, “không dùng memory”), tránh thêm bước AUTOMATION/MEMORY sai. Test hồi quy nằm trong `tests/test_router.py`.

Controlled write: `route(write=True)` chỉ chấp nhận nhiệm vụ CODING, đổi action thành `code_write`, ép risk `high`; executor chỉ dispatch khi có `--write --approve`.

### 3.3 Safety Guard — `core/guard.py`

Kiểm tra thuần túy, không thực thi:

- Workspace containment: path resolve, `commonpath`, chặn traversal, absolute path ngoài root, symlink escape.
- File nhạy cảm: `.env*`, `*.pem`, `*.key`, `id_rsa*`, `credentials*`, `secrets*`, `.git/`, `.ssh/`, `.gnupg/`.
- Command: executable allowlist, argv-only, chặn shell metachar khi input là chuỗi, chặn path ngoài workspace, chặn option ghi file nguy hiểm.
- Git: deny `reset`, `clean`, `filter-repo`, `filter-branch`, `gc`, `prune`, `update-ref`; deny `push --force/-f/--force-with-lease/+refspec/:refspec`; deny `branch -D/-d`, `commit --amend`, xóa tag/stash, thay đổi remote/config, subcommand không nằm trong allowlist.
- `checkout`, `switch`, `restore`, `rm` cần approval vì có thể làm mất thay đổi working tree.
- Approval: risk nằm trong `guard.require_approval_for_risk` → `requires_approval=True`; executor quyết định dừng hay chạy.
- `git add .`/`-A` yêu cầu approval để tránh stage secrets.

Guard không phải sandbox. Nó không thể ngăn một interpreter được allowlist thực hiện hành vi tùy ý; đây là lý do `python3`/`pytest` được xếp risk `high` và cần approval.

### 3.4 Resource Guard — `core/resource.py`

- Đọc `/proc/meminfo` (`MemAvailable`), fallback `MemFree+Buffers+Cached`, rồi `os.sysconf`.
- Nếu không đọc được: `readable=False`, `available_mb=None`; **không coi là 0 MB**, tiếp tục với cảnh báo.
- Ngưỡng riêng: Linux 384 MB / Termux 192 MB; disk 200/150 MB.
- `on_low_memory: deny|warn`.
- Timeout clamp theo profile (`default_timeout_seconds`, `max_timeout_seconds`).
- Khóa tuần tự bằng `fcntl.flock` tại `logs/.trinity.lock`, non-blocking; task thứ hai fail ngay.
- Lock **fail closed**: nếu `fcntl` không khả dụng, non-POSIX, hoặc filesystem không hỗ trợ flock (shared storage Android), Trinity từ chối chạy thay vì chạy không khóa.

### 3.5 Executor — `core/executor.py`

Luồng `run` thật:

1. Route.
2. Nếu risk cao và chưa `--approve`: tạo task `PENDING → WAITING_APPROVAL`, dừng.
3. Resource preflight; nếu fail: tạo task `PENDING → FAILED`.
4. Tạo task `PENDING`, acquire lock, `PENDING → RUNNING`.
5. Chạy từng bước plan tuần tự; ghi event trước/sau mỗi adapter.
6. Bước fail (`FAILED`, `TIMEOUT`, `UNAVAILABLE`, `DENIED`, `UNVERIFIED`) → dừng plan, task `FAILED`.
7. Tất cả thành công → `COMPLETED`, lưu output đã redact/truncate.
8. Luôn release lock trong `finally`.

Không có thread pool, không chạy song song, không retry vô hạn.

Khởi động lại: `Executor.recover_interrupted_tasks()` acquire lock non-blocking. Mỗi runner có `runner_id` (host:pid:nonce) và heartbeat mỗi 5s. Chỉ task `RUNNING` có heartbeat cũ hơn `recovery_stale_seconds` (mặc định 60s) hoặc chưa có heartbeat mới bị đánh dấu `FAILED` với event `recovered`. Nếu lock đang bận hoặc heartbeat còn mới, không đụng task — kể cả khi lock file bị mất. Dry-run không recovery. Không task nào được đánh dấu `COMPLETED` nếu chưa có adapter result hợp lệ.

Luồng `resume` (v3.1):

1. Đọc task theo Task ID; chỉ chấp nhận `WAITING_APPROVAL`.
2. Kiểm tra profile, workspace đã lưu, `plan_json` và prompt đã lưu.
3. Re-check Guard workspace, Resource, adapter availability.
4. Không có `--approve` → chỉ in kế hoạch, không ghi.
5. Acquire lock; `claim_for_resume()` UPDATE có điều kiện `WHERE status='WAITING_APPROVAL'`.
6. Lưu `approved_by`, `approved_at`, `approval_scope`, tăng `resume_count`.
7. Chạy đúng plan cũ; nếu adapter/resource/lock chưa sẵn sàng, giữ `WAITING_APPROVAL`.
8. Nếu đã claim mà execution fail → `FAILED`, không chạy lại trùng.

Ở trạng thái `WAITING_APPROVAL`, chưa có bước nào được thực thi, nên resume không chạy lại bước đã hoàn thành. Task v3.0 thiếu plan/workspace bị từ chối resume thay vì đoán.

### 3.6 Storage — `storage/memory.py` và `core/task.py`

`core/task.py` là facade công khai; implementation SQLite nằm trong `storage/memory.py`.

Schema:

```sql
tasks(
  id TEXT PRIMARY KEY,
  created_at TEXT, updated_at TEXT,
  task_type TEXT, status TEXT,
  adapter TEXT, risk_level TEXT, profile TEXT,
  prompt TEXT, result TEXT, error TEXT,
  dry_run INTEGER,
  plan_json TEXT, workspace TEXT,
  approved_by TEXT, approved_at TEXT,
  approval_scope TEXT, resume_count INTEGER
)

task_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
  created_at TEXT, kind TEXT, message TEXT
)
```

Trạng thái và transition:

```text
PENDING ──► RUNNING ──► COMPLETED
   │           └─────► FAILED
   ├─────► WAITING_APPROVAL ──► RUNNING (resume) ──► COMPLETED / FAILED
   │                         └──► CANCELLED / FAILED
   └─────► FAILED / CANCELLED
```

- Task ID: `TRN-YYYYMMDD-HHMMSS-<6 hex>`.
- Không có cột cho API key/token/password.
- `plan_json`/`workspace` được lưu khi tạo task để resume đúng plan và đúng workspace.
- `claim_for_resume` là barrier chống chạy trùng; `resume_count` tăng đúng một lần.
- Mọi text được `truncate()` (redact + giới hạn ký tự) trước khi ghi.
- DB `0600`, thư mục `0700`; audit JSONL `0600`.
- Schema v3.1 có migration `ALTER TABLE` cho DB v3.0; `PRAGMA user_version = 2`.

### 3.7 Adapter contract — `adapters/base.py`

```python
class BaseAdapter:
    def discover(force=False) -> AdapterHealth
    def execute(request: AdapterRequest) -> AdapterResult
```

`AdapterHealth`:

- `AVAILABLE`, `INSTALLED_BUT_UNVERIFIED`, `UNAVAILABLE`, `ERROR`.
- Gồm version, executable, detail, capabilities, memory_path, http_url.

`AdapterResult`:

- `SUCCESS`, `FAILED`, `TIMEOUT`, `UNAVAILABLE`, `DENIED`, `UNVERIFIED`, `DRY_RUN`.
- Timeout bắt buộc, output redacted, duration.

Mọi subprocess dùng `run_process(argv, timeout, ...)`: `shell=False`, `start_new_session=True`, kill process group khi timeout, giới hạn output. Khi có `cwd`, `PWD`/`OLDPWD` được đồng bộ để công cụ như OpenCode không đọc nhầm project config. Output được redact theo pattern và theo giá trị của các biến môi trường có tên nhạy cảm. Trên Linux, direct child nhận `PR_SET_PDEATHSIG` best-effort.

OpenCode adapter có thêm policy riêng (v3.1):

- `require_explicit_agent: true` + `agent: null` → `DENIED`, không dùng agent mặc định.
- `code_task` dùng `agent` (`trinity-readonly`); `code_write` dùng `write_agent` (`trinity-write`) và không fallback.
- Chỉ operator đặt `agent`/`write_agent` sau khi kiểm tra `opencode debug agents`.
- `standalone: true` mặc định: server riêng cho lần chạy, tránh service/cache của OpenCode UI.
- Output JSON được kiểm tra hợp lệ (`json_valid`); lỗi permission được đánh dấu `permission_required`.
- Không truyền `--auto`.
- Đã kiểm chứng read-only thật với agent project-local `trinity-readonly`: xem `docs/OPENCODE_SANDBOX.md`.
- Đã kiểm chứng controlled write thật với `trinity-write` trong sandbox: xem `docs/OPENCODE_WRITE.md`.

## 4. Luồng dữ liệu

```text
prompt
  → normalize/keyword → Route
  → risk check → approval?
  → resource check → lock
  → SQLite create task
  → adapter.execute(request)
  → AdapterResult
  → SQLite event/result/error
  → audit JSONL
  → CLI output (redacted)
```

Không có prompt hay output nào được gửi tới agent khác ngoài adapter đã chọn trong plan. Không có agent nền.

## 5. Mở rộng adapter

1. Tạo `adapters/myagent.py`, kế thừa `BaseAdapter`.
2. Cài `name`, `role`.
3. `discover()`: kiểm tra executable/version/help thật; trả `AVAILABLE` chỉ khi xác minh được.
4. `execute()`: kiểm tra health, validate input, dựng argv list, gọi `run_process` với timeout, trả `AdapterResult`.
5. Đăng ký trong `core/executor.py` và `adapters/__init__.py`.
6. Thêm route mapping trong `core/router.py` nếu là nhóm mới.
7. Thêm config vào cả `linux.yaml` và `termux.yaml`.
8. Viết test cho missing tool, timeout, error, approval, path/command safety.
9. Cập nhật `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `README.md`.

Không sửa executor để biết chi tiết CLI của agent mới; toàn bộ chi tiết nằm trong adapter.

## 6. Quyết định thiết kế

| Quyết định | Lý do |
|---|---|
| Rule-based router | Rẻ, deterministic, không phụ thuộc model; dễ test |
| PyYAML là dependency duy nhất | Profile YAML rõ ràng, vẫn nhẹ |
| SQLite | Có sẵn, không server, phù hợp Termux |
| JSONL audit | Append-only, dễ đọc/parse, không cần DB thứ hai |
| fcntl lock | Đơn giản, đúng trên Linux/Termux, không cần Redis |
| CLI trước, HTTP sau | Tránh phụ thuộc server/auth chưa xác minh |
| Không tự cài agent | Tránh thay đổi môi trường người dùng ngoài phạm vi |
| Fail closed | Adapter thiếu guard/allowlist → DENIED/UNAVAILABLE, không đoán |

## 7. Giới hạn đã biết

- Guard không sandbox được hành vi bên trong OpenCode/Hermes/OpenClaw.
- Chưa có HTTP execution adapter cho OpenCode (auth/endpoint chưa xác minh).
- OpenClaw natural-language execution chưa hỗ trợ; cần argv tường minh và `invoke_prefix` đã xác minh.
- Hermes cần command template do operator cấu hình; không đoán CLI.
- Chưa kiểm thử trên thiết bị Termux thật; profile Termux mới được test ở mức logic/cấu hình. Xem `docs/TERMUX_COMPATIBILITY.md`.
- Không có retry/queue dài hạn; mỗi `run` là một task tuần tự.
- Task `WAITING_APPROVAL` chỉ resume được nếu còn `plan_json`/`workspace`; task v3.0 cũ bị từ chối.
- OpenCode execution mặc định fail closed nếu chưa cấu hình `agent`; không có agent built-in nào vừa deny edit vừa deny shell.
- OpenCode read-only đã được kiểm chứng với `trinity-readonly`; controlled write đã được kiểm chứng trong sandbox với `trinity-write`; agent production (`build`/`general`) và HTTP execution vẫn chưa.
- Guard không kiểm soát công cụ nội bộ của OpenCode sau khi tiến trình được khởi chạy; permission của agent là lớp kiểm soát chính.
- Recovery dùng lease/heartbeat, không chỉ lock; task có heartbeat mới không bị đánh dấu sai. Recovery vẫn chỉ đánh dấu task orphan `FAILED`, không đảm bảo kill mọi process detached.
- Xóa lock file khi đang chạy có thể phá mutual exclusion; đây là quy tắc vận hành.
