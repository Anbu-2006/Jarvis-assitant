
import time
import asyncio
from jarvis.api.main import synthesize_speech

async def test():
    t0 = time.time()
    res = await synthesize_speech("Hello, this is a test of the text to speech latency. I hope it is fast enough.")
    t1 = time.time()
    print(f"TTS Latency: {t1 - t0:.3f}s for {len(res)} bytes")

asyncio.run(test())

