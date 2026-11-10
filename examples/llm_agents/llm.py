"""A minimal OpenAI-compatible chat call over httpx. Settings come from the environment only."""

import os

import httpx


async def chat(system: str, user: str, *, temperature: float = 0.4) -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    base = os.environ.get("CMF_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("CMF_OPENAI_MODEL", "gpt-4o-mini")
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
    if r.status_code != 200:
        raise RuntimeError(f"LLM call failed ({r.status_code}): {r.text[:200]}")
    content: str = r.json()["choices"][0]["message"]["content"]
    return content.strip()
