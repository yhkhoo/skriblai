import httpx
from os import getenv
OPENROUTER_API_KEY = getenv("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
}
MODEL = "google/gemma-4-31b-it:free"
PROMPT = """
Output 3 random English words, one on each line, with no additional text.
"""
with httpx.Client() as client:
    response = client.post(
        url=OPENROUTER_URL,
        headers=HEADERS,
        json={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": PROMPT
                        },
                    ]
                },
            ],
            "reasoning": {"enabled": False},
        }
    )
    resp = response.json()
    print(resp)
    print(type(resp))
    print(resp["choices"][0]["message"]["content"])