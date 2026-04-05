import httpx
import asyncio

async def test():
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            "http://localhost:8000/agent/run",
            json={"query": "зайди на сайт wildberries.ru и найди кроссовки Nike"}
        )
        data = resp.json()
        print("Успех:", data["success"])
        print("Результат:", data["result"])
        print("Ошибка:", data["error"])
        for step in data["steps"]:
            print(f"  Шаг {step['step']}: {step['action']} — {step['description']}")

asyncio.run(test())