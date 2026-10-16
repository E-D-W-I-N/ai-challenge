# Архитектура дня 33

`support.__main__` — консоль одной сессии выбранного тикета. `support.context` —
отдельно запущенный оператором read-only MCP и его клиент; `support.assistant`
связывает свежий контекст, docs RAG и generation. `rag.index` — независимый
Markdown scanner/SQLite индекс. `shared_models` сохраняет совместимый HTTP,
system proxy и reasoning contract. Нет web, Git интеграции, CRM мутаций или auth.

## Контекст и границы доверия

MCP слушает loopback, принимает явный JSON path и один ticket ID от оператора.
Единственный tool `selected_context` без аргументов возвращает только связанный
тикет/владельца. На старте и каждом вызове JSON читается заново и проверяются
типизированные users/tickets, уникальные IDs, связи владельцев и размер.
Неизвестный тикет или ошибка файла/схемы — отказ. Модель не получает весь JSON,
другие записи, путь или инструмент произвольного выбора пользователя.

Клиент сверяет привязку при старте и перед каждым ответом через MCP. Прямого
чтения JSON/старого контекста при ошибке нет. Другой тикет требует нового MCP
binding и нового CLI процесса; /new очищает историю текущего тикета. Это trusted
operator selection, не access control для многопользовательского сервиса.
JSON может обновляться извне; один ответ использует один прочитанный контекст.

## Индекс и RAG

Сканер без Git читает только Markdown из явной knowledge directory, исключая
hidden/symlink, известные secret/build/vendor/data/files имена во всех компонентах пути и бинарные файлы.
Текст LF делится окнами40 с path/start/end/text/hash; Unicode separators остаются
в строках. Фильтр не заменяет выбор безопасной папки оператором. Превышения
документа128KiB/corpus4MiB/files10000/chunks2000/chunk12000bytes — явные ошибки.

/index явно сканирует корпус, использует vectors кэша неизменившихся hash при
совпадении absolute root/model, отправляет новые тексты batches32, строит новую
SQLite и атомарно заменяет индекс (0600). Старый индекс сохраняется при ошибке, включая недоступное поддерево: os.walk
onerror поднимает ошибку чтения вместо молчаливого пропуска.
Пустой корпус публикует пустой индекс, никакие прежние чанки не остаются.
SQLite URI строится через Path.as_uri. Индекс вне корпуса, проверяется root/model;
автоматического платного reindex нет. Отсутствующий индекс даёт понятную ошибку,
изменившийся корпус — предупреждение STALE с использованием опубликованного снимка.

Перед ответом: fresh MCP → check index → query embedding(question, selected
context, last3 successful pairs) → top3 cosine docs → один Chat Completions.
Пустой индекс пропускает embeddings и передаёт пустую документацию. Весь контекст
ограничен явно; silent truncation нет. Prompt отделяет недоверенные ticket/user,
documentation/history от инструкций. Просит ссылки [n], различение ticket facts
и документальных фактов, признание пробелов и уточнение. Семантической проверки
ответа/цитат нет; рядом печатаются фактически переданные path/line/hash sources.
История добавляет пару только после успешной генерации/проверки размера; errors
её не меняют. Последние3 пары — явный rolling conversation limit, /new очищает.

## Подключение и состояние

Settings JSON — private atomic0600; defaults: compatible loopback URL, empty
chat/embedding IDs, reasoning OFF, temperature.2/max_tokens1024. Температура
конечная неотрицательная, max_tokens положительное целое либо null; null исключает
поле из HTTP payload. Верхних caps нет, совместимость проверяет модельный сервер.
Settings верхнего уровня обязаны быть mapping; null/другие типы дают безопасный отказ. /settings использует
getpass, blank сохраняет ключ, '-' удаляет; /models лишь каталог, без inference
или угадывания типов. Новая URL не подменяет IDs. CLI не читает env/dotenv/старые
базы. State по умолчанию под home/.local/state/support-assistant/hash(root),
явный --state вне корпуса поддержан; SQLite проверяет принадлежность root/model.
Ключ скрыт из repr, безопасного status, prompt, exception bodies. Чаты и MCP
контексты не сохраняются. Индекс/настройки содержат приватные данные.

URL/key/settings зафиксированы Models instance на обмен. HTTP redirects OFF,
timeout60s/response16MiB, system proxy retained и loopback direct, без direct
fallback/retries. MCP timeout15s. Generation reasoning_effort none/medium применяется
после controls; reported reasoning при OFF — ошибка без retries. Embeddings не
получают reasoning fields. Внутреннее поведение arbitrary endpoint не обещается.

## Проверки

Один `checks.core`: временный нейтральный Markdown/JSON, HTTP model mock и реальные
HTTP MCP процессы двух выбранных тикетов. Измеряет изоляцию контекста, retrieval,
индекс/cache/empty publication, JSON validation, LF/URI, key redaction, отсутствие
retries, OFF rejection и CLI startup. Не читает private corpus или .env, не вызывает
живую модель. Quality, реальная совместимость endpoint и ручной workflow остаются
проверкой человека; evaluation questions и пользовательские fixtures вне Git.
