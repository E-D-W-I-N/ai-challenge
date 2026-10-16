# Архитектура дня 32

Python >=3.11, headless advisory review. `review.git` читает точные Git blobs;
`rag` выбирает base docs/code; `shared_models` предоставляет совместимый HTTP,
system proxy и reasoning contract; `review.runner` валидирует и публикует отчёт.
`review.__main__` — локальный вход с явным connection file. `review.ci` — узкий
адаптер доверенного GitHub.com workflow. Прямая зависимость: `httpx[socks]`.
Нет чата, MCP, web, target plugin/config loader или постоянного индекса.

## Ревизии и границы доверия

CLI принимает только полные lowercase 40-character SHA. Оба объекта должны
быть ровно commit, с одним merge base. Сравнение exact base → head; merge base
фиксируется, но не заменяет base. Деревья и blobs читаются Git plumbing, без
checkout/worktree/filters/hooks. External diff и textconv выключены;
replace objects, global/system Git config и fsmonitor отключены. Локальный
repo config не исполняется; допустимы только фиксированные read-only операции.
Git argv не содержит непроверенных ref/option строк модели.

Целевые пути и тексты — недоверенные данные. Mode только regular blob;
symlink/submodule/hidden/secret/build/vendor/binary/unknown formats исключены.
Изменённые исключения сохраняются в metadata. Превышения размера подходящего
файла, corpus, chunks, diff или prompt — явные ошибки без успешного усечения.
Неподдерживаемые категории файлов пропускаются по задокументированной policy.
Репозиторий ограничен 10000 entries, diff 100 paths / 256 KiB, blob 128 KiB,
общий текст 4 MiB, base chunks 2000, chunk 60 lines / 12000 bytes, prompt 512 KiB.
Git stdout bounded, timeout 30s, stderr не отражается в диагностике.

## RAG и запрос

Для одного immutable base собираются docs и code windows с ID, revision,
path, start/end и text. Все подходят в in-memory corpus: embeddings батчами32,
без постоянного кэша, затем query embedding path+diff для каждого файла.
Cosine сортируется детерминированно, квота два docs + два code на файл;
объединение deduplicated. Missing category видна через corpus counts.
Чанки изменённого head добавляются отдельным полем и kind=changed_head.
Diff и список изменённых путей берутся из тех же base/head.

Один final Chat Completions получает JSON данных и фиксированный system prompt.
Нет tools, retries, fallback или выполнения target tests. Модель отвечает только
JSON bugs/architecture/recommendations arrays, включая пустые. Каждый finding
имеет path, integer line, непустые source_ids и text. Валидатор запрещает
неизвестные IDs, duplicate JSON keys и ссылки за пределами переданных строк;
source с этим path/line должен входить в finding source_ids. Семантика, релевантность,
цитаты и отсутствие багов автоматически не доказываются. Raw отказ не публикуется.

## Подключение и результат

CLI явно загружает bounded connection JSON вне целевого проекта; chat/embedding
IDs, reasoning и max_tokens приходят аргументами. Нет dotenv/environment key
loader локально. Connection frozen на запуск; ключ скрыт из repr и не попадает
в prompt, report, Git argv или exception body. HTTP ошибки фиксированы;
response bound 16 MiB, timeout 60s, redirects OFF. System HTTP/HTTPS proxy
сохраняется, loopback direct, без direct fallback. Reasoning OFF по умолчанию,
reasoning_effort none/medium применяется после payload controls. Наблюдаемое
reasoning при OFF отклоняется без повторного вызова; внутреннее поведение
произвольного сервера не обещается. Embeddings не получают reasoning controls.

Отчёт содержит base/head/merge_base, reviewed/changed/excluded paths, corpus
counts, retrieval selections, все переданные source chunks и безопасно выбранные
серверные token counters каждого запроса. При отсутствии usage счётчики пусты.
Raw responses/credentials не пишутся. Markdown экранирует model/repo text;
JSON сохраняет данные. Оба файла 0600 собираются в private temp directory и
атомарно переименовываются в новый output directory; существующий output запрещён.
Артефакты содержат приватный код target и не должны коммититься.

## GitHub Actions

Caller на default branch целевого проекта вызывает reusable workflow по полному
проверенному SHA. `pull_request_target` opened/synchronize/reopened;
permissions contents:read + pull-requests:read, PR concurrency cancel-in-progress.
Workflow получает trusted code по job.workflow_repository/job.workflow_sha,
не по github.repository caller. Actions закреплены полными SHA.

CI читает только явные trusted model Variables/Secret, github.token и служебные
GitHub environment values. Event JSON проверяет integer PR number, repository,
base/head SHA; PR-controlled branch/title/body не используются. Fixed GitHub API
origin сверяет open PR и exact base/head. Отдельный временный bare repo fetch-ит
base SHA и refs/pull/N/head из base repository; fetched SHA должны совпасть.
Token для Git передаётся env extraheader, не argv/config/remote URL. Target
никогда не checkout-ится; его dependencies/hooks/config/build не исполняются.

После модели API снова сверяет base/head до publish. Concurrency снимает старый
run при push, но минимальное окно до artifact upload остаётся; SHA в отчёте
обязательны. Summary только fixed text, artifacts Markdown/JSON retention7d.
Нет GitHub comments/write API/merge gate. Missing config/HTTP/model format/bounds/
stale revisions дают failed job без нового отчёта. Интеграция GitHub.com;
GitHub Enterprise Server отдельно не поддержан и не проверялся.

## Проверки

`make check`: нейтральный временный Git fixture, реальные immutable commits,
dirty worktree, secret/symlink/vendor/binary filtering, external diff marker,
mock HTTP embeddings/chat, source metadata, usage, OFF reasoning rejection,
HTTP no-retry, malformed source/line failures, bounds, CLI и CI event/fetch.
Ни пользовательские data/.env, ни live inference не используются. Проверки
измеряют поведение; docs text и model quality не тестируются. Живой workflow,
совместимость модели и качество review остаются ручной проверкой после установки.
