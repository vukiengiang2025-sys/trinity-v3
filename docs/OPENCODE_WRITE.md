# CONTROLLED WRITE AGENT — TRINITY v3.1

## 1. Mục tiêu

`trinity-write` là agent OpenCode riêng, chỉ dùng trong workspace thử nghiệm
để chứng minh Trinity có thể điều phối một tác vụ sửa mã có kiểm soát.

Nó **không** thay thế hoặc mở rộng quyền của `trinity-readonly`.

## 2. Cấu hình

Fixture:

- `tests/fixtures/opencode_write/opencode.json`
- `tests/fixtures/opencode_write/baseline.json`
- `tests/fixtures/opencode_write/APPROVAL.md`

Agent `trinity-write`:

- `mode: primary`
- đọc/glob/grep/list trong workspace;
- edit trong workspace;
- bash chỉ cho:
  - `git status*`
  - `git diff*`
  - `git log*`
  - `python3 -m unittest*`
  - `python3 -m py_compile*`
- deny: `external_directory`, `.env`, `/etc`, `/home`, `/root`, `/proc`, `/sys`,
  `webfetch`, `websearch`, `task`, `skill`, `todowrite`, `question`, browser,
  và mọi bash command khác.

Cấu hình OpenCode tương ứng:

```yaml
adapters:
  opencode:
    agent: trinity-readonly
    write_agent: trinity-write
    require_explicit_agent: true
    standalone: true
```

## 3. Approval

Controlled write yêu cầu **hai cờ**:

```bash
python3 trinity.py run --write --approve "sửa lỗi python trong workspace"
```

- `--write` một mình → `WAITING_APPROVAL`, không chạy adapter.
- `--approve` một mình không bật write.
- `--write --approve` mới dispatch `code_write`.
- Resume dùng plan đã lưu; vẫn cần `resume <id> --approve`.

Đây là approval theo task/plan, chưa phải approval theo từng lệnh shell. Các
lệnh shell vẫn bị giới hạn bởi permission của agent và Guard của Trinity.

## 4. Live evidence

Sandbox `/tmp/trinity-write-sandbox`:

| Mục | Giá trị |
|---|---|
| Approval-only task | `TRN-20261104-164146-ac673b` — `WAITING_APPROVAL`, không chạy |
| Live write task | `TRN-20261104-164146-bfed3b` — `COMPLETED` |
| Thay đổi | `calc.py` thêm `subtract`; `test_calc.py` thêm test |
| Test gate | `python3 -m unittest` — 2/2 PASS |
| Workspace ngoài | Không đổi |
| `trinity-readonly` | Không bị ảnh hưởng |

Sau test, sandbox được khôi phục bằng `git checkout -- calc.py test_calc.py`
trong **thư mục tạm**, không dùng `reset --hard`/`clean` trên repo chính.

## 5. Giới hạn

- `trinity-write` mới được kiểm chứng trên sandbox, chưa phải production.
- Permission của OpenCode không phải OS sandbox; agent vẫn có thể làm bất cứ
  gì trong quyền `edit`/`bash` được cấp.
- Approval theo task chưa phân biệt từng hành động nguy hiểm; cần approval
  cấp hành động nếu mở rộng quyền.
- Không dùng trên repository có dữ liệu người dùng nếu chưa có backup/OS
  sandbox và review diff.
- Không bật cho `build/general` hay agent production.
