import asyncio
import os
import sys

# Add jarvis-assistant to path so imports work
sys.path.insert(0, os.path.abspath("."))

from dotenv import load_dotenv
load_dotenv()

from jarvis.core.llm_router import get_router

async def test():
    r = get_router()
    try:
        res = await r.generate("Reply with the word SUCCESS only.", temperature=0.1)
        print("Response:", res)
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    asyncio.run(test())
