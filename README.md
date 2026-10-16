# День 32. Автоматизация ревью кода

```text
🔥 День 32. Автоматизация ревью кода

Настройте пайплайн, в котором ассистент анализирует новый PR.

Ассистент должен:

👉 получать diff и изменённые файлы
👉 использовать RAG (документация + код)
👉 выдавать текст ревью

Минимальный результат:

👉 пайплайн (например GitHub Action) запускается на PR
👉 ассистент получает diff
👉 возвращает:

- потенциальные баги
- архитектурные проблемы
- рекомендации

Результат:

Автоматическое AI-ревью кода
```

## Вывод

Headless Python-команда и reusable GitHub Actions workflow для произвольного
репозитория. Ассистент получает точный diff base → head, изменённые файлы,
отдельные чанки изменённого head и RAG-контекст документации **и кода base**.
Выдаёт Markdown с потенциальными багами, архитектурными проблемами и
рекомендациями; пустые списки допустимы. JSON-артефакт сохраняет точные ревизии,
merge base, переданные источники с путями/строками, выбор retrieval и доступный usage.
Результат рекомендательный: findings не блокируют merge, комментарии и другие
записи в GitHub не создаются. Ошибка инфраструктуры может сделать job красным;
не назначайте его обязательным status check.

Старые чат CLI, интерактивные `/settings`, MCP и их зависимости удалены.
Приватные данные предыдущих дней не читаются и не мигрируются.

## Установка

Нужны Python >=3.11, Git и OpenAI-compatible HTTP endpoint с Chat Completions
и embeddings. Generation и embedding model IDs задаются независимо, без
угадывания по каталогу. Качество и совместимость живой модели проверяет человек.

```sh
make setup
# Или: make setup PYTHON=/path/to/python
make check
```

После setup используйте `.venv/bin/python`. Прямые зависимости закреплены.
System HTTP/HTTPS proxy сохраняется, loopback direct; нет direct fallback при
ошибке proxy. Нет retries или скрытых пробных inference-вызовов.

## Локальный запуск

Создайте приватный connection JSON **вне репозитория** и ограничьте права 0600:

```json
{"base_url":"https://MODEL_HOST/v1","api_key":"OPTIONAL_API_KEY"}
```

Ключ можно опустить. JSON не коммитьте. Локальный CLI не читает `.env`, environment
keys или настройки целевого проекта. Модельная конфигурация явная:

```sh
.venv/bin/python -m review \
  --repo /path/to/project \
  --base FULL_40_CHARACTER_BASE_COMMIT_SHA \
  --head FULL_40_CHARACTER_HEAD_COMMIT_SHA \
  --connection-file /private/path/model-connection.json \
  --chat-model YOUR_CHAT_MODEL \
  --embedding-model YOUR_EMBEDDING_MODEL \
  --output /private/path/new-review-output
```

Обе ревизии должны существовать локально; символические refs не принимаются.
Сравнение именно `base..head`, а не triple-dot. Единственный merge base записывается
отдельно. Каталог output должен быть новым: `review.md` и `review.json` публикуются
одновременно через переименование подготовленного каталога. Артефакты содержат
фрагменты целевого кода и требуют такого же уровня доступа, как исходники.

Reasoning default OFF; `--reasoning-enabled` явно включает его. `--max-tokens`
по умолчанию 4096, допустимо 1..32768. HTTP timeout 60s, ответ до 16 MiB.
Наблюдаемое reasoning при OFF — ошибка без повторного платного вызова.
Отсутствие reported evidence не доказывает внутреннее поведение сервера.

## Автоматический PR pipeline

Reusable workflow — [.github/workflows/review.yml](.github/workflows/review.yml).
Он рассчитан на **GitHub.com**, использует `job.workflow_repository` и
`job.workflow_sha`, чтобы получить код самого закреплённого reviewer, даже когда
`github.repository` относится к вызывающему проекту. Эти поля описаны в
[официальной документации GitHub](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#example-usage-of-job-context-workflow-identity).

Сначала разместите этот reviewer в доступном Actions репозитории, включите
использование reusable workflow другими проектами при необходимости и сохраните
доверенный commit на default branch. В целевом проекте создайте caller ниже
**на default branch до открытия PR**. Замените `OWNER/REVIEWER` и `FULL_REVIEWER_COMMIT_SHA`
реальным репозиторием reviewer и полным проверенным commit SHA. Не указывайте
ветку или код из PR. Target может быть другим проектом; его структура и конфиги
не должны совпадать с этим репозиторием.

```yaml
name: Advisory AI PR review
on:
  pull_request_target:
    types: [opened, synchronize, reopened]
permissions:
  contents: read
  pull-requests: read
concurrency:
  group: ai-review-${{ github.repository }}-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  review:
    uses: OWNER/REVIEWER/.github/workflows/review.yml@FULL_REVIEWER_COMMIT_SHA
    with:
      model_url: ${{ vars.REVIEW_MODEL_URL }}
      chat_model: ${{ vars.REVIEW_CHAT_MODEL }}
      embedding_model: ${{ vars.REVIEW_EMBEDDING_MODEL }}
    secrets:
      model_api_key: ${{ secrets.REVIEW_MODEL_API_KEY }}
```

В repository Actions Variables целевого проекта задайте `REVIEW_MODEL_URL`,
`REVIEW_CHAT_MODEL`, `REVIEW_EMBEDDING_MODEL`; optional ключ — в Actions Secret
`REVIEW_MODEL_API_KEY`. Только CI adapter явно читает эти environment values,
переданные workflow, и read-only `github.token`. Ключ не передаётся модели в prompt,
Git command line, отчёты или config blobs. Отсутствующая конфигурация даёт явную
ошибку job; автоматического выбора модели или fallback нет.

`pull_request_target` получает секреты доверенного base workflow, в том числе для
fork PR: разрешайте это только с закреплённым проверенным reviewer. PR title,
branch names, bodies и целевые конфиги не подставляются в shell. Целевой проект
fetch-ится в отдельный bare Git repository по base SHA и `refs/pull/N/head`;
полученный head обязан совпасть с event SHA. Ничего из PR не checkout-ится и не
запускается: ни код, ни hooks, ни сборка, ни установка зависимостей. Устанавливаются
только зависимости доверенного reviewer. Модель получает недоверенные данные,
без tools; prompt injection и правильность выводов всё равно требуют оценки человеком.

Новый head отменяет старый job через concurrency. Read-only API повторно сверяет
base/head непосредственно перед публикацией отчёта; изменение закрывает старый
run ошибкой. Малое окно между последней проверкой и upload остаётся; артефакт
всегда содержит точные SHA и привязан к конкретному run. Summary содержит только
сообщение о готовности; Markdown и JSON доступны как artifact, retention 7 дней.
Для private target нужны доступ checkout к reviewer и права чтения target.
Не давайте этому job permissions `write`.

## Контекст и границы

Каждый запуск заново строит in-memory индекс закреплённого base, отправляет
его подходящие docs/code чанки embedding-модели батчами по 32, затем по одному
query embedding на изменённый файл. Постоянного кэша нет: повторный run оплачивает
эти embeddings снова. Для каждого файла независимо выбираются до двух docs и
двух code chunks по cosine; объединение дедуплицируется. Порог отсечения не
используется. JSON фиксирует corpus counts и выбранные источники: отсутствие
категории видно, модель получает только доступный контекст. Это выборка, а не
обещание проанализировать весь проект.

Git blobs читаются без worktree, external diff и textconv. Индексируются UTF-8
документы и распространённые текстовые языки/форматы; docs/README и документация
в `docs/` отделяются от кода. Symlinks, submodules, hidden paths, data/files,
известные secret/key имена, lock-файлы, build/vendor, бинарные и неизвестные
форматы исключены. Изменённые исключённые пути перечисляются в отчёте; если
все изменения исключены, run завершается ошибкой. Фильтр не распознаёт любой
секрет в обычном исходнике: оператор отвечает за допустимость отправки кода
выбранному endpoint.

Явные границы: 10000 tree entries на ревизию, 100 изменённых путей, 128 KiB на
подходящий blob, 4 MiB текста снимков суммарно, 256 KiB diff, 2000 base chunks,
окно до 60 строк / 12000 bytes, final prompt до 512 KiB. Превышение любой границы
— ошибка, а не обрезанный успешный анализ. Git read/fetch timeout 30s; job 20 минут.
Finding обязан ссылаться на переданный source ID и существующий path/line внутри
него. Проверяется структура и адрес источника, **не** смысл или правильность
вывода; ошибочный JSON не исправляется и не повторяется.

## Проверки

`make check` использует только временный Git-репозиторий и HTTP mocks. Проверяет
immutable snapshots при грязном worktree, RAG docs/code base и отдельный head,
exclusions, отсутствие исполнения diff/hooks, bounds, HTTP failure без retries,
reasoning OFF, evidence validation, CLI, event identity и stale fetched head.
Ни реальная модель, ни живой GitHub Actions запуск здесь не проверялись.
После установки caller проверьте вручную тестовый PR и новый push: artifacts,
отмену старого run, SHA, источники и качество рекомендаций.

[Архитектура](docs/architecture.md)
