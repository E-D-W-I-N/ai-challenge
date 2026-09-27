# Карта клиента дня 16

FastAPI отдаёт HTML, CSS и три обычных локальных скрипта; сборки нет.
`index.html` загружает `text.js` → `records.js` → `app.js`.

| Файл | Граница |
|---|---|
| `app/static/text.js` | Чистые Markdown/экранирование, формат чисел, stop/JSON, сравнение значений, предупреждения параметров. В браузере `ChatText`, под Node объект CommonJS. |
| `app/static/records.js` | `createChatRecords({state, $, el, iconButton, api, json, fmt})`: память, общий editor, профиль и меню, инварианты, ленивый MCP. State один, передаётся из app.js; запросов при создании фабрики нет. |
| `app/static/app.js` | State и DOM/API-помощники, список чатов/ветки, лента/поток/промпт/метрики, команды задачи, настройки, sidebar/подтверждения и init. Фабрика возвращает только функции/карты, нужные этой оболочке. |
| `app/static/style.css`, `index.html` | Светлая гибкая оболочка для ноутбука: sidebar, общая шапка, шесть страниц, нижние метрики. Компактные формы без отдельного phone/drawer режима. |

В Node `require(app/static/app.js)` подключает оба соседних модуля и отдаёт
`init`, `state`, `renderMarkdown`, `readStopLines`, `parseCommand`,
`parseResponseFormat`, `paramWarnings`, `fmt`. Init вызывает стенд после
установки DOM/fetch. В браузере app.js вызывает init сам.

## Чтение по задаче

Имена ниже — границы функций, а не фиксированные диапазоны строк.
Сначала найдите функцию через `rg`, затем читайте соответствующий раздел
[архитектуры](architecture.md); не загружайте монолит целиком для одной правки.

| Задача | Функции/файлы | Проверяемая граница | Архитектура |
|---|---|---|---|
| Markdown, формат, JSON/stop | text.js | Чистый разбор и один путь текста в карточку под Node | Клиент; Промпт, контекст и метрики |
| Панель → запрос | app.js: readPanel, applySettings, ensurePanelApplied, exchange | Реальные поля → payload под Node; контракт вызова в Python | Клиент; Промпт, контекст и метрики |
| Поток, метрики, промпт | app.js: exchange, refreshCurrent, renderFeed, usageLine, showPrompt | Success/error, committed prompt, отмена/смена области; серверные суммы | Промпт, контекст и метрики; Клиент |
| CRUD и ошибки editor | records.js: startRecordEdit, loadMemory, editWorking/editMemory, editInvariant | Scope/id/различающий тип, один PATCH Enter/focusout, текст после отказа | Память и профиль; Инварианты и сторож; Клиент |
| Профиль и аватар | records.js: loadProfile, saveProfile, toggleProfileMenu | Partial PATCH/dirty/error под Node; глобальность и prompt в Python | Память и профиль; Клиент |
| Машина задачи | app.js: parseCommand, runCommand, taskApi | Двойной Enter и смена чата при PATCH под Node; stage/gate/atomic в Python | Состояние задачи |
| Ветка и очистка | app.js: forkFrom, openAgent; app/agent.py, store.py | Владение, persist/restart/seq и заполненные слои до очистки | Ветвление; Хранение и очистка |
| MCP | records.js: loadMcp, toolsVisible, stopMcpPolling; app/mcp.py | Список/поздний GET под Node; локальные initialize/list/call/env/stop в Python | MCP |
| Оболочка/CSS | app.js: showWorkspace, showSettings, setCollapsed, confirmBox; style.css, index.html | Сохранение черновика и mounted форм; ручная геометрия/клавиши | Клиент; Проверки |

Проверки находятся в `checks/browser_check.js` (настоящий клиент),
`checks/dom.js` (DOM и сценарные API/SSE), `checks/run_checks.py`
и отдельных restart/two_processes/MCP-фикстурах. `make check` — всё ядро,
`make check-browser` — только Node. Стенд не реализует второй сервер:
ожидаемые промпт, суммы и переходы проверяются независимо на Python.
Матрицы вариаций оформления и сценарий batch-100 больше не являются
автоматическим обещанием; small isolation/deep-copy и межпроцессное владение
остаются проверяемыми границами.

## Ручной прогон

Используйте offline API-фикстуры/временную базу или подготовленное демо.
Не обращайтесь к пользовательской базе, `.env` или живой LLM.

1. Откройте desktop 1440 px, ноутбук 1100/900 px и окно с zoom 125–150%.
   Пройдите все шесть страниц: формы доступны при прокрутке, длинные слова
   переносятся, код/JSON и нижняя строка метрик прокручиваются внутри области.
2. Сверните/откройте список: видна одна кнопка, фокус переходит к ней,
   скрытый список не участвует в Tab, предпочтение переживает перезагрузку.
   Проверьте центры SVG, строки текста, шапку дня и baseline метрик/заметок.
3. Откройте аватар, просмотрите глобальный профиль, перейдите к editor;
   Escape возвращает фокус. Проверьте черновик/ошибку после смены областей,
   подписи full/window/summary, guard без метрик и пустой/down MCP.
4. Один editor: смените текст/тип, Enter, focusout, Escape, пустую/неизменную
   правку. Одно подтверждение: Tab, Escape и возврат фокуса; стрелки/Home/End
   на вкладках, видимый outline и prefers-reduced-motion.

Это проверка представления; она не заменяет автоматические границы хранения,
payload, идентификаторов, отмены позднего GET и асинхронных гонок.
