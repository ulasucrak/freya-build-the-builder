# Setup

Needs Python >= 3.10 (the system python3 is 3.9, so use Homebrew 3.12).

```sh
brew install python@3.12
/opt/homebrew/bin/python3.12 -m venv /Users/ulas/Desktop/Freya/.venv
/Users/ulas/Desktop/Freya/.venv/bin/pip install -r build-the-builder-v3/build-the-builder/requirements.txt
```

Environment (Freya keys; untracked, never commit or print):

```sh
source /Users/ulas/Desktop/Freya/build-the-builder-v3/build-the-builder/env.sh
```

Claude auth for the SDK: it reuses the logged-in Claude Code CLI (`~/.local/bin/claude`).
Check with `~/.local/bin/claude -p "say ok"`.

Quick read-only Freya check:

```sh
cd build-the-builder-v3/build-the-builder
/Users/ulas/Desktop/Freya/.venv/bin/python -c "import freya; print(len(freya.api('GET','/api/v2/workflow', workspaceId=freya.workspace_id()) or []))"
```

Tests (simulations: only one per workspace at a time):

```sh
/Users/ulas/Desktop/Freya/.venv/bin/python -m unittest discover -s tests
python3 tools/run_cases.py   # see README.md for arguments
```
