# Available Tools

This document describes the tools available to miniclaw.

## File Operations

### read_file
Read the contents of a file.
```
read_file(path: str) -> str
```

### write_file
Write content to a file (creates parent directories if needed).
```
write_file(path: str, content: str) -> str
```

### edit_file
Edit a file by replacing specific text.
```
edit_file(path: str, old_text: str, new_text: str) -> str
```

### list_dir
List contents of a directory.
```
list_dir(path: str) -> str
```

## Shell Execution

### exec
Execute a shell command and return output.
```
exec(command: str, working_dir: str = None) -> str
```

**Safety Notes:**
- Commands have a configurable timeout (default 60s)
- Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)
- Output is truncated at 10,000 characters
- Optional `restrictToWorkspace` config to limit paths

## Web Access

### web_search
Search the web using Brave Search API.
```
web_search(query: str, count: int = 5) -> str
```

Returns search results with titles, URLs, and snippets. Requires `tools.web.search.apiKey` in config.

### web_fetch
Fetch and extract main content from a URL.
```
web_fetch(url: str, extractMode: str = "markdown", maxChars: int = 50000) -> str
```

**Notes:**
- Content is extracted using readability
- Supports markdown or plain text extraction
- Output is truncated at 50,000 characters by default

## Browser Automation (Optional)

### browser
Control a headless Chromium browser (requires Playwright).
```
browser(action: str, ...) -> str
```

**Common actions:**
- `navigate`, `click`, `type`, `hover`, `scroll`, `select`, `drag`, `file_upload`
- `get_snapshot` (returns `[ref=N]` targets), `screenshot`, `get_content`
- `cookies_get/set/clear`, `storage_get/set`, `set_geolocation`, `set_viewport`
- `new_tab`, `close_tab`, `list_tabs`, `switch_tab`, `close`

Use `[ref=N]` from `get_snapshot` for reliable targeting:
```
browser(action="get_snapshot")
browser(action="click", selector="[ref=12]")
```

## Memory Search

### memory_search
Search memory files for relevant past context (BM25 + optional embeddings).
```
memory_search(query: str, max_results: int = 5) -> str
```

## Communication

### message
Send a message to the user (used internally).
```
message(content: str, channel: str = None, chat_id: str = None) -> str
```

## Background Tasks

### spawn
Spawn a subagent to handle a task in the background.
```
spawn(task: str, label: str = None) -> str
```

Use for complex or time-consuming tasks that can run independently. The subagent will complete the task and report back when done.

## Scheduled Reminders (Cron)

### cron
Schedule reminders or tasks directly via the cron tool.
```
cron(action: "add"|"list"|"remove", message: str, kind: "reminder"|"task", ...) -> str
```

Examples:
```
cron(action="add", message="Stand up and stretch", every_seconds=3600, kind="reminder")
cron(action="add", message="Summarize today’s notes", cron_expr="0 20 * * *", kind="task", isolated=true)
```

Use the `exec` tool to create scheduled reminders with `miniclaw cron add`:

### Set a recurring reminder
```bash
# Every day at 9am
miniclaw cron add --name "morning" --message "Good morning!" --cron "0 9 * * *"

# Every 2 hours
miniclaw cron add --name "water" --message "Drink water!" --every 7200
```

### Set a one-time reminder
```bash
# At a specific time (ISO format)
miniclaw cron add --name "meeting" --message "Meeting starts now!" --at "2025-01-31T15:00:00"
```

### Manage reminders
```bash
miniclaw cron list              # List all jobs
miniclaw cron remove <job_id>   # Remove a job
```

## Heartbeat Task Management

The `HEARTBEAT.md` file in the workspace is checked every 30 minutes.
Use file operations to manage periodic tasks:

### Add a heartbeat task
```python
# Append a new task
edit_file(
    path="HEARTBEAT.md",
    old_text="## Example Tasks",
    new_text="- [ ] New periodic task here\n\n## Example Tasks"
)
```

### Remove a heartbeat task
```python
# Remove a specific task
edit_file(
    path="HEARTBEAT.md",
    old_text="- [ ] Task to remove\n",
    new_text=""
)
```

### Rewrite all tasks
```python
# Replace the entire file
write_file(
    path="HEARTBEAT.md",
    content="# Heartbeat Tasks\n\n- [ ] Task 1\n- [ ] Task 2\n"
)
```

## Lifecycle Hooks

MiniClaw can run optional lifecycle hooks from your workspace.

- Configure in `config.hooks`:
  - `enabled` (default `false`)
  - `path` (default `workspace/hooks`)
  - `configFile` (default `hooks.json`)
  - `timeoutSeconds` (default `8`)
  - `safeMode` (default `true`)
  - `allowCommandPrefixes` (optional allowlist)
  - `denyCommandPatterns` (dangerous command denylist)
- Hook config file path: `<workspace>/<hooks.path>/<configFile>`
- Supported events:
  - `SessionStart`
  - `SessionEnd`
  - `PreToolUse`
  - `PostToolUse`
  - `PreCompact`
  - `Stop`

Example `hooks.json`:
```json
{
  "SessionStart": [{"command": "echo session-start"}],
  "PreToolUse": [
    {"command": "workspace/hooks/check_tool.sh", "matchers": ["exec", "write_*"]}
  ],
  "PostToolUse": [{"command": "echo tool-finished"}]
}
```

Notes:
- Hooks run as shell commands with working directory set to the workspace.
- Hook payload is available in `MINICLAW_HOOK_PAYLOAD` (JSON string).
- PreToolUse hooks can block a tool by exiting non-zero.
- Hook failures are logged and do not crash the agent loop.
- With `safeMode=true`, deny-pattern matches are blocked before execution.

---

## Adding Custom Tools

To add custom tools:
1. Create a class that extends `Tool` in `miniclaw/agent/tools/`
2. Implement `name`, `description`, `parameters`, and `execute`
3. Register it in `AgentLoop._register_default_tools()`
