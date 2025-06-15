"""Test the full voice pipeline: WebSocket → LLM → TTS → Audio"""
import asyncio, websockets, json, time, base64, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

async def test_pipeline():
    print("=" * 60)
    print("JARVIS VOICE PIPELINE TEST")
    print("=" * 60)
    
    # Step 1: Connect WebSocket
    print("\n[1] Connecting to WebSocket...")
    t0 = time.time()
    try:
        ws = await asyncio.wait_for(
            websockets.connect("ws://localhost:8340/ws/voice"),
            timeout=10
        )
        print(f"    Connected in {time.time()-t0:.2f}s")
    except Exception as e:
        print(f"    FAILED: {e}")
        return
    
    # Step 2: Wait for greeting
    print("\n[2] Waiting for greeting...")
    t0 = time.time()
    greeting_received = False
    audio_received = False
    
    try:
        for _ in range(20):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                msg = json.loads(raw)
                mtype = msg.get("type", "")
                
                if mtype == "status":
                    print(f"    Status: {msg.get('state')} ({time.time()-t0:.2f}s)")
                elif mtype == "audio":
                    audio_data = msg.get("data", "")
                    text = msg.get("text", "")
                    print(f"    Audio: {len(audio_data)} chars b64, text='{text}' ({time.time()-t0:.2f}s)")
                    audio_received = True
                    greeting_received = True
                elif mtype == "text":
                    print(f"    Text: {msg.get('text', '')} ({time.time()-t0:.2f}s)")
                    greeting_received = True
                    
                if greeting_received:
                    break
            except asyncio.TimeoutError:
                print(f"    Timeout waiting for message ({time.time()-t0:.2f}s)")
                break
    except Exception as e:
        print(f"    Error: {e}")
    
    # Step 3: Send a transcript
    print("\n[3] Sending transcript: 'Hello JARVIS'")
    t0 = time.time()
    try:
        await ws.send(json.dumps({
            "type": "transcript",
            "text": "Hello JARVIS",
            "isFinal": True
        }))
        print(f"    Sent in {time.time()-t0:.4f}s")
    except Exception as e:
        print(f"    FAILED: {e}")
        await ws.close()
        return
    
    # Step 4: Wait for response
    print("\n[4] Waiting for JARVIS response...")
    t0 = time.time()
    response_text = ""
    response_audio = False
    
    try:
        for _ in range(30):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                msg = json.loads(raw)
                mtype = msg.get("type", "")
                
                if mtype == "status":
                    state = msg.get('state', '')
                    print(f"    Status: {state} ({time.time()-t0:.2f}s)")
                    if state == "idle" and response_audio:
                        break
                elif mtype == "audio":
                    audio_data = msg.get("data", "")
                    text = msg.get("text", "")
                    if text:
                        response_text = text
                    audio_bytes = len(base64.b64decode(audio_data)) if audio_data else 0
                    print(f"    Audio: {audio_bytes} bytes, text='{text[:50]}' ({time.time()-t0:.2f}s)")
                    response_audio = True
                elif mtype == "text":
                    text = msg.get("text", "")
                    response_text = text
                    print(f"    Text: {text[:80]} ({time.time()-t0:.2f}s)")
            except asyncio.TimeoutError:
                print(f"    Timeout ({time.time()-t0:.2f}s)")
                break
    except Exception as e:
        print(f"    Error: {e}")
    
    total = time.time() - t0
    
    # Summary
    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"  Response text:  {response_text[:80]}")
    print(f"  Audio received: {response_audio}")
    print(f"  Total time:     {total:.2f}s")
    
    if response_audio:
        print("\n  ✅ PIPELINE WORKING — Voice input → LLM → TTS → Audio output")
    elif response_text:
        print("\n  ⚠️  TEXT OK, NO AUDIO — TTS may be failing")
    else:
        print("\n  ⛔ NO RESPONSE — Backend may not be processing messages")
    
    await ws.close()

asyncio.run(test_pipeline())
