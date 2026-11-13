# Termind

A cross-platform, multi-provider autonomous AI engineering assistant for your terminal (**Linux**, **Windows**, and **macOS**). Built with a hybrid native engine architecture to preserve genuine Chain-of-Thought (reasoning) streams, enforce workspace security sandboxing, and provide live Markdown rendering.

---

## Key Features

* **Hybrid Multi-Provider Engine:**
  * **Google Gemini:** Runs on the native `google-genai` SDK with manual tool orchestration, full thought streaming (`part.thought`), model-aware `ThinkingConfig`, and `thought_signature` preservation.
  * **OpenAI, DeepSeek, Groq, Ollama, OpenRouter:** Runs via the `openai` SDK with native support for `reasoning_content` (preserved across multi-turn DeepSeek tool chains) and inline `<think>...</think>` streaming.
  * **Anthropic (Claude):** Runs on the native `anthropic` SDK with Extended Thinking (`thinking_delta`, preserved `thinking` signatures and `redacted_thinking` blocks), multi-turn tool formatting, and vision support.
* **Autonomous Developer Toolset:**
  * `list_directory` & `read_file` — Directory listing and lazy line-paginated reading with automatic binary and size protection.
  * `edit_file_replace` — Surgical search-and-replace editing with uniqueness checks, line-ending preservation (`LF`/`CRLF`), and colored diff previews.
  * `write_file` & `delete_file` — Safe file creation, overwriting, and deletion.
  * `search_code` — Symlink-safe recursive grep across workspace text files (skips binaries, oversized files, and build/dependency directories).
  * `execute_shell_command` — Live-streaming terminal execution (Bash/Zsh on POSIX, UTF-8 PowerShell on Windows) with 180s process-tree timeout enforcement.
  * `view_image` — Autonomous visual inspection of local images (`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, `.bmp`) strictly using the active provider.
  * `fetch_webpage_content` — Extracts clean text from public documentation and web links with SSRF and payload-size protections.
* **Security & Guardrails:**
  * **Workspace Sandbox:** Strict path validation using `os.path.realpath` and `os.path.commonpath` across all file and search tools to block directory traversal, symlink escapes, and cross-drive access.
  * **SSRF Protection:** Blocks non-HTTP(S) schemes and resolves hostnames to reject loopback, private, and link-local IP ranges.
  * **Command Blacklist:** Regex filtering against destructive OS commands.
  * **Human-in-the-Loop (HITL):** Prompts `[y/N]` before file modifications or shell execution (can be toggled with `/yolo`).
* **Terminal Experience:**
  * Real-time Markdown rendering and syntax highlighting via `rich`.
  * Persistent command history (`~/.universal_agent_history`) via `readline` / `pyreadline3`.
  * Multiline paste mode (`"""`) and smart `/file` path parsing (supports quotes, spaces, and drag-and-drop escapes).
  * Automatic project rules loading from `.agentrules`, `.cursorrules`, or `PROJECT.md`.
  * Per-turn token usage reporting, graceful `Ctrl+C` turn interruption with history rollback, and exponential backoff retries on `429` / `503` / `529` errors.

---

## Supported Providers

| Provider | Engine | Default Model | Environment Variable |
| :--- | :--- | :--- | :--- |
| `gemini` | `google-genai` | `gemini-3.8-flash` | `GEMINI_API_KEY` |
| `openai` | `openai` | `gpt-4o` | `OPENAI_API_KEY` |
| `deepseek` | `openai` | `deepseek-flash` | `DEEPSEEK_API_KEY` |
| `claude` | `anthropic` | `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| `groq` | `openai` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| `ollama` | `openai` | `qwen2.5-coder:latest` | *(Local, no key needed)* |
| `openrouter` | `openai` | `anthropic/claude-sonnet-4.6` | `OPENROUTER_API_KEY` |

---

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/Arta48/termind.git
   cd termind
   ```

2. **Create and activate a virtual environment (recommended):**

   **Linux / macOS:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

   **Windows (PowerShell):**
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

---

## Configuration (API Keys)

Set environment variables for the providers you plan to use:

**Linux / macOS:**
```bash
export GEMINI_API_KEY="your_gemini_key"
export OPENAI_API_KEY="your_openai_key"
export DEEPSEEK_API_KEY="your_deepseek_key"
export ANTHROPIC_API_KEY="your_anthropic_key"
export GROQ_API_KEY="your_groq_key"
export OPENROUTER_API_KEY="your_openrouter_key"
```

**Windows (PowerShell):**
```powershell
$env:GEMINI_API_KEY="your_gemini_key"
$env:OPENAI_API_KEY="your_openai_key"
$env:DEEPSEEK_API_KEY="your_deepseek_key"
```

*(Alternatively, you can configure API keys and custom endpoints dynamically at runtime using `/key` and `/base_url`.)*

---

## Usage

Run Termind from inside the project directory you want it to work in (this directory becomes the sandbox root):

```bash
python termind.py
```

### Interactive Commands

| Command | Description |
| :--- | :--- |
| `/provider <name>` | Switch active provider (`gemini`, `openai`, `deepseek`, `claude`, `groq`, `ollama`, `openrouter`). |
| `/model <name>` | Switch model while preserving conversation memory and compacted summary. |
| `/thinking <level>` | Set reasoning depth: `off`, `low`, `medium`, `high` (preserves conversation memory). |
| `/key <api_key>` | Set a per-provider API key override for the current session (use `/key reset` to clear). |
| `/base_url <url>` | Set a per-provider custom endpoint URL (use `/base_url reset` to clear). |
| `/yolo` | Toggle auto-approval mode (skips `[y/N]` confirmation prompts). |
| `/file <path> [prompt]` | Attach a local image (Vision) or text/source file directly to your message. |
| `/compact` | Summarize the current session (without tool calls) and reset history while preserving the summary. |
| `/reset` | Clear the entire conversation memory and session summary. |
| `/help` | Display the list of interactive commands. |
| `"""` or `'''` | Enter multiline paste mode (end with `"""` on a new line to submit, or `Ctrl+C` to cancel). |
| `exit` / `quit` | Terminate the agent session. |

---

## Project-Specific Rules (Optional)

If your workspace contains a `.agentrules`, `.cursorrules`, or `PROJECT.md` file, the agent automatically loads up to 4,000 characters of its contents into the system prompt at startup.

Example `.agentrules`:
```text
- This is a Java 21 project built with Gradle.
- Always run `./gradlew test` after modifying `.java` files.
- Do not modify anything inside the `vendor/` directory.
```

---

## Security Notes

* **Workspace Isolation:** File and search tools cannot access paths outside the directory from which `termind.py` was started, including via `../` traversal or symbolic links.
* **Active-Provider Privacy:** `view_image` always uses your currently selected provider and never falls back to external cloud vision APIs behind your back.
* **Shell Execution & YOLO Mode Caution:** Enabling `/yolo` allows the agent to run shell commands and modify or delete files inside the workspace without asking `[y/N]`. Note that shell commands inherit the process environment and are not OS-containerized—use `/yolo` only in trusted workspaces backed by version control.
* **Protect Your Keys:** Never commit API keys or `.env` files to public repositories.
