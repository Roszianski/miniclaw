<div align="center">
  <img src="miniclaw_logo.png" alt="miniclaw" width="500">
  <h1>miniclaw: Ultra-Lightweight Personal AI Assistant</h1>
  <p>
    <img src="https://img.shields.io/badge/python-≥3.11-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
</div>

🦀 **miniclaw** is an **ultra-lightweight** personal AI assistant.

⚡️ Delivers core agent functionality in just **10k+** lines of code.

## Key Features

🪶 **Ultra-Lightweight**: Small, readable codebase — easy to understand, modify, and extend.

🔬 **Research-Ready**: Clean code for rapid experimentation.

⚡️ **Lightning Fast**: Minimal footprint means faster startup and lower resource usage.

💎 **Easy-to-Use**: Simple setup — configure and go.

## Architecture

<p align="center">
  <img src="miniclaw_arch.png" alt="miniclaw architecture" width="800">
</p>

## Install

**Install from source** (recommended for development)

```bash
git clone https://github.com/your-org/miniclaw.git
cd miniclaw
pip install -e .
```

**With browser automation support:**

```bash
pip install -e ".[browser]"
playwright install chromium
```

## Quick Start

> [!TIP]
> Set your API key in `~/.miniclaw/config.json`.
> Get API keys: [OpenRouter](https://openrouter.ai/keys) (Global) · [DashScope](https://dashscope.console.aliyun.com) (Qwen) · [Brave Search](https://brave.com/search/api/) (optional, for web search)

**1. Initialize**

```bash
miniclaw onboard
```

`miniclaw onboard` now starts with `Guided Setup (Recommended)` or `Advanced Setup`, supports resume/non-interactive setup, guides Telegram/WhatsApp channel setup, includes `Skills Setup`, and can optionally download the default `whisper-small.en` model when Whisper local STT is selected.

**2. Configure** (`~/.miniclaw/config.json`)

For OpenRouter - recommended for global users:
```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx"
    }
  },
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5"
    }
  }
}
```

**3. Chat**

```bash
miniclaw agent -m "What is 2+2?"
```

That's it! You have a working AI assistant in 2 minutes.

## Local Models (vLLM)

Run miniclaw with your own local models using vLLM or any OpenAI-compatible server.

**1. Start your vLLM server**

```bash
vllm serve meta-llama/Llama-3.1-8B-Instruct --port 8000
```

**2. Configure** (`~/.miniclaw/config.json`)

```json
{
  "providers": {
    "vllm": {
      "apiKey": "dummy",
      "apiBase": "http://localhost:8000/v1"
    }
  },
  "agents": {
    "defaults": {
      "model": "meta-llama/Llama-3.1-8B-Instruct"
    }
  }
}
```

**3. Chat**

```bash
miniclaw agent -m "Hello from my local LLM!"
```

> [!TIP]
> The `apiKey` can be any non-empty string for local servers that don't require authentication.

## Chat Apps

Talk to your miniclaw through Telegram or WhatsApp — anytime, anywhere.

| Channel | Setup |
|---------|-------|
| **Telegram** | Easy (just a token) |
| **WhatsApp** | Medium (scan QR) |

<details>
<summary><b>Telegram</b> (Recommended)</summary>

**1. Create a bot**
- Open Telegram, search `@BotFather`
- Send `/newbot`, follow prompts
- Copy the token

**2. Configure**

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allowFrom": ["YOUR_USER_ID"]
    }
  }
}
```

> Get your user ID from `@userinfobot` on Telegram.

**3. Run**

```bash
miniclaw gateway
```

</details>

<details>
<summary><b>WhatsApp</b></summary>

Requires **Node.js ≥18**.

**1. Link device**

```bash
miniclaw channels login
# Scan QR with WhatsApp → Settings → Linked Devices
```

**2. Configure**

```json
{
  "channels": {
    "whatsapp": {
      "enabled": true,
      "bridgeUrl": "ws://127.0.0.1:3001",
      "bridgeHost": "127.0.0.1",
      "bridgeAuthToken": "set-a-random-secret",
      "allowFrom": ["+1234567890"]
    }
  }
}
```

**3. Run** (two terminals)

```bash
# Terminal 1
miniclaw channels login

# Terminal 2
miniclaw gateway
```

</details>

### Rich Chat Interactions

When using Telegram or WhatsApp channels, miniclaw supports:
- Typing indicators while a run is in progress.
- Reply-thread wiring (responses can reply to the triggering message when metadata is available).
- Session commands:
  - `/cancel` cancel the active run for the current session.
  - `/status` show model, active runs, cron status, and heartbeat status.
  - `/reset` clear the current session history.
  - `/think off|low|medium|high` set per-session thinking mode.

## Configuration

Config file: `~/.miniclaw/config.json`

### Providers

> [!NOTE]
> Voice/audio transcription uses this priority: local whisper.cpp (`whisper-cli`) if enabled and available, then optional Groq fallback.

| Provider | Purpose | Get API Key |
|----------|---------|-------------|
| `openrouter` | LLM (recommended, access to all models) | [openrouter.ai](https://openrouter.ai) |
| `anthropic` | LLM (Claude direct) | [console.anthropic.com](https://console.anthropic.com) |
| `openai` | LLM (GPT direct) | [platform.openai.com](https://platform.openai.com) |
| `deepseek` | LLM (DeepSeek direct) | [platform.deepseek.com](https://platform.deepseek.com) |
| `groq` | LLM + **Voice transcription** (Whisper) | [console.groq.com](https://console.groq.com) |
| `gemini` | LLM (Gemini direct) | [aistudio.google.com](https://aistudio.google.com) |
| `aihubmix` | LLM (API gateway, access to all models) | [aihubmix.com](https://aihubmix.com) |
| `dashscope` | LLM (Qwen) | [dashscope.console.aliyun.com](https://dashscope.console.aliyun.com) |

OpenAI and Anthropic support OAuth device flow in addition to API keys.


### Provider Auth

| Option | Default | Description |
|--------|---------|-------------|
| `providers.<name>.authMode` | `"api_key"` | Auth strategy: `"api_key"` or `"oauth"` (OAuth currently supported for `openai` and `anthropic`). |
| `providers.<name>.oauthTokenRef` | `""` | SecretStore token reference metadata. If empty, defaults to `oauth:<provider>:token`. |

OAuth CLI flow:

```bash
miniclaw auth login --provider openai
miniclaw auth status
miniclaw auth logout --provider openai
```

When `authMode=oauth`, miniclaw uses OAuth token from SecretStore. If OAuth is unavailable and `apiKey` is configured, miniclaw falls back explicitly to API-key auth.


### Transcription

| Option | Default | Description |
|--------|---------|-------------|
| `transcription.localWhisper.enabled` | `false` | Enable local whisper.cpp transcription (`whisper-cli`). |
| `transcription.localWhisper.cli` | `"whisper-cli"` | Whisper CLI binary name/path. |
| `transcription.localWhisper.modelPath` | `"~/.miniclaw/models/whisper-small.en.bin"` | Local Whisper model path. |
| `transcription.groqFallback` | `true` | If local Whisper is unavailable/fails, try Groq transcription (if configured). |

When local Whisper is enabled, `miniclaw doctor` validates that both the configured `whisper-cli` binary and model file are present.


### Skills & Secrets

- Skill requirements declared in `metadata.miniclaw.requires` are validated automatically (CLI + dashboard).
- `requires.env` keys are resolved from process env vars first, then SecretStore.
- SecretStore backend selection:
  - `MINICLAW_SECRETS_BACKEND=auto|keychain|file` (default `auto`)
  - `MINICLAW_SECRETS_MASTER_KEY=...` (optional master key for encrypted-file backend)
- Secrets are never written in plaintext to config; dashboard and API return masked presence only.

Dashboard skill secret endpoints:
- `GET /api/skills/{name}/secrets`
- `PUT /api/skills/{name}/secrets`


### Multi-Agent Routing

By default, miniclaw runs in single-agent mode. Multi-agent routing is opt-in.

| Option | Default | Description |
|--------|---------|-------------|
| `agents.instances` | `[]` | Optional list of agent instances. Max 3 total. If set, it must include an instance with `id: "default"`. |
| `agents.routing.rules` | `[]` | Ordered top-down match rules. First match wins and binds a session to that agent. Supported selectors: `channel`, `chatId`, `senderId`, `isGroup`. |

When routing is enabled, per-agent conversation state is isolated with session keys like `agent:<id>:<channel>:<chat_id>`.

Example:

```json
{
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5"
    },
    "instances": [
      { "id": "default" },
      { "id": "ops", "model": "openai/gpt-5-mini", "thinking": "low" }
    ],
    "routing": {
      "rules": [
        { "agent": "ops", "channel": "telegram", "isGroup": true }
      ]
    }
  }
}
```

## Workflow Recipes (Linear + DAG)

Workflow recipes live in your workspace by default:

- `workspace/workflows/*.yaml|*.yml|*.json`
- configurable via `workflows.path` in `~/.miniclaw/config.json`

Runtime supports:

- **Linear** mode: run steps in order.
- **DAG** mode: run independent steps in parallel, then merge on dependencies.

### Recipe Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | no | Recipe name (defaults to filename). |
| `mode` | no | `"linear"` or `"dag"`. If omitted and any step has `depends_on`, runtime treats recipe as DAG. |
| `max_parallel` | no | Max concurrent DAG steps (default `4`). |
| `steps` | yes | List of step objects. |
| `steps[].id` | no | Step ID (auto-generated if omitted). Must be unique. |
| `steps[].prompt` | yes | Prompt sent to the agent for that step. |
| `steps[].depends_on` | no | Step IDs this step depends on (`dependsOn` also supported). |
| `steps[].retry_max_attempts` | no | Retry count (default `1`). |
| `steps[].retry_backoff_ms` | no | Backoff between retries (default `750`). |
| `steps[].require_approval` | no | Require manual approval before step runs. |
| `steps[].on_failure` | no | `"stop"` or `"continue"` (default `"stop"`). |

Step outputs are available to later prompts as template vars:

- `{step_id_output}` (example: `{draft_output}`)
- `{workflow_name}`

### Linear Example

```yaml
name: publish-update
mode: linear
steps:
  - id: draft
    prompt: "Draft today's update from these notes: {notes}"
    retry_max_attempts: 2
    retry_backoff_ms: 1000

  - id: review
    prompt: "Review this draft and tighten it:\n\n{draft_output}"
    require_approval: true

  - id: publish
    prompt: "Post this final message:\n\n{review_output}"
```

### DAG Example (Parallel Branches + Merge)

```yaml
name: weekly-ops-brief
mode: dag
max_parallel: 4
steps:
  - id: collect
    prompt: "Collect key metrics for the last 7 days."

  - id: support
    prompt: "Summarize support issues from: {collect_output}"
    depends_on: [collect]

  - id: growth
    prompt: "Summarize growth metrics from: {collect_output}"
    depends_on: [collect]

  - id: finance
    prompt: "Summarize finance anomalies from: {collect_output}"
    depends_on: [collect]

  - id: merge
    prompt: |
      Build one concise brief from:
      support={support_output}
      growth={growth_output}
      finance={finance_output}
    depends_on: [support, growth, finance]
```

### DAG Behavior Notes

- Unknown/self/cyclic dependencies are rejected at recipe load time.
- If a dependency fails, downstream dependent steps are skipped with `dependency_failed`.
- `on_failure: stop` on a failed step stops remaining runnable work.
- `on_failure: continue` allows other independent branches to proceed.

### Run a Recipe

Dashboard API:

```bash
curl -X POST "http://localhost:18791/api/workflows/run" \
  -H "Authorization: Bearer <dashboard-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "recipe": "weekly-ops-brief",
    "vars": { "notes": "Top priorities this week..." },
    "channel": "dashboard",
    "chat_id": "workflow"
  }'
```


### Security

> [!TIP]
> For production deployments, enable sandboxing with `"sandbox": true` and keep `"restrictToWorkspace": true`.

| Option | Default | Description |
|--------|---------|-------------|
| `tools.sandbox` | `false` | Opt-in OS-level sandbox for shell exec. When enabled, miniclaw runs shell commands in a fail-closed wrapper (`sandbox-exec` on macOS, `unshare` on Linux). |
| `tools.exec.resourceLimits` | `{ cpuSeconds: 30, memoryMb: 512, fileSizeMb: 64, maxProcesses: 64 }` | Resource caps applied to shell execution via `ulimit` (CPU time, virtual memory, max file size, max processes). |
| `tools.restrictToWorkspace` | `false` | When `true`, restricts **all** agent tools (shell, file read/write/edit, list) to the workspace directory. Prevents path traversal and out-of-scope access. |
| `channels.*.allowFrom` | `[]` (allow all) | Whitelist of user IDs. Empty = allow everyone; non-empty = only listed users can interact. |

### Service

| Option | Default | Description |
|--------|---------|-------------|
| `service.enabled` | `false` | Desired service mode. Used by `miniclaw doctor` to validate service installation. |
| `service.autoStart` | `false` | If true, `miniclaw service install` enables auto-start behavior for your OS user service manager. |
| `service.logRetentionDays` | `60` | Log cleanup retention window in days (used by onboarding hardening step). |


## CLI Reference

| Command | Description |
|---------|-------------|
| `miniclaw onboard` | Run Guided/Advanced setup (channels, service, skills, readiness report) |
| `miniclaw agent -m "..."` | Chat with the agent |
| `miniclaw agent` | Interactive chat mode |
| `miniclaw gateway` | Start the gateway |
| `miniclaw status` | Show status |
| `miniclaw doctor [--fix] [--json]` | Run environment/runtime diagnostics |
| `miniclaw service install` | Install user service definition |
| `miniclaw service start` | Start installed user service |
| `miniclaw service stop` | Stop running user service |
| `miniclaw service status` | Show user service state |
| `miniclaw service uninstall` | Remove user service definition |
| `miniclaw auth login --provider openai\|anthropic` | Start OAuth login for provider |
| `miniclaw auth status` | Show OAuth/API-key auth status |
| `miniclaw auth logout --provider openai\|anthropic` | Remove OAuth token and switch provider authMode to `api_key` |
| `miniclaw channels login` | Link WhatsApp (scan QR) |
| `miniclaw channels status` | Show channel status |

<details>
<summary><b>User Service Management</b></summary>

Supports user-level services on:
- macOS: `launchd` (`~/Library/LaunchAgents`)
- Linux: `systemd --user` (`~/.config/systemd/user`)

```bash
# Install from current config (respects service.autoStart)
miniclaw service install

# Start/stop/status
miniclaw service start
miniclaw service stop
miniclaw service status

# Remove definition
miniclaw service uninstall
```

</details>

<details>
<summary><b>Scheduled Tasks (Cron)</b></summary>

```bash
# Add a job
miniclaw cron add --name "daily" --message "Good morning!" --cron "0 9 * * *"
miniclaw cron add --name "hourly" --message "Check status" --every 3600

# List jobs
miniclaw cron list

# Remove a job
miniclaw cron remove <job_id>
```

</details>

## Docker

> [!TIP]
> The `-v ~/.miniclaw:/root/.miniclaw` flag mounts your local config directory into the container, so your config and workspace persist across container restarts.

Build and run miniclaw in a container:

```bash
# Build the image
docker build -t miniclaw .

# Initialize config (first time only)
docker run -v ~/.miniclaw:/root/.miniclaw --rm miniclaw onboard

# Edit config on host to add API keys
vim ~/.miniclaw/config.json

# Run gateway (connects to Telegram/WhatsApp)
docker run -v ~/.miniclaw:/root/.miniclaw -p 18790:18790 -p 18791:18791 miniclaw gateway

# Or run a single command
docker run -v ~/.miniclaw:/root/.miniclaw --rm miniclaw agent -m "Hello!"
docker run -v ~/.miniclaw:/root/.miniclaw --rm miniclaw status
```

## Project Structure

```
miniclaw/
├── agent/          # Core agent logic
│   ├── loop.py     #    Agent loop (LLM <-> tool execution)
│   ├── context.py  #    Prompt builder
│   ├── memory.py   #    Persistent memory
│   ├── skills.py   #    Skills loader
│   ├── subagent.py #    Background task execution
│   └── tools/      #    Built-in tools (incl. spawn)
├── skills/         # Bundled skills (github, weather, tmux...)
├── channels/       # Telegram + WhatsApp integration
├── bus/            # Message routing
├── cron/           # Scheduled tasks
├── workflows/      # Linear + DAG workflow runtime
├── heartbeat/      # Proactive wake-up
├── providers/      # LLM providers (OpenRouter, etc.)
├── session/        # Conversation sessions
├── config/         # Configuration
└── cli/            # Commands
```

## License

MIT. See LICENSE file for details.

<p align="center">
  <sub>miniclaw is for educational, research, and technical exchange purposes only</sub>
</p>
