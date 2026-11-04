# OpenCode read-only baseline fixture

This directory freezes the `trinity-readonly` project-local agent that was
used for the v3.1 end-to-end validation.

- `opencode.json` is a **project-local** test config. Do not copy it into the
  global OpenCode config or enable its agent for production edits.
- `baseline.json` records the OpenCode version, config hash, permission-test
  result and Task ID used as the regression reference.
- The fixture contains no credentials, provider keys or user data.

`tests/test_opencode_baseline.py` verifies the hash and required deny rules.
