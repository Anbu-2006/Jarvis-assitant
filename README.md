# JARVIS

> [!IMPORTANT]
> **UNDER CONSTRUCTION** — This project is currently in active development.

**Just A Rather Very Intelligent System.**

A voice-first AI assistant that runs on your Windows PC. Talk to it, and it talks back — with a British accent, dry wit, and an audio-reactive particle orb straight out of the MCU.

JARVIS can browse the web, build entire projects via your IDE, see your screen, take notes, and plan your day — all through natural voice conversation. **100% free-tier** — no credit card required.

> "Will do, sir."

<!-- TODO: Add demo GIF or screenshot here -->
<!-- ![JARVIS Demo](docs/demo.gif) -->

---

## What It Does

- **Voice conversation** — speak naturally, get spoken responses with a JARVIS voice
- **Builds software** — say "build me a landing page" and JARVIS prepares the project for your IDE
- **Reads your calendar** — "What's on my schedule today?"
- **Reads your email** — "Any unread messages?" (read-only, by design)
- **Browses the web** — "Search for the best restaurants in Austin"
- **Manages tasks** — "Remind me to call the client tomorrow"
- **Takes notes** — "Save that as a note"
- **Remembers things** — "I prefer React over Vue" (it remembers next time)
- **Plans your day** — combines calendar, tasks, and priorities into a plan
- **Sees your screen** — knows what apps are open for context-aware responses
- **Audio-reactive orb** — a Three.js particle visualization that pulses with JARVIS's voice

## Requirements

- **Windows 10/11**
- **Python 3.11+**
- **Node.js 18+**
- **Google Chrome** (required for Web Speech API)
- **NVIDIA API key** — powers the DeepSeek V4 Flash brain ([get one here](https://build.nvidia.com/))
- **Windows Terminal** (optional but recommended)

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/yourusername/jarvis.git
cd jarvis

# 2. Set up environment
cp .env.example .env
# Edit .env — add your NVIDIA_API_KEY (required)

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install frontend dependencies
cd frontend && npm install && cd ..

# 5. Generate SSL certificates (needed for secure WebSocket)
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=localhost'

# 6. Start the backend (Terminal 1)
python server.py

# 7. Start the frontend (Terminal 2)
cd frontend && npm run dev

# 8. Open Chrome
start http://localhost:5173
```

Click the page once to enable audio, then speak. JARVIS will respond.

## Configuration

Edit your `.env` file:

```env
# Required — NVIDIA API key
NVIDIA_API_KEY=your-nvidia-api-key-here

# Optional — your name (JARVIS will address you personally)
# USER_NAME=Tony
# HONORIFIC=sir
```

## Architecture

```text
Microphone -> Web Speech API -> WebSocket -> FastAPI -> Dual-Path Inference -> Edge-TTS -> Speaker
                                                  |                  |
                                         Fast-Path (0ms)       Slow-Path (Complex)
                                         MCP Tools (47+)       Ollama Llama 3.2 (Local)
                                         Instant Cache         NVIDIA NIM (Fallback)
```

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI + Python (`main.py`, ~4000 lines) |
| Frontend | Vite + TypeScript + Three.js + Brutalist HUD |
| Communication | WebSocket (JSON messages + binary audio) |
| Inference Core | Dual-Path Engine (Local Ollama Llama 3.2 + NVIDIA Fallback) |
| System Control| MCP Server (47+ Native OS Automation Tools) |
| TTS | Edge-TTS (free, high-quality neural voices) |
| Memory | SQLite with FTS5 Full-Text Search |

## How the Zero-Latency Loop Works

1. You speak into your microphone.
2. Web Speech API transcribes your speech in real-time.
3. The transcript is sent to the server via WebSocket.
4. JARVIS evaluates intent with **Dual-Path Routing**:
   - **Instant Cache (0ms):** Matches 50+ common phrases ("yo", "how are you") directly to predefined responses.
   - **Fast-Path MCP (0ms):** Intercepts OS commands (weather, brightness, volume, ping, calculator, app launch) and executes Python tools instantly without touching the LLM.
   - **Local Inference:** Routes complex conversational queries to a local **Ollama (Llama 3.2)** instance with heavily pruned context for lightning-fast replies on consumer GPUs (e.g., RTX 3050).
5. Edge-TTS converts the response to speech with the JARVIS voice.
6. Audio streams back to the browser via WebSocket and the Three.js orb deforms to the audio.
7. **Barge-in:** If you interrupt JARVIS while he is speaking, playback instantly stops and he listens to your new command.

## Key Files

| File | Purpose |
|------|---------|
| `jarvis/api/main.py` | Main server — WebSocket handler, Fast-path logic, and Instant Cache |
| `jarvis/core/llm_router.py` | Hybrid local/cloud routing engine with extreme context pruning |
| `jarvis/mcp_server.py` | Native Windows OS automation tools (47+ professional-grade tools) |
| `jarvis/mcp_client.py` | Bridge executing MCP tools in-memory for zero latency |
| `frontend/src/orb.ts` | Three.js particle orb visualization |
| `frontend/src/voice.ts` | Web Speech API + audio playback + **Barge-in** |
| `frontend/src/settings.ts` | Brutalist UI for API Key management and diagnostic HUD |

## Features in Detail

### Zero-Latency Local SLM Engine
JARVIS was specifically re-architected to run locally on an RTX 3050. By aggressively pruning context, limiting token generation to 150 words, and bypassing expensive OS system calls during local inference, JARVIS achieves near-instantaneous response times using Ollama (`llama3.2:3b`).

### Extreme OS Automation (47+ MCP Tools)
The Model Context Protocol (MCP) server replaces fragile scripts with robust Python libraries. JARVIS can instantly:
- Control system hardware (Brightness, Volume, Power states, Ping, Disk Usage)
- Manage files and workspaces (Create, Read, Search codebase)
- Interact with applications (Open, Close, List active windows, Spotify control)
- Compute and connect (Web search, Weather, Timers, Notifications, Unit conversion)

### Brutalist HUD & Settings
A built-in frontend settings panel allows you to hot-swap API keys, monitor system health, check active tasks, and view live memory fragmentation stats without touching configuration files.

### Action System
JARVIS uses action tags to trigger deep system actions:
- `[ACTION:PROMPT_PROJECT]` — Connects to an existing code project
- `[ACTION:BUILD]` — Prepares a project workspace for your IDE
- `[ACTION:RESEARCH]` — Deep web research with an HTML report
- `[ACTION:ADD_TASK]` — Creates a tracked task with priority and due date
- `[ACTION:REMEMBER]` — Stores a fact for future context

### Memory System
JARVIS remembers things you tell it using SQLite with FTS5 full-text search. Preferences, decisions, and facts persist across sessions.

### Calendar & Mail
Calendar and Mail are currently stubbed on Windows (return empty data). They can be wired to Outlook COM automation or Microsoft Graph API for full functionality.

### Notes
Notes are stored as markdown files in `data/notes/`. You can create, search, and read notes through voice commands. Notes persist across sessions.

## Contributing

Contributions are welcome. Some areas that could use work:

- **Outlook integration** — wire calendar_access.py and mail_access.py to Outlook COM or Graph API
- **Additional LLM providers** — add more free providers to the router
- **Mobile client** — a companion app for voice interaction on the go
- **Plugin system** — make it easy to add new actions and integrations
- **Linux support** — adapt the PowerShell calls to bash equivalents

Please open an issue before submitting large PRs so we can discuss the approach.

## License

Free for personal, non-commercial use. Commercial use requires a license — visit [ethanplus.ai](https://ethanplus.ai) for inquiries. See [LICENSE](LICENSE) for details.

## Credits

Built by [Ethan](https://ethanplus.ai).

Powered by [NVIDIA NIM](https://build.nvidia.com/) — DeepSeek V4 Flash.

Inspired by the AI that started it all — Tony Stark's JARVIS.

> **Disclaimer:** This is an independent fan project and is not affiliated with, endorsed by, or connected to Marvel Entertainment, The Walt Disney Company, or any related entities. The JARVIS name and character are property of Marvel Entertainment.
