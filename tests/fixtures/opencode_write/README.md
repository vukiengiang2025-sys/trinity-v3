# Controlled-write OpenCode fixture

`opencode.json` defines the project-local `trinity-write` agent used only in a
disposable sandbox. It is **not** a recommendation to enable write access on a
real repository.

It allows:
- read/glob/grep/list inside the workspace;
- edit inside the workspace;
- `git status`, `git diff`, `git log`;
- `python3 -m unittest*` and `python3 -m py_compile*`.

It denies:
- outside-directory access and `.env`/system paths;
- every other bash command;
- network, task, skill, todo, question and browser tools.

Trinity still requires `run --write --approve`; approval is scoped to the task
plan and the write agent is selected explicitly.
