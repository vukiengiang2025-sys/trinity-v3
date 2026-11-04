# HERMES / OPENCLAW INTEGRATION STATUS

## 1. Hermes — BRAIN

| Mục | Trạng thái |
|---|---|
| Package metadata | `hermes-agent` 0.19.0 (PyPI) available |
| Installed | **NO** |
| CLI discovery | UNAVAILABLE |
| Memory path | Chưa xác định; không mặc định `~/.hermes/memories` |
| Adapter | CODE VERIFIED + MOCK VERIFIED |
| Live integration | **NOT VERIFIED** |

Không cài theo quy tắc an toàn hiện tại (`Không tự cài Hermes/OpenClaw`).
Không có API key/model configuration được cung cấp. Không đọc/ghi memory của
người dùng.

Khi operator cài và xác minh CLI, cấu hình Trinity:

```yaml
adapters:
  hermes:
    binary: hermes
    timeout_seconds: 300
    memory_path: null              # chỉ đặt sau khi xác minh
    memory_search_command: []      # ví dụ ["hermes", "memory", "search", "{query}"]
    memory_store_command: []       # ví dụ ["hermes", "memory", "store", "{prompt}"]
```

Chỉ khi `hermes --version` và `hermes --help` xác nhận memory/skill thì adapter
mới chuyển `AVAILABLE` và chỉ chạy command template đã cấu hình.

## 2. OpenClaw — MUSCLE

| Mục | Trạng thái |
|---|---|
| Package metadata | `openclaw` 2026.10.10 (npm), multi-channel AI gateway |
| Installed | **NO** |
| Gateway | Không chạy/không cấp quyền |
| Adapter | CODE VERIFIED + MOCK VERIFIED |
| Live integration | **NOT VERIFIED** |

Không cài, không khởi động gateway, không cấu hình channel/credential. OpenClaw
là gateway đa kênh, cần auth; health check không chứng minh khả năng thực thi.

Khi operator cài và xác minh CLI/gateway, cấu hình:

```yaml
adapters:
  openclaw:
    binary: openclaw
    timeout_seconds: 300
    gateway_enabled: false          # chỉ bật khi có auth/health đã xác minh
    gateway_url: "http://127.0.0.1:18789"
    invoke_prefix: []               # ví dụ ["openclaw", "exec"], phải xác minh
    command_allowlist:
      - git
      - ls
      - pwd
      - cat
      - grep
      - find
```

Adapter chỉ chạy argv tường minh qua Guard + allowlist; không tự sinh command
từ prompt, không dùng shell=True.

## 3. Gate để chuyển LIVE VERIFIED

- Cài trong môi trường cô lập (không đè bản có sẵn).
- Xác minh version/help/health bằng lệnh thật.
- Dùng workspace giả, không dữ liệu người dùng.
- Chạy một request đọc/vô hại, kiểm tra output, timeout, process cleanup.
- Kiểm tra audit, DB, workspace/Git trước-sau.
- Chỉ sau đó mới đánh dấu adapter `LIVE VERIFIED`.

Không tuyên bố Trinity P1 hoàn tất khi Hermes/OpenClaw chưa qua gate này.
