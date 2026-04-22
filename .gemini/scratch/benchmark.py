import os
import time
import httpx
import asyncio

async def test_nvidia():
    api_key = os.getenv("NVIDIA_API_KEY")
    if not api_key:
        print("No NVIDIA_API_KEY found.")
        return

    large_context = "context word " * 2500 

    client = httpx.AsyncClient(timeout=30.0)
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "meta/llama-3.3-70b-instruct",
        "messages": [{"role": "user", "content": large_context + " Hello. Answer with exactly one word."}],
        "max_tokens": 10,
        "temperature": 0.0,
        "stream": False
    }

    print("Testing NVIDIA Llama 3.3 70B Latency...")
    start_time = time.time()
    try:
        response = await client.post(url, headers=headers, json=payload)
        elapsed = time.time() - start_time
        data = response.json()
        print(f"Status: {response.status_code}")
        print(f"Total Time: {elapsed:.3f} seconds")
        if "usage" in data:
            print(f"Total Tokens: {data['usage']['total_tokens']}")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await client.aclose()

if __name__ == "__main__":
    # load env manually since we are in a subfolder
    env_path = ".env"
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    os.environ[k] = v
    asyncio.run(test_nvidia())
