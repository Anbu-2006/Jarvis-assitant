"""JARVIS FULL PIPELINE DIAGNOSTIC — Comprehensive Speed Audit"""
import time, httpx, json, sys, io, asyncio, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

RESULTS = {}

def call_ollama(prompt, system="", model="llama3.2:3b", timeout=300):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    t0 = time.time()
    r = httpx.post("http://localhost:11434/api/chat", json={
        "model": model,
        "messages": msgs,
        "stream": False,
        "options": {"num_ctx": 4096, "num_gpu": 99},
        "keep_alive": "5m"
    }, timeout=timeout)
    t1 = time.time()
    data = r.json()
    return data["message"]["content"], t1 - t0

async def test_tts():
    """Test Edge TTS network latency"""
    try:
        import edge_tts
        t0 = time.time()
        communicate = edge_tts.Communicate("Hello, this is a speed test.", "en-GB-RyanNeural")
        audio = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]
        t1 = time.time()
        return t1 - t0, len(audio)
    except Exception as e:
        return -1, str(e)

def benchmark():
    print("=" * 70)
    print("JARVIS FULL PIPELINE DIAGNOSTIC REPORT")
    print(f"RTX 3050 Laptop (4096 MiB VRAM) | {time.strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)

    # ================================================================
    # SECTION 1: GPU STATUS
    # ================================================================
    print("\n[1] GPU STATUS")
    print("-" * 40)
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu",
             "--format=csv,noheader"],
            capture_output=True, text=True
        )
        parts = [p.strip() for p in r.stdout.strip().split(",")]
        print(f"   VRAM Used:    {parts[0]}")
        print(f"   VRAM Total:   {parts[1]}")
        print(f"   GPU Util:     {parts[2]}")
        print(f"   Temperature:  {parts[3]}C")
        RESULTS["gpu_mem"] = parts[0]
    except Exception as e:
        print(f"   Error: {e}")

    # ================================================================
    # SECTION 2: OLLAMA MODEL PERFORMANCE
    # ================================================================
    print("\n[2] OLLAMA MODEL BENCHMARK")
    print("-" * 40)
    
    # Cold start (loads model into VRAM)
    print("   [2a] Cold Start (loading into VRAM)...")
    try:
        reply, t = call_ollama("hi")
        print(f"        Time: {t:.2f}s | Reply: {reply[:50]}")
        RESULTS["cold_start"] = t
    except Exception as e:
        print(f"        FAILED: {e}")
        RESULTS["cold_start"] = -1
        return
    
    # Check GPU after loading
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader"],
            capture_output=True, text=True
        )
        gpu_after = r.stdout.strip()
        print(f"        GPU after load: {gpu_after}")
        RESULTS["gpu_after_load"] = gpu_after
    except:
        pass

    # Warm tests
    print("   [2b] Warm Response #1...")
    reply, t = call_ollama("Say hello in 3 words", timeout=30)
    print(f"        Time: {t:.2f}s | Reply: {reply[:60]}")
    RESULTS["warm_1"] = t

    print("   [2c] Warm Response #2...")
    reply, t = call_ollama("What is 2+2? One word.", timeout=30)
    print(f"        Time: {t:.2f}s | Reply: {reply[:60]}")
    RESULTS["warm_2"] = t

    print("   [2d] With SLM System Prompt...")
    slm = ("You are JARVIS, an AI assistant. British wit, concise. "
           "Voice-only. 1 sentence MAX. No markdown. "
           "Actions: [ACTION:RUN_COMMAND], [ACTION:OPEN_APP], [ACTION:BROWSE].")
    reply, t = call_ollama("What time is it?", system=slm, timeout=30)
    print(f"        Time: {t:.2f}s | Reply: {reply[:60]}")
    RESULTS["slm_prompt"] = t

    # ================================================================
    # SECTION 3: EDGE TTS LATENCY  
    # ================================================================
    print("\n[3] EDGE TTS (Text-to-Speech) LATENCY")
    print("-" * 40)
    tts_time, audio_size = asyncio.run(test_tts())
    if tts_time > 0:
        print(f"   TTS Time:     {tts_time:.2f}s")
        print(f"   Audio Size:   {audio_size} bytes")
        RESULTS["tts"] = tts_time
    else:
        print(f"   TTS Error: {audio_size}")
        RESULTS["tts"] = -1

    # ================================================================
    # SECTION 4: SLM SYSTEM PROMPT SIZE
    # ================================================================
    print("\n[4] SYSTEM PROMPT ANALYSIS")
    print("-" * 40)
    try:
        with open(r"e:\Antigravity\Jarvis-assitant\jarvis\api\main.py", "r", encoding="utf-8") as f:
            content = f.read()
        
        # Check SLM_SYSTEM_PROMPT (the one actually used for local)
        start = content.find('SLM_SYSTEM_PROMPT = """')
        if start > 0:
            end = content.find('"""', start + 22)
            sp = content[start:end]
            words = len(sp.split())
            tokens = int(words * 1.3)
            print(f"   SLM Prompt:   {len(sp)} chars, ~{words} words, ~{tokens} tokens")
            print(f"   Context Use:  {tokens}/4096 = {tokens/4096*100:.0f}%")
            RESULTS["slm_tokens"] = tokens
        
        # Check JARVIS_SYSTEM_PROMPT too
        start2 = content.find('JARVIS_SYSTEM_PROMPT = """')
        if start2 > 0:
            end2 = content.find('"""', start2 + 25)
            sp2 = content[start2:end2]
            words2 = len(sp2.split())
            tokens2 = int(words2 * 1.3)
            print(f"   Main Prompt:  {len(sp2)} chars, ~{words2} words, ~{tokens2} tokens")
    except Exception as e:
        print(f"   Error: {e}")

    # ================================================================  
    # SECTION 5: MCP CLIENT STATUS
    # ================================================================
    print("\n[5] MCP CLIENT STATUS")
    print("-" * 40)
    try:
        sys.path.insert(0, r"e:\Antigravity\Jarvis-assitant")
        from jarvis.mcp_client import get_mcp_client
        mcp = get_mcp_client()
        print(f"   Started:      {mcp._started}")
        schemas = mcp.get_tool_schemas()
        print(f"   Tools loaded: {len(schemas) if isinstance(schemas, list) else 'N/A'}")
    except Exception as e:
        print(f"   Error: {e}")

    # ================================================================
    # SECTION 6: FRONTEND COMPLEXITY
    # ================================================================
    print("\n[6] FRONTEND ANALYSIS")
    print("-" * 40)
    frontend_files = {
        "main.ts": 9509, "orb.ts": 12790, "settings.ts": 23547,
        "voice.ts": 5377, "ws.ts": 1699
    }
    total = sum(frontend_files.values())
    print(f"   Files:        {len(frontend_files)}")
    print(f"   Total Size:   {total} bytes ({total/1024:.1f} KB)")
    print(f"   Largest:      settings.ts (23.5 KB)")
    print(f"   Impact:       LOW — static JS, no runtime overhead on AI speed")

    # ================================================================
    # SECTION 7: FULL PIPELINE ESTIMATE  
    # ================================================================
    print("\n" + "=" * 70)
    print("FULL PIPELINE LATENCY BREAKDOWN")
    print("=" * 70)
    
    warm_avg = (RESULTS.get("warm_1", 0) + RESULTS.get("warm_2", 0)) / 2
    tts = RESULTS.get("tts", 2.0)
    speech_rec = 0.5  # Browser Web Speech API
    ws_overhead = 0.1  # WebSocket round trip
    
    pipeline_total = speech_rec + warm_avg + tts + ws_overhead
    
    print(f"  Speech Recognition:     ~0.50s  (Browser Web Speech API)")
    print(f"  WebSocket overhead:     ~0.10s  (send/receive)")
    print(f"  LLM Inference (warm):   ~{warm_avg:.2f}s  (Ollama llama3.2:3b)")
    print(f"  Edge TTS:               ~{tts:.2f}s  (Microsoft servers)")
    print(f"  ─────────────────────────────────")
    print(f"  TOTAL ESTIMATED:        ~{pipeline_total:.2f}s")
    
    print("\n" + "=" * 70)
    print("DIAGNOSIS & RECOMMENDATIONS")
    print("=" * 70)
    
    if warm_avg > 10:
        print("  ⛔ CRITICAL: LLM running on CPU, not GPU!")
        print("     → Model not fitting in VRAM or GPU layers not offloaded")
    elif warm_avg > 5:
        print("  ⚠️  WARNING: LLM partially on CPU")
    else:
        print("  ✅ LLM speed OK (GPU inference)")
    
    if tts > 3:
        print("  ⚠️  TTS is slow — network latency to Microsoft servers")
    elif tts > 0:
        print("  ✅ TTS speed OK")
    
    if RESULTS.get("cold_start", 0) > 30:
        print("  ⚠️  Cold start is very slow — preload model on server start")
    
    print()
    print("  BOTTLENECK RANKING:")
    bottlenecks = [
        ("LLM Inference", warm_avg),
        ("Edge TTS", tts),
        ("Speech Recognition", speech_rec),
        ("WebSocket", ws_overhead),
    ]
    bottlenecks.sort(key=lambda x: x[1], reverse=True)
    for i, (name, t) in enumerate(bottlenecks, 1):
        bar = "█" * int(t * 5)
        print(f"    {i}. {name:25s} {t:.2f}s {bar}")

if __name__ == "__main__":
    benchmark()
