/**
 * JARVIS — Settings Panel (Dark Brutalist Edition)
 *
 * Professional system configuration overlay with LLM provider status,
 * capability monitoring, and Windows-native system service indicators.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface LLMProvider {
  name: string;
  available: boolean;
  config: string;
}

interface StatusResponse {
  platform: string;
  version: string;
  tts_engine: string;
  tts_voice: string;
  tts_cost: string;
  llm_providers: LLMProvider[];
  active_provider: string;
  calendar_accessible: boolean;
  mail_accessible: boolean;
  notes_accessible: boolean;
  memory_count: number;
  task_count: number;
  server_port: number;
  uptime_seconds: number;
  env_keys_set: {
    nvidia: boolean;
    gemini: boolean;
    user_name: string;
  };
}

interface PreferencesResponse {
  user_name: string;
  honorific: string;
  calendar_accounts: string;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let panelEl: HTMLElement | null = null;
let isOpen = false;
let isFirstTimeSetup = false;
let setupStep = 0;
let lastStatus: StatusResponse | null = null;

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function apiGet<T>(url: string): Promise<T> {
  const res = await fetch(url);
  return res.json();
}

async function apiPost<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

// ---------------------------------------------------------------------------
// Panel HTML
// ---------------------------------------------------------------------------

function buildPanelHTML(): string {
  return `
    <div class="settings-backdrop" id="settings-backdrop"></div>
    <div class="settings-panel brutalist" id="settings-panel-inner">
      <div class="settings-header">
        <h2>SYS_CONFIG</h2>
        <button class="settings-close" id="settings-close">[X]</button>
      </div>

      <div class="settings-welcome" id="settings-welcome" style="display:none">
        <p>> INITIALIZING OPERATOR ENVIRONMENT...</p>
      </div>

      <div class="settings-body">

        <!-- LLM Providers -->
        <section class="settings-section" id="section-providers">
          <h3>[ AI ENGINES ]</h3>
          <div class="provider-cards" id="provider-cards">
            <div class="provider-card" id="provider-nvidia">
              <div class="provider-name">NVIDIA LLAMA</div>
              <div class="provider-status">--</div>
            </div>
          </div>
        </section>

        <!-- API Keys -->
        <section class="settings-section" id="section-api-keys">
          <h3>[ CREDENTIALS ]</h3>

          <div class="settings-field">
            <label>NVIDIA API KEY _(Primary)</label>
            <div class="settings-input-row">
              <input type="password" id="input-nvidia-key" placeholder="nvapi-..." />
              <button class="settings-btn" id="btn-test-nvidia">TEST</button>
              <span class="status-dot" id="status-nvidia"></span>
            </div>
          </div>

          <div class="settings-field">
            <label>GEMINI API KEY _(Vision Fallback)</label>
            <div class="settings-input-row">
              <input type="password" id="input-gemini-key" placeholder="AIzaSy..." />
              <button class="settings-btn" id="btn-test-gemini">TEST</button>
              <span class="status-dot" id="status-gemini"></span>
            </div>
          </div>

          <div class="settings-actions">
            <button class="settings-btn primary" id="btn-save-keys">COMMIT_KEYS</button>
          </div>
        </section>

        <!-- System Services -->
        <section class="settings-section" id="section-status">
          <h3>[ SUBSYSTEMS ]</h3>
          <div class="status-grid">
            <div class="status-row"><span class="status-dot" id="status-server"></span><span>Core Service</span><span class="status-detail" id="status-server-detail"></span></div>
            <div class="status-row"><span class="status-dot" id="status-tts"></span><span>Voice Synth (Edge-TTS)</span><span class="status-detail">FREE</span></div>
            <div class="status-row"><span class="status-dot" id="status-screen"></span><span>Screen Awareness</span><span class="status-detail">PowerShell</span></div>
            <div class="status-row"><span class="status-dot" id="status-notes"></span><span>Local Notes I/O</span><span class="status-detail">Markdown</span></div>
            <div class="status-row"><span class="status-dot" id="status-apps"></span><span>App Launcher</span><span class="status-detail">Win32</span></div>
            <div class="status-row"><span class="status-dot" id="status-memory"></span><span>Memory Store</span><span class="status-detail" id="status-memory-count">--</span></div>
          </div>
        </section>

        <!-- Capabilities -->
        <section class="settings-section" id="section-capabilities">
          <h3>[ CAPABILITIES ]</h3>
          <div class="cap-grid">
            <div class="cap-item"><span class="cap-dot"></span>Voice Input</div>
            <div class="cap-item"><span class="cap-dot"></span>Voice Response</div>
            <div class="cap-item"><span class="cap-dot"></span>Open Apps</div>
            <div class="cap-item"><span class="cap-dot"></span>Browse Web</div>
            <div class="cap-item"><span class="cap-dot"></span>Screen Vision</div>
            <div class="cap-item"><span class="cap-dot"></span>Build Projects</div>
            <div class="cap-item"><span class="cap-dot"></span>Task Manager</div>
            <div class="cap-item"><span class="cap-dot"></span>Local Notes</div>
            <div class="cap-item"><span class="cap-dot"></span>Memory Store</div>
            <div class="cap-item"><span class="cap-dot"></span>Day Planning</div>
            <div class="cap-item"><span class="cap-dot"></span>Research</div>
            <div class="cap-item"><span class="cap-dot"></span>Terminal</div>
          </div>
        </section>

        <!-- User Preferences -->
        <section class="settings-section" id="section-preferences">
          <h3>[ IDENTITY ]</h3>

          <div class="settings-field">
            <label>OPERATOR ALIAS</label>
            <input type="text" id="input-user-name" placeholder="Name" />
          </div>

          <div class="settings-field">
            <label>HONORIFIC</label>
            <select id="input-honorific">
              <option value="sir">Sir</option>
              <option value="ma'am">Ma'am</option>
              <option value="none">None</option>
            </select>
          </div>

          <div class="settings-actions">
            <button class="settings-btn primary" id="btn-save-prefs">COMMIT_CONF</button>
          </div>
        </section>

        <!-- Diagnostics -->
        <section class="settings-section" id="section-sysinfo">
          <h3>[ DIAGNOSTICS ]</h3>
          <div class="sysinfo-grid">
            <div class="sysinfo-row"><span class="sysinfo-label">ACTIVE_LLM</span><span id="sysinfo-llm" class="glitch-text">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">PLATFORM</span><span id="sysinfo-platform">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">VOICE_TTS</span><span id="sysinfo-tts">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">MEM_FRAGMENTS</span><span id="sysinfo-memory">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">ACTIVE_TASKS</span><span id="sysinfo-tasks">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">UPTIME</span><span id="sysinfo-uptime">--</span></div>
          </div>
        </section>

        <!-- Setup Navigation (first-time only) -->
        <div class="setup-nav" id="setup-nav" style="display:none">
          <button class="settings-btn primary" id="btn-setup-next">NEXT_PHASE</button>
        </div>

      </div>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Panel lifecycle
// ---------------------------------------------------------------------------

function createPanel(): HTMLElement {
  const container = document.createElement("div");
  container.id = "settings-container";
  container.innerHTML = buildPanelHTML();
  document.body.appendChild(container);
  return container;
}

function setDotStatus(id: string, status: "green" | "red" | "yellow" | "off") {
  const dot = document.getElementById(id);
  if (!dot) return;
  dot.className = "status-dot";
  if (status !== "off") dot.classList.add(`status-${status}`);
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

function updateProviderCards(status: StatusResponse) {
  const providers = status.llm_providers || [];
  const activeProvider = status.active_provider?.toLowerCase() || "";

  const mapping: Record<string, string> = {
    nvidia: "provider-nvidia",
  };

  for (const provider of providers) {
    const cardId = mapping[provider.name.toLowerCase()];
    if (!cardId) continue;
    const card = document.getElementById(cardId);
    if (!card) continue;

    card.classList.remove("active", "available", "unavailable");

    if (provider.available) {
      card.classList.add("available");
    } else {
      card.classList.add("unavailable");
    }

    if (activeProvider.includes(provider.name.toLowerCase())) {
      card.classList.add("active");
    }

    const statusEl = card.querySelector(".provider-status");
    if (statusEl) {
      if (activeProvider.includes(provider.name.toLowerCase())) {
        statusEl.textContent = "● ACTIVE";
      } else if (provider.available) {
        statusEl.textContent = "READY";
      } else {
        statusEl.textContent = "NO KEY";
      }
    }
  }
}

async function loadStatus() {
  try {
    const status = await apiGet<StatusResponse>("/api/settings/status");
    lastStatus = status;

    // Subsystem status
    setDotStatus("status-server", "green");
    setDotStatus("status-tts", "green"); // Edge-TTS is always available
    setDotStatus("status-screen", "green");
    setDotStatus("status-notes", status.notes_accessible ? "green" : "red");
    setDotStatus("status-apps", status.platform === "windows" ? "green" : "yellow");
    setDotStatus("status-memory", status.memory_count > 0 ? "green" : "yellow");

    const serverDetail = document.getElementById("status-server-detail");
    if (serverDetail) serverDetail.textContent = `[:${status.server_port}] ${formatUptime(status.uptime_seconds)}`;

    const memCount = document.getElementById("status-memory-count");
    if (memCount) memCount.textContent = `${status.memory_count} entries`;

    // API key status dots
    setDotStatus("status-nvidia", status.env_keys_set.nvidia ? "green" : "red");
    setDotStatus("status-gemini", status.env_keys_set.gemini ? "green" : "yellow");

    // Provider cards
    updateProviderCards(status);

    // Diagnostics
    const llmEl = document.getElementById("sysinfo-llm");
    if (llmEl) llmEl.textContent = status.active_provider.toUpperCase();
    const platEl = document.getElementById("sysinfo-platform");
    if (platEl) platEl.textContent = status.platform.toUpperCase();
    const ttsEl = document.getElementById("sysinfo-tts");
    if (ttsEl) ttsEl.textContent = (status.tts_engine || "edge-tts").toUpperCase();
    const memEl = document.getElementById("sysinfo-memory");
    if (memEl) memEl.textContent = String(status.memory_count);
    const tasksEl = document.getElementById("sysinfo-tasks");
    if (tasksEl) tasksEl.textContent = String(status.task_count || 0);
    const upEl = document.getElementById("sysinfo-uptime");
    if (upEl) upEl.textContent = formatUptime(status.uptime_seconds);

    // Update global HUD elements
    updateGlobalHUD(status);

    return status;
  } catch (e) {
    console.error("[settings] failed to load status:", e);
    setDotStatus("status-server", "red");
    return null;
  }
}

function updateGlobalHUD(status: StatusResponse) {
  // Top status bar
  const hudUptime = document.getElementById("hud-uptime");
  if (hudUptime) hudUptime.textContent = formatUptime(status.uptime_seconds);
  const hudProvider = document.getElementById("hud-provider");
  if (hudProvider) hudProvider.textContent = status.active_provider.toUpperCase();

  // Right panel diagnostics
  const diagMemory = document.getElementById("hud-diag-memory");
  if (diagMemory) diagMemory.textContent = `${status.memory_count} FRAG`;
  const diagTasks = document.getElementById("hud-diag-tasks");
  if (diagTasks) diagTasks.textContent = `${status.task_count || 0} ACTIVE`;
  const diagTTS = document.getElementById("hud-diag-tts");
  if (diagTTS) diagTTS.textContent = (status.tts_engine || "EDGE-TTS").toUpperCase();

  // Right panel directives
  const hudNeeded = document.getElementById("hud-needed");
  if (hudNeeded) {
    const items: string[] = [];
    if (!status.env_keys_set.nvidia) items.push('<div class="needed-item error">NVIDIA KEY MISSING</div>');
    if (items.length === 0) items.push('<div class="needed-item ok">ALL SYSTEMS NOMINAL</div>');
    hudNeeded.innerHTML = items.join("");
  }
}

async function loadPreferences() {
  try {
    const prefs = await apiGet<PreferencesResponse>("/api/settings/preferences");
    const nameEl = document.getElementById("input-user-name") as HTMLInputElement;
    const honEl = document.getElementById("input-honorific") as HTMLSelectElement;
    if (nameEl) nameEl.value = prefs.user_name || "";
    if (honEl) honEl.value = prefs.honorific || "sir";
  } catch (e) {
    console.error("[settings] failed to load preferences:", e);
  }
}

function wireEvents() {
  // Close
  document.getElementById("settings-close")?.addEventListener("click", closeSettings);
  document.getElementById("settings-backdrop")?.addEventListener("click", closeSettings);

  // Save all keys
  document.getElementById("btn-save-keys")?.addEventListener("click", async () => {
    const nvidiaKey = (document.getElementById("input-nvidia-key") as HTMLInputElement).value.trim();
    if (nvidiaKey) await apiPost("/api/settings/keys", { key_name: "NVIDIA_API_KEY", key_value: nvidiaKey });

    const geminiKey = (document.getElementById("input-gemini-key") as HTMLInputElement).value.trim();
    if (geminiKey) await apiPost("/api/settings/keys", { key_name: "GEMINI_API_KEY", key_value: geminiKey });

    await loadStatus();
  });

  // Test NVIDIA
  document.getElementById("btn-test-nvidia")?.addEventListener("click", async () => {
    setDotStatus("status-nvidia", "yellow");
    const key = (document.getElementById("input-nvidia-key") as HTMLInputElement).value.trim();
    try {
      const result = await apiPost<{ valid: boolean; error?: string }>("/api/settings/test-nvidia", { key_value: key || undefined });
      setDotStatus("status-nvidia", result.valid ? "green" : "red");
    } catch {
      setDotStatus("status-nvidia", "red");
    }
  });

  // Test Gemini
  document.getElementById("btn-test-gemini")?.addEventListener("click", async () => {
    setDotStatus("status-gemini", "yellow");
    const key = (document.getElementById("input-gemini-key") as HTMLInputElement).value.trim();
    try {
      // Send a dummy post just to test or save
      if (key) await apiPost("/api/settings/keys", { key_name: "GEMINI_API_KEY", key_value: key });
      setDotStatus("status-gemini", key ? "green" : "yellow");
    } catch {
      setDotStatus("status-gemini", "red");
    }
  });

  // Save preferences
  document.getElementById("btn-save-prefs")?.addEventListener("click", async () => {
    const user_name = (document.getElementById("input-user-name") as HTMLInputElement).value.trim();
    const honorific = (document.getElementById("input-honorific") as HTMLSelectElement).value;
    await apiPost("/api/settings/preferences", { user_name, honorific, calendar_accounts: "auto" });
    await loadStatus();
  });

  // Setup next button
  document.getElementById("btn-setup-next")?.addEventListener("click", advanceSetup);
}

// ---------------------------------------------------------------------------
// First-time setup wizard
// ---------------------------------------------------------------------------

function enterSetupMode() {
  isFirstTimeSetup = true;
  setupStep = 0;

  const welcome = document.getElementById("settings-welcome");
  if (welcome) welcome.style.display = "block";

  const nav = document.getElementById("setup-nav");
  if (nav) nav.style.display = "flex";

  showSetupStep(0);
}

function showSetupStep(step: number) {
  const sections = ["section-providers", "section-api-keys", "section-status", "section-preferences", "section-sysinfo", "section-capabilities"];
  sections.forEach((id, i) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (step === 0 && (i === 0 || i === 1)) el.style.display = "";
    else if (step === 1 && i === 3) el.style.display = "";
    else if (step === 2) el.style.display = "";
    else el.style.display = "none";
  });

  const nextBtn = document.getElementById("btn-setup-next");
  if (nextBtn) {
    if (step === 0) nextBtn.textContent = "> NEXT: CONFIGURE IDENTITY";
    else if (step === 1) nextBtn.textContent = "> INITIALIZE SYSTEM";
    else nextBtn.style.display = "none";
  }
}

async function advanceSetup() {
  setupStep++;
  if (setupStep >= 2) {
    isFirstTimeSetup = false;
    const welcome = document.getElementById("settings-welcome");
    if (welcome) welcome.style.display = "none";
    const nav = document.getElementById("setup-nav");
    if (nav) nav.style.display = "none";

    ["section-providers", "section-api-keys", "section-status", "section-preferences", "section-sysinfo", "section-capabilities"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.style.display = "";
    });

    closeSettings();
    return;
  }
  showSetupStep(setupStep);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export async function openSettings() {
  if (isOpen) return;
  isOpen = true;

  if (!panelEl) {
    panelEl = createPanel();
    wireEvents();
  }

  panelEl.style.display = "block";

  requestAnimationFrame(() => {
    panelEl!.classList.add("open");
  });

  const status = await loadStatus();
  await loadPreferences();

  if (status && !status.env_keys_set.nvidia) {
    enterSetupMode();
  }
}

export function closeSettings() {
  if (!panelEl || !isOpen) return;
  isOpen = false;
  panelEl.classList.remove("open");
  setTimeout(() => {
    if (panelEl) panelEl.style.display = "none";
  }, 300);
}

export function isSettingsOpen(): boolean {
  return isOpen;
}

export async function checkFirstTimeSetup(): Promise<boolean> {
  try {
    const status = await apiGet<StatusResponse>("/api/settings/status");
    if (!status.env_keys_set.nvidia) {
      openSettings();
      return true;
    }
  } catch {
    // Server not ready yet
  }
  return false;
}

export async function pollStatus() {
  await loadStatus();
}

export function getLastStatus(): StatusResponse | null {
  return lastStatus;
}
