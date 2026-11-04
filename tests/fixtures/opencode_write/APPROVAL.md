# trinity-write approval policy

`trinity-write` is a controlled-write test agent, not a production agent.

Required:
1. Run only in a disposable workspace with a Git baseline.
2. Always invoke as `trinity run --write --approve`; `--approve` alone must not
   enable write mode.
3. Review the plan/diff before and after execution.
4. Run test gate (`python3 -m unittest`) and compare Git status.
5. Do not merge this config into the global OpenCode config.
6. Do not use it on repositories containing user data until an OS-level
   sandbox and independent approval are in place.
7. Roll back agent-created changes by restoring the disposable baseline; never
   use `git reset --hard` or `git clean` on the main repository.

The fixture freezes permissions and live evidence; update requires a new
approval record and commit.
