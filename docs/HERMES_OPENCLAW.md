# HERMES / OPENCLAW INTEGRATION STATUS

## 1. Hermes — BRAIN

| Mục | Trạng thái |
|---|---|
| Installed isolated | **YES** — `/tmp/trinity-hermes-venv` |
| Version | `hermes-agent 0.19.0` |
| HERMES_HOME | `/tmp/trinity-hermes-home` (không dùng home thật) |
| CLI discovery | **PASS** — `hermes --version`, `--help` |
| Memory/skills offline | **PASS** — `hermes memory status`, `hermes skills list` |
| Memory path | Built-in `MEMORY.md`/`USER.md` trong HERMES_HOME; không đọc home thật |
| Adapter discovery | `INSTALLED_BUT_UNVERIFIED` |
| Live reasoning | **NOT VERIFIED** — local Qwen 0.5B one-shot bị timeout |

Đã thử live:

```text
hermes -z "Say HERMES_OK" --provider openai-api --model local ...
```

với OpenAI-compatible local llama.cpp server. Kết quả: timeout 150s ở toolset
`memory`; không có output hợp lệ. Không dùng API trả phí, không dùng key người dùng.

Khi operator có model đủ mạnh (hoặc API key hợp lệ), cấu hình:

```yaml
adapters:
  hermes:
    binary: hermes
    timeout_seconds: 300
    memory_path: null
    memory_search_command: []
    memory_store_command: []
```

## 2. OpenClaw — MUSCLE

| Mục | Trạng thái |
|---|---|
| Installed isolated | **YES** — `/tmp/trinity-openclaw` |
| Version | `openclaw 2026.10.10` |
| CLI discovery | **PASS** — `openclaw --version`, `--help` |
| Agent command | `openclaw agent exec --isolated ...` available |
| Gateway | **NOT running / no auth** |
| Adapter discovery | `INSTALLED_BUT_UNVERIFIED` |
| Live reasoning | **NOT VERIFIED** — `agent exec` bị kill/timeout với local model |

Đã thử:

```text
openclaw agent exec --isolated --state-dir ... \
  --cwd ... --model openai/local --auth-env-only \
  --json "Reply with exactly: OPENCLAW_OK"
```

Kết quả: exit 137 (bị kill) / không có output. Không tự cấu hình gateway hay
channel; không dùng credential người dùng.

Khi operator có model/gateway phù hợp:

```yaml
adapters:
  openclaw:
    binary: openclaw
    gateway_enabled: true
    gateway_url: "http://127.0.0.1:18789"
    invoke_prefix: ["openclaw", "agent", "exec", "--isolated"]
    command_allowlist: []
```

## 3. Trạng thái tổng hợp

| Mức | Hermes | OpenClaw |
|---|---|---|
| DISCOVERED | ✅ | ✅ |
| AVAILABLE (CLI) | ✅ | ✅ |
| CONNECTED (live model/gateway) | ❌ | ❌ |
| INTEGRATED (adapter live) | ❌ | ❌ |
| VERIFIED | ❌ | ❌ |
| PRODUCTION_READY | ❌ | ❌ |

Trinity hiện chỉ có **OpenCode read-only + controlled-write** LIVE VERIFIED.
Ba-agent live vẫn chưa đạt; không tuyên bố P1 hoàn tất.

## 4. Cleanup / safety

- Model server và Ollama tạm đã dừng sau thử nghiệm.
- Các cài đặt nằm trong `/tmp`, không sửa global config.
- Không dùng API key, token, tài khoản hoặc dịch vụ trả phí.
- Không bật gateway/channel/automation.
