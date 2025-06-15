/**
 * JARVIS — Main entry point.
 *
 * Wires together the orb visualization, WebSocket communication,
 * speech recognition, and audio playback into a single experience.
 * Drives the Brutalist HUD with live data from the backend.
 */

import { createOrb, type OrbState } from "./orb";
import { createVoiceInput, createAudioPlayer } from "./voice";
import { createSocket } from "./ws";
import { openSettings, checkFirstTimeSetup, pollStatus } from "./settings";
import "./style.css";

// ---------------------------------------------------------------------------
// State machine
// ---------------------------------------------------------------------------

type State = "idle" | "listening" | "thinking" | "speaking";
let currentState: State = "idle";
let isMuted = false;

const statusEl = document.getElementById("status-text")!;
const errorEl = document.getElementById("error-text")!;

function showError(msg: string) {
  errorEl.textContent = msg;
  errorEl.style.opacity = "1";
  setTimeout(() => {
    errorEl.style.opacity = "0";
  }, 5000);
}

function updateStatus(state: State) {
  const labels: Record<State, string> = {
    idle: "",
    listening: "listening...",
    thinking: "thinking...",
    speaking: "",
  };
  statusEl.textContent = labels[state];
}

// ---------------------------------------------------------------------------
// HUD Log Feed
// ---------------------------------------------------------------------------

const hudLogs = document.getElementById("hud-logs")!;
const hudTasks = document.getElementById("hud-tasks")!;
const activeTasks: Map<string, string> = new Map();

function addLog(msg: string) {
  const timestamp = new Date().toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const div = document.createElement("div");
  div.textContent = `[${timestamp}] ${msg}`;
  hudLogs.appendChild(div);
  // Keep max 20 entries
  while (hudLogs.children.length > 20) {
    hudLogs.removeChild(hudLogs.firstChild!);
  }
  hudLogs.scrollTop = hudLogs.scrollHeight;
}

function updateTasksDisplay() {
  if (activeTasks.size === 0) {
    hudTasks.innerHTML = '<span class="hud-empty">NO ACTIVE TASKS</span>';
    return;
  }
  hudTasks.innerHTML = "";
  activeTasks.forEach((prompt, id) => {
    const div = document.createElement("div");
    div.className = "needed-item ok";
    div.textContent = prompt.substring(0, 40) + (prompt.length > 40 ? "..." : "");
    div.title = prompt;
    hudTasks.appendChild(div);
  });
}

// ---------------------------------------------------------------------------
// Init components
// ---------------------------------------------------------------------------

const canvas = document.getElementById("orb-canvas") as HTMLCanvasElement;
const orb = createOrb(canvas);

const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
const WS_URL = `${wsProto}//${window.location.host}/ws/voice`;
const socket = createSocket(WS_URL);

const audioPlayer = createAudioPlayer();
orb.setAnalyser(audioPlayer.getAnalyser());

function transition(newState: State) {
  if (newState === currentState) return;
  currentState = newState;
  orb.setState(newState as OrbState);
  updateStatus(newState);

  switch (newState) {
    case "idle":
      if (!isMuted) voiceInput.resume();
      break;
    case "listening":
      if (!isMuted) voiceInput.resume();
      break;
    case "thinking":
      voiceInput.pause();
      break;
    case "speaking":
      voiceInput.pause();
      break;
  }
}

// ---------------------------------------------------------------------------
// Voice input
// ---------------------------------------------------------------------------

const voiceInput = createVoiceInput(
  (text: string) => {
    audioPlayer.stop();
    socket.send({ type: "transcript", text, isFinal: true });
    transition("thinking");
    addLog(`USER: ${text.substring(0, 50)}`);
  },
  (msg: string) => {
    showError(msg);
  },
  () => {
    // onBargeIn: User started speaking while JARVIS was talking
    if (currentState === "speaking") {
      audioPlayer.stop();
      transition("listening");
      addLog("SYS: Barge-in detected, playback stopped");
    }
  }
);

// ---------------------------------------------------------------------------
// Audio playback finished
// ---------------------------------------------------------------------------

audioPlayer.onFinished(() => {
  transition("idle");
});

// ---------------------------------------------------------------------------
// WebSocket messages
// ---------------------------------------------------------------------------

socket.onMessage((msg) => {
  const type = msg.type as string;

  if (type === "audio") {
    const audioData = msg.data as string;
    if (audioData) {
      if (currentState !== "speaking") {
        transition("speaking");
      }
      audioPlayer.enqueue(audioData);
    } else {
      transition("idle");
    }
    if (msg.text) {
      console.log("[JARVIS]", msg.text);
      addLog(`JARVIS: ${(msg.text as string).substring(0, 50)}`);
    }
  } else if (type === "status") {
    const state = msg.state as string;
    if (state === "thinking" && currentState !== "thinking") {
      transition("thinking");
    } else if (state === "working") {
      transition("thinking");
      statusEl.textContent = "working...";
      addLog("SYS: Task dispatched");
    } else if (state === "idle") {
      transition("idle");
    }
  } else if (type === "text") {
    console.log("[JARVIS]", msg.text);
    addLog(`JARVIS: ${(msg.text as string).substring(0, 50)}`);
  } else if (type === "task_spawned") {
    const taskId = msg.task_id as string;
    const prompt = msg.prompt as string || "Task";
    activeTasks.set(taskId, prompt);
    updateTasksDisplay();
    addLog(`TASK: Spawned ${taskId}`);
    // Update Copilot HUD
    const copilotEl = document.getElementById("hud-copilot-status");
    if (copilotEl) { copilotEl.textContent = "ACTIVE"; copilotEl.style.color = "var(--neon-cyan)"; }
  } else if (type === "task_complete") {
    const taskId = msg.task_id as string;
    activeTasks.delete(taskId);
    updateTasksDisplay();
    addLog(`TASK: Complete ${taskId}`);
    // Revert Copilot HUD if no more tasks
    if (activeTasks.size === 0) {
      const copilotEl = document.getElementById("hud-copilot-status");
      if (copilotEl) { copilotEl.textContent = "IDLE"; copilotEl.style.color = ""; }
    }
  }
});

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

function ensureAudioContext() {
  const ctx = audioPlayer.getAnalyser().context as AudioContext;
  if (ctx.state === "suspended") {
    ctx.resume().then(() => console.log("[audio] context resumed"));
  }
}

const btnInitialize = document.getElementById("btn-initialize")!;
const initOverlay = document.getElementById("init-overlay")!;
const hudAudioStatus = document.getElementById("hud-audio-status")!;

btnInitialize.addEventListener("click", () => {
  ensureAudioContext();
  initOverlay.style.opacity = "0";
  setTimeout(() => initOverlay.style.display = "none", 500);

  hudAudioStatus.textContent = "ACTIVE";
  hudAudioStatus.style.color = "var(--neon-cyan)";

  socket.connect();
  addLog("SYS: Audio context initialized");
  addLog("SYS: WebSocket connecting...");

  setTimeout(() => {
    voiceInput.start();
    transition("listening");
    addLog("SYS: Voice input active");
  }, 1000);
});

ensureAudioContext();

// ---------------------------------------------------------------------------
// UI Controls
// ---------------------------------------------------------------------------

const btnMute = document.getElementById("btn-mute")!;
const btnMenu = document.getElementById("btn-menu")!;
const menuDropdown = document.getElementById("menu-dropdown")!;
const btnRestart = document.getElementById("btn-restart")!;
const btnFixSelf = document.getElementById("btn-fix-self")!;

btnMute.addEventListener("click", (e) => {
  e.stopPropagation();
  isMuted = !isMuted;
  btnMute.classList.toggle("muted", isMuted);
  if (isMuted) {
    voiceInput.pause();
    transition("idle");
    addLog("SYS: Microphone muted");
  } else {
    voiceInput.resume();
    transition("listening");
    addLog("SYS: Microphone unmuted");
  }
});

btnMenu.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = menuDropdown.style.display === "none" ? "block" : "none";
});

document.addEventListener("click", () => {
  menuDropdown.style.display = "none";
});

btnRestart.addEventListener("click", async (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  statusEl.textContent = "restarting...";
  addLog("SYS: Restart requested");
  try {
    await fetch("/api/restart", { method: "POST" });
    setTimeout(() => window.location.reload(), 4000);
  } catch {
    statusEl.textContent = "restart failed";
    addLog("ERR: Restart failed");
  }
});

btnFixSelf.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  socket.send({ type: "fix_self" });
  statusEl.textContent = "entering work mode...";
  addLog("SYS: Work mode activated");
});

// Settings button
const btnSettings = document.getElementById("btn-settings")!;
btnSettings.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  openSettings();
});

// First-time setup detection + HUD polling
setTimeout(() => {
  checkFirstTimeSetup();
  setInterval(pollStatus, 5000);
  pollStatus();
}, 2000);
