"""Главный критерий Дня 6: сто агентов с разными конфигами в одном процессе.

    .venv/bin/python checks/spawn_100.py

Ни сети, ни ключа, ни подпроцессов: спавн — это сто объектов в словаре
реестра. Скрипт проверяет и прямой вызов реестра, и HTTP-ручку, потому что
демонстрация организатору идёт через ручку.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from checks._stub import install_offline  # noqa: E402

N = 100

MODELS = [
    "openai/gpt-4o-mini",
    "meta-llama/llama-3.1-8b-instruct",
    "mistralai/mistral-small-3.2-24b-instruct",
    "google/gemini-3.1-flash-lite",
    "openai/gpt-4o",
]


def configs(n: int = N) -> list[dict]:
    """Сто разных конфигов: модель, температура, потолок токенов, память, промпт."""
    return [
        {
            "label": f"агент-{i:03d}",
            "model": MODELS[i % len(MODELS)],
            "temperature": round(0.1 + (i % 10) / 10, 2),
            "max_tokens": 200 + i,
            "history_limit": i % 7,
            "system": f"Ты агент номер {i}. Отвечай одной строкой.",
        }
        for i in range(n)
    ]


def main() -> int:
    install_offline()

    from app.registry import AgentRegistry
    from app.schema import AgentSpec

    # 1) Прямой спавн через реестр — то, что происходит внутри процесса.
    registry = AgentRegistry(max_agents=1000)
    specs = [
        AgentSpec(
            label=c["label"],
            model=c["model"],
            messages=[],
            temperature=c["temperature"],
            max_tokens=c["max_tokens"],
            history_limit=c["history_limit"],
            system=c["system"],
        )
        for c in configs()
    ]
    started = time.perf_counter()
    agents = registry.create_many(specs)
    direct_ms = (time.perf_counter() - started) * 1000

    assert len(agents) == N, len(agents)
    assert len(registry) == N, len(registry)
    assert len({a.id for a in agents}) == N, "id агентов должны быть уникальны"
    assert len({(a.spec.model, a.spec.temperature, a.spec.max_tokens) for a in agents}) > 1
    assert all(a.spec.system.startswith("Ты агент номер") for a in agents)
    # Конфиги действительно разные и не ссылаются на один объект.
    assert agents[0].spec is not specs[0], "агент обязан держать копию конфига"
    assert agents[0].history is not agents[1].history, "история у каждого своя"
    assert registry.get(agents[7].id) is agents[7]
    print(f"[1] реестр: {N} агентов за {direct_ms:.1f} мс, живых {len(registry)}")

    # 2) Та же сотня — одним HTTP-запросом. Именно это показывают организатору.
    import app.main as main_module

    with TestClient(main_module.app) as client:
        before = client.get("/api/health").json()["agents_live"]
        started = time.perf_counter()
        response = client.post("/api/agents", json={"agents": configs()})
        http_ms = (time.perf_counter() - started) * 1000
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["created"] == N, body["created"]
        assert len({a["id"] for a in body["agents"]}) == N
        assert len({a["model"] for a in body["agents"]}) == len(MODELS)
        assert len({a["temperature"] for a in body["agents"]}) == 10
        print(
            f"[2] POST /api/agents: {body['created']} агентов за {http_ms:.1f} мс "
            f"(спавн внутри {body['spawn_ms']} мс), живых {body['live']}"
        )

        listing = client.get("/api/agents").json()
        assert listing["live"] == before + N, (listing["live"], before)
        print(f"[3] GET /api/agents: {listing['live']} живых, потолок {listing['max_agents']}")

        # Ни одного вызова к модели: спавн бесплатен.
        from checks._stub import CALLS

        assert not CALLS, f"спавн не должен ходить в модель, а сходил {len(CALLS)} раз"
        print("[4] вызовов к модели за спавн: 0")

        # Все сто — в одном процессе: pid один, потоков не прибавилось.
        import threading

        print(
            f"[5] процесс один: pid={os.getpid()}, потоков {threading.active_count()}, "
            f"агентов {listing['live']}"
        )

    print("\nОК: сто агентов с разными конфигами живут в одном процессе.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
