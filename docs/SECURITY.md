# SECURITY — TRINITY v3.1

## 1. Mô hình mối đe dọa

Trinity chạy trên máy cá nhân, với quyền của người dùng hiện tại. Nó không phải sandbox, không chạy với quyền root và không thiết kế cho multi-tenant.

Tài sản cần bảo vệ:

- API key/token của OpenCode, Hermes, OpenClaw và GitHub.
- Dữ liệu người dùng trong workspace.
- Lịch sử Git (không bị reset/force push/amend ngoài ý muốn).
- SQLite và audit log (không chứa credentials).
- Cấu hình OpenCode/Hermes/OpenClaw hiện có.

Rủi ro chính:

1. Prompt độc hại cố chèn shell argument hoặc path traversal.
2. Agent hoặc lệnh hệ thống thực hiện hành động phá hủy ngoài workspace.
3. Vô tình commit `.env`, key, token.
4. Ghi đè lịch sử Git.
5. Rò rỉ secrets qua log, SQLite, stdout/stderr, audit.
6. Cạn RAM/disk hoặc chạy nhiều agent đồng thời trên Termux.
7. Adapter giả định sai khả năng của công cụ chưa cài đặt.

## 2. Ranh giới tin cậy

| Thành phần | Mức tin cậy | Ghi chú |
|---|---|---|
| `config/*.yaml` | Tin cậy cục bộ | Không chứa secrets; người dùng kiểm soát |
| Prompt người dùng | Không tin cậy | Được route, guard, adapter validate |
| Adapter bên thứ ba | Không tin cậy hoàn toàn | Chỉ gọi qua interface, timeout, argv |
| OpenCode/Hermes/OpenClaw nội bộ | Tin cậy có điều kiện | Có policy riêng; Trinity không sandbox bên trong |
| SQLite/audit | Dữ liệu nhạy cảm nhẹ | Đã redact, quyền 0600 |
| GitHub credential | Secret | Chỉ Git sử dụng; Trinity không đọc/in |

## 3. Kiểm soát đã triển khai

### 3.1 Workspace và đường dẫn

- Mọi path được `expanduser()` + `resolve(strict=False)`.
- Chỉ cho phép path nằm trong `workspace.allowed_roots`.
- Chặn `..`, absolute path ngoài root, symlink escape.
- Chặn thư mục nhạy cảm: `.git`, `.ssh`, `.gnupg`, `.aws`, `.kube`, `.docker`.
- Chặn file nhạy cảm theo tên: `.env*`, `*.pem`, `*.key`, `*.p12`, `id_rsa*`, `credentials*`, `secrets*`, `.netrc`, `.npmrc`, `.pypirc`,...
- Ghi file chỉ khi `guard.allow_write: true`.

### 3.2 Shell command

- Không dùng `shell=True`.
- Input dạng chuỗi bị chặn nếu chứa `; | & \` $ > < newline`; sau đó `shlex.split`.
- Executable phải là tên ngắn trong `guard.allowed_commands`; không cho path `/usr/bin/...`.
- Path trong argument được kiểm tra; option `-C`, `--output`, `--file`, `-o`, `-f`, `--git-dir`, `--work-tree` được kiểm tra path.
- Chặn `find -exec/-execdir/-delete/-ok`, `python3 -c`, `python3 -` và các module `-m` ngoài allowlist nhỏ.
- `python3`/`pytest` xếp risk `high` → cần approval (vì chúng có thể thực thi mã tùy ý).
- `subprocess` có timeout, `start_new_session=True`, kill process group, giới hạn output, `RLIMIT_CPU`/`RLIMIT_CORE` best effort.

### 3.3 Git safety

Chặn vĩnh viễn:

- `git reset`, `git clean`, `git filter-repo`, `git filter-branch`, `git gc`, `git prune`, `git update-ref`.
- `git push --force`, `-f`, `--force-with-lease`, `--force-if-includes`, `--mirror`, `--delete`, `+refspec`, `:refspec`.
- `git branch -d/-D`, `git commit --amend`.
- `git tag -d`, `git stash drop/clear`.
- `git remote add/remove/set-url/rename/prune`.
- Ghi `git config`; chỉ cho phép đọc (`--get`, `--list`,...).
- `git -c`, `--exec-path` (thay đổi hành vi).
- Git subcommand không nằm trong allowlist (ví dụ `git daemon`) bị từ chối.
- `checkout`, `switch`, `restore`, `rm` yêu cầu approval vì có thể làm mất thay đổi working tree.

`git push` thường vẫn cần approval vì là thao tác mạng/thay đổi remote. `git add .`/`-A` cần approval để tránh stage nhầm secrets. `git add <path>` cụ thể risk medium.

### 3.4 Phê duyệt

- Router trả `risk_level` và `approval_required` (tham khảo).
- Executor kiểm tra lại `guard.require_approval_for_risk`; **không tin router**.
- Risk cao nhưng thiếu `--approve` → `WAITING_APPROVAL`, không chạy adapter.
- `--approve` chỉ mở cổng approval; guard vẫn có thể deny lệnh. Nó **không** biến thao tác nguy hiểm thành an toàn.
- OpenClaw adapter yêu cầu `request.approved` cho lệnh risk cao và vẫn kiểm tra allowlist riêng.
- `resume` dùng Task ID cũ; không tạo task trùng. `claim_for_resume` là UPDATE có điều kiện `WHERE status='WAITING_APPROVAL'`, lưu `approved_by`, `approved_at`, `approval_scope` (hash plan), `resume_count`.
- Resume re-check profile, workspace đã lưu, Guard, Resource và adapter availability. Nếu chưa đủ điều kiện, task vẫn `WAITING_APPROVAL`.
- Task v3.0 thiếu `plan_json`/`workspace` bị từ chối resume thay vì đoán.

### 3.5 Tài nguyên

- RAM/disk preflight trước khi chạy.
- Không đọc được RAM → unknown + cảnh báo, không giả định 0.
- Ngưỡng riêng Linux/Termux.
- Một lock `fcntl` duy nhất; không chạy đồng thời.
- **Fail closed**: nếu `fcntl` thiếu, không phải POSIX, hoặc filesystem không hỗ trợ flock (shared storage Android), Trinity từ chối chạy. Không fallback sang chạy không khóa.
- Timeout bị clamp theo profile; process tree bị kill khi quá hạn.
- Output giới hạn để tránh flood bộ nhớ/DB.

### 3.6 Secrets và dữ liệu

- Không đọc file `.env`, credential, provider config.
- Không đọc/ghi API key của OpenCode.
- `redact()` xử lý: `password=`, `token=`, `api_key:`, `Bearer`, `sk-`, `ghp_`, `github_pat_`, `AKIA`, private key block, URL có user:pass.
- Prompt, result, error, event, audit detail đều đi qua redact + truncate trước khi lưu/in.
- SQLite không có cột credentials; audit JSONL cũng không.
- DB `0600`, thư mục `~/.trinity`/`logs` `0700`, audit `0600`.
- `config` CLI che key nhạy cảm và redact value trông giống secret.
- `.gitignore` loại trừ `.env`, key, credentials, DB runtime, logs.

### 3.7 Mạng

- Không bắt buộc Internet cho status/doctor/config/tasks.
- HTTP health chỉ cho loopback (`127.0.0.1`, `localhost`, `::1`), không gửi auth.
- Không tự mở server/gateway.
- OpenCode CLI có thể tự dùng mạng theo cấu hình của nó; Trinity không kiểm soát network của agent.

### 3.8 Adapter

- `discover()` chỉ tuyên bố `AVAILABLE` khi executable + version/help xác minh được (Hermes/OpenClaw cần thêm template/prefix đã cấu hình).
- Không đoán CLI.
- Không tạo memory directory.
- Không tự cài/gỡ/nâng cấp công cụ.
- Không sửa cấu hình OpenCode global.
- OpenCode **không dùng agent mặc định**: `agent: null` + `require_explicit_agent: true` → `DENIED`. Chỉ bật agent sau khi operator đọc permission bằng `opencode debug agents`.
- Error permission/approval từ OpenCode được đánh dấu `permission_required`; Trinity không dùng `--auto`.

## 4. Những gì Guard KHÔNG bảo vệ

Đây là các giới hạn có chủ đích, phải được hiểu rõ:

1. **Không sandbox agent.** OpenCode/Hermes/OpenClaw có công cụ và policy riêng. Nếu OpenCode được cấu hình quyền rộng, nó có thể làm nhiều hơn Guard của Trinity.
2. **Không kiểm tra nội dung file.** Guard chặn theo tên/path, không quét nội dung để tìm secret. File tên bình thường chứa token vẫn có thể bị đọc bởi lệnh được allowlist.
3. **Allowlist interpreter.** `python3 script.py`, `pytest`, `git` có thể thực thi logic tùy ý. Vì vậy chúng cần approval hoặc được xem là tin cậy cục bộ.
4. **TOCTOU path.** Guard kiểm tra path trước khi chạy; symlink/file có thể bị thay đổi giữa lúc kiểm tra và lúc thực thi. Không dùng Trinity trên workspace có người khác ghi đồng thời.
5. **TOCTOU Git.** Guard không khóa Git index/worktree. Người dùng có thể chạy Git song song ngoài Trinity.
6. **HTTP execution.** Chưa triển khai; health 200 không có nghĩa endpoint thực thi an toàn/tương thích.
7. **OpenClaw gateway auth.** Chưa có token/auth flow; chỉ health-check loopback. Không bật gateway execution khi chưa xác minh.
8. **Termux shared storage.** Quyền `0600` có thể không được honor trên một số filesystem dùng chung (FAT/exFAT). Ưu tiên bộ nhớ riêng của Termux.
9. **Full environment inheritance.** Adapter con nhận `os.environ` để hoạt động với cấu hình hiện có; Trinity không log/đọc giá trị, nhưng agent con có thể thấy biến môi trường của tiến trình cha.
10. **Không chống prompt injection từ nội dung file.** Nếu OpenCode đọc file độc hại, hành vi của nó do OpenCode quyết định.
11. **Không có built-in OpenCode agent read-only hoàn chỉnh.** `plan` deny edit nhưng wildcard vẫn cho shell; `architect` deny edit nhưng cho read/glob/grep/webfetch và shell Git read-only. Vì vậy không chạy prompt thật khi chưa có permission boundary đã chứng minh.
12. **Prompt resume lấy từ bản đã redact.** Nếu prompt ban đầu chứa secret, task resume sẽ dùng bản đã redact; đây là đánh đổi có chủ đích để không lưu secrets.

## 4.1 Findings v3.1

| Mức | Finding | Trạng thái |
|---|---|---|
| High | OpenCode có thể chạy shell/edit theo permission nội bộ; Guard không sandbox | **Chấp nhận là giới hạn kiến trúc**; default agent bị chặn |
| High | Không có OpenCode agent built-in vừa deny edit vừa deny shell | **Chưa chạy prompt thật**; chỉ fake-CLI test |
| Medium | Lock có thể fail-open nếu fcntl/FS không hỗ trợ | **Đã sửa**: fail closed + test |
| Medium | Resume tạo task mới, mất plan/approval history | **Đã sửa**: resume cùng Task ID, plan, scope |
| Medium | Hai tiến trình có thể resume cùng task | **Đã sửa**: conditional claim + lock + test |
| Medium | OpenCode default agent có quyền rộng | **Đã sửa**: yêu cầu `agent` tường minh |
| Low | JSON output chưa được kiểm tra hợp lệ | **Đã bổ sung** metadata `json_valid` |
| Low | Lỗi permission OpenCode không được phân loại | **Đã bổ sung** `permission_required` |
| Low | Chưa có migration DB v3.0 → v3.1 | **Đã sửa**: ALTER TABLE + test |

Residual risk: `--approve` + OpenCode agent có shell/edit vẫn có thể thay đổi ngoài workspace. Chỉ dùng agent đã kiểm tra permission và workspace tin cậy.

## 5. Quy tắc vận hành

- Luôn chạy `doctor` trước khi dùng lần đầu.
- Dùng `--dry-run` để xem plan trước khi chạy tác vụ sửa code/hệ thống.
- Chỉ dùng `--approve` sau khi đọc plan và hiểu phạm vi.
- Không chạy `trinity` bằng `sudo`.
- Không đặt secrets trong YAML, prompt, commit, issue, log.
- Không xóa `logs/.trinity.lock` khi tiến trình còn sống.
- Giữ quyền truy cập workspace ở mức tối thiểu.
- Kiểm tra `git diff --cached` trước khi commit.
- Không force push, không reset, không clean, không amend lịch sử.

## 6. Kiểm thử bảo mật đã chạy

Xem `docs/TEST_REPORT.md`. Các test bao gồm:

- Chặn `rm -rf /`, `sudo`, `mkfs`, `shutdown`, absolute executable.
- Chặn shell metacharacter và command injection.
- Chặn path traversal, absolute path ngoài workspace, symlink escape, `.env`, `.git/config`.
- Chặn force push, reset, clean, branch -D, commit --amend, filter-repo, đổi remote/config.
- Chặn `find -exec/-delete`, `python3 -c`, `python3 -m http.server`.
- Redaction prompt/result/error/event/audit.
- DB/audit permission `0600`.
- Adapter thiếu tool → UNAVAILABLE; OpenClaw thiếu guard/approval → DENIED.
- OpenCode thiếu agent tường minh → DENIED; permission error được đánh dấu.
- Resume cùng Task ID, lưu approval scope, atomic claim, chống chạy trùng.
- Resume sai profile/workspace/plan bị từ chối.
- `fcntl` không hỗ trợ/non-POSIX → fail closed.
- Migration DB v3.0 → v3.1.
- Dry-run không tạo DB/audit; resume dry-run không đổi task.

## 7. Báo cáo sự cố

Nếu phát hiện secret bị lộ:

1. Dừng sử dụng Trinity.
2. Thu hồi/rotate secret tại nhà cung cấp tương ứng.
3. Kiểm tra `logs/audit.jsonl`, SQLite (`tasks`, `task_events`), shell history, Git history.
4. Không commit bản sửa chứa secret; xóa khỏi working tree và lịch sử bằng quy trình riêng của người dùng (Trinity không tự viết lại lịch sử).
5. Ghi lại phạm vi ảnh hưởng và cập nhật test/redaction nếu cần.
