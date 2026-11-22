"""Выгрузить актуальный OpenAPI из приложения в 10-ARCHITECTURE/openapi.yaml.

Зачем: контракт в репозитории отставал от кода (29 путей против 75 операций), и фронтенд
не мог опираться на файл. Теперь файл — артефакт сборки: `python scripts/dump_openapi.py`,
а расхождение ловит tests/test_schema_sync.py::test_openapi_matches_app.

Запуск:
    PYTHONPATH=src python scripts/dump_openapi.py            # перезаписать файл
    PYTHONPATH=src python scripts/dump_openapi.py --check    # только проверить (CI)
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src"))

from orbita.main import app  # noqa: E402

TARGET = BACKEND.parent / "10-ARCHITECTURE" / "openapi.yaml"

HEADER = """# ВНИМАНИЕ: файл сгенерирован из кода.
# Источник правды — обработчики FastAPI (40-BACKEND/src/orbita/api*.py).
# Пересобрать: PYTHONPATH=src python scripts/dump_openapi.py
"""


def render() -> str:
    spec = app.openapi()
    return HEADER + yaml.safe_dump(spec, allow_unicode=True, sort_keys=False, width=110)


def main() -> int:
    content = render()
    spec = app.openapi()
    summary = (f"{TARGET}: {len(spec['paths'])} путей, "
               f"{sum(len(v) for v in spec['paths'].values())} операций")
    if "--check" in sys.argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != content:
            print(f"openapi.yaml отстаёт от кода — пересоберите: {summary}", file=sys.stderr)
            return 1
        print(f"openapi.yaml совпадает с кодом — {summary}")
        return 0
    TARGET.write_text(content, encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
