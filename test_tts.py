import asyncio, edge_tts

async def test_tts():
    try:
        c = edge_tts.Communicate("Hello, this is a speed test.", "en-GB-RyanNeural")
        audio = b""
        async for chunk in c.stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]
        print(f"TTS SUCCESS: {len(audio)} bytes of audio generated")
    except Exception as e:
        print(f"TTS FAILED: {e}")

asyncio.run(test_tts())
