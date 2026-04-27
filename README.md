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

```
Microphone -> Web Speech API -> WebSocket -> FastAPI -> LLM Router -> Edge-TTS -> WebSocket -> Speaker
                                                |
                                                v
                                        DeepSeek V4 Flash (NVIDIA NIM)
                                                |
                                                v
                                        Windows Integration
                                        (PowerShell, file delegation, local files)
```

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI + Python (`server.py`, ~2500 lines) |
| Frontend | Vite + TypeScript + Three.js |
| Communication | WebSocket (JSON messages + binary audio) |
| AI | DeepSeek V4 Flash — ultra-fast reasoning with thinking |
| TTS | Edge-TTS (free, no API key) |
| System | PowerShell + file-based delegation for Windows |

## How the Voice Loop Works

1. You speak into your microphone
2. Chrome's Web Speech API transcribes your speech in real-time
3. The transcript is sent to the server via WebSocket
4. JARVIS detects intent — conversation, action, or build request
5. For actions: prepares project workspace or runs system commands
6. Generates a response via LLM router (auto-selects best available provider)
7. Edge-TTS converts the response to speech with the JARVIS voice
8. Audio streams back to the browser via WebSocket
9. The Three.js orb deforms and pulses in response to the audio
10. Background tasks notify you proactively when they complete

## Key Files

| File | Purpose |
|------|---------|
| `server.py` | Main server — WebSocket handler, LLM, action system |
| `llm_router.py` | Multi-provider LLM router with automatic failover |
| `frontend/src/orb.ts` | Three.js particle orb visualization |
| `frontend/src/voice.ts` | Web Speech API + audio playback |
| `frontend/src/main.ts` | Frontend state machine |
| `memory.py` | SQLite memory system with FTS5 full-text search |
| `calendar_access.py` | Calendar integration (stub on Windows) |
| `mail_access.py` | Mail integration (stub on Windows, read-only design) |
| `notes_access.py` | Local markdown notes in `data/notes/` |
| `actions.py` | System actions (Terminal, Browser, IDE delegation) |
| `browser.py` | Playwright web automation |
| `work_mode.py` | Project-focused work sessions |
| `screen.py` | Window detection + screenshots via PowerShell/Pillow |
| `planner.py` | Multi-step task planning with smart questions |

## Features in Detail

### Action System
JARVIS uses action tags to trigger real system actions:
- `[ACTION:BUILD]` — prepares a project workspace for your IDE
- `[ACTION:BROWSE]` — opens the browser to a URL or search query
- `[ACTION:RESEARCH]` — deep research with LLM, outputs an HTML report
- `[ACTION:PROMPT_PROJECT]` — connects to an existing project
- `[ACTION:ADD_TASK]` — creates a tracked task with priority and due date
- `[ACTION:REMEMBER]` — stores a fact for future context

### LLM Engine
JARVIS is powered by **DeepSeek V4 Flash** via **NVIDIA NIM** — delivering ultra-fast reasoning with deep-thinking support, streaming responses, and no rate-limit juggling.

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
