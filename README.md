# Agora

Agora is a local, two-model workspace where Claude Code and Codex work on the same question until they produce a shared statement. It is designed for collaborative inquiry rather than model-versus-model scoring.

## Requirements

- Python 3.8 or newer
- The `claude` and `codex` CLIs installed and authenticated
- No Python packages to install

## Run

```bash
python3 agora.py
```

Agora opens on `http://127.0.0.1:8765`. Useful options:

```bash
python3 agora.py --mock        # try the interface without using either model
python3 agora.py --port 9000   # choose a port
python3 agora.py --no-browser  # do not open a browser window
```

## Features

- **Solution focus** (default): both models act as a team, improving one practical shared answer.
- **Debate focus**: both models directly stress-test the draft when rigorous opposition is useful.
- **Response style**: choose everyday conversation or philosophical analysis.
- **Image analysis**: attach up to six PNG, JPEG, GIF, or WebP images/photos for both models to examine.
- **Voice**: turn on the browser's built-in speech voice to hear new replies aloud.
- **Session exports**: completed sessions are saved as Markdown in `agora_sessions/` and can be downloaded from the app.

The server listens only on your local machine. Attached images are held in a temporary local sandbox for the running app and are removed when it closes; exported session Markdown retains only their filenames.

## Optional CLI overrides

Prompts are sent to the CLIs on standard input. Override their commands or the per-turn timeout when needed:

```bash
AGORA_CLAUDE_CMD="claude -p" \
AGORA_CODEX_CMD="codex exec --skip-git-repo-check --sandbox read-only --output-last-message {outfile} -" \
AGORA_TIMEOUT=420 \
python3 agora.py
```
