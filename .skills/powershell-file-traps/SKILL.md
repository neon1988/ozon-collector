---
name: powershell-file-traps
description: >
  Ловушки PowerShell при редактировании файлов в Windows-проектах (BOM, CRLF,
  обнуление файла, cp1251 в выводе) и безопасные альтернативы.
Триггеры: "Set-Content", "Out-File", "BOM", "файл стал пустым", "0 байт",
"UnicodeEncodeError", "кириллица в логе", "powershell редактирование",
"editing python file from powershell", "git diff --check"
---

# PowerShell: ловушки при работе с файлами

## Контекст

Проекты на Windows, редактирование из PowerShell 5.1 (default shell opencode).
PowerShell 5.1 НЕ UTF-8 по умолчанию: `Set-Content -Encoding UTF8` пишет **BOM**,
консоль в cp1251, а `Get-Content`/`Set-Content` без `-Raw` режут файлы.

## Ловушка 1 (самая дорогая): обнуление файла

```powershell
# ❌ НИКОГДА: open('w') выполняется ПЕРВЫМ (truncate), потом читает уже пустой файл
[io.open(f,'w').write(io.open(f).read()) for f in $files]
```

Результат: файл 0 байт, все несохранённые правки потеряны.
Формально код «работает» (write успешен), поэтому ошибка неочевидна.

**✅ Безопасно:** отдельный python-скрипт, где чтение идёт ДО открытия на запись,
плюс проверка результата:

```python
data = io.open(f, encoding='utf-8-sig').read()      # сначала прочитали
io.open(f, 'w', encoding='utf-8', newline='').write(data)   # потом пишем
print(f, len(data))                                   # и проверяем размер
```

## Ловушка 2: BOM

`Set-Content -Encoding UTF8` в PS 5.1 → BOM (U+FEFF).
Python 3 ругается `SyntaxError: invalid non-printable character U+FEFF`.

**✅ Правильно:** любые правки Python-исходников — python-скриптом
(`io.open(..., encoding='utf-8', newline='')`), НЕ `Set-Content`.

Проверка (должно быть пусто):
```powershell
git diff --name-only | % { $b=[IO.File]::ReadAllBytes("D:\repo\$_"); if ($b[0] -eq 0xEF) { "BOM: $_" } }
```
Но проще/надёжнее python-скриптом с `git diff --name-only` + `startswith(b'\xef\xbb\xbf')`.

**Исключение:** `*.ps1` с кириллицей — BOM там нужен (PS 5.1 иначе читает как ANSI).
Сверяйся с HEAD: `git show HEAD:<file> | head -c 3 | Format-Hex`.

## Ловушка 3: CRLF/LF и хвостовая новая строка

- `Get-Content | Set-Content` теряет финальный перевод строки и может добавить CRLF.
- После любой Set-Content: `git diff --check` + проверка, что файл не «поплыл»
  целиком в диффе (признак — дифф на hundreds строк вместо своих правок).

## Ловушка 4: cp1251 в stdout

`UnicodeEncodeError: 'charmap' codec can't encode character '\u2192'` — вывод
скрипта с эмодзи/стрелками в консоль cp1251.

**✅:** в начале скрипта `sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')`
или пиши в файл (`io.open(..., encoding='utf-8')`) и читай через `read` tool.

## Ловушка 5: PowerShell парсит тут-строку в команде

`python -c "... \"u\" ..."` — кавычки ломаются, кириллица ломается.
**✅** многострочный код → write tool во временный `scripts/_patch.py`, запуск, `Remove-Item`.

## Ловушка 6: `git diff --name-only` + относительные пути

PowerShell-пайпы могут резолвить относительные пути не в том cwd (D:\Users\Neon
вместо репозитория) → `FileNotFoundException` на каждый файл.
**✅:** выполняй проверки python-скриптом с `os.chdir(REPO)`.

## Восстановление после обнуления файла (работает!)

1. `__pycache__/<name>.cpython-312.pyc` — последняя компиляция НЕпустого файла.
   `marshal.loads(open(pyc,'rb').read()[16:])` → code objects.
2. Сверить с HEAD: `git show HEAD:<file>`; `ast.parse` + сравнение
   top-level имён и docstring'ов → точный список потерянного.
3. `dis.dis(code)` по каждому потерянному объекту → восстановление исходника
   (co_names/co_consts/co_firstlineno дают почти всё, включая литералы).
4. Проверка: количество тестов == ожидаемому, полный suite зелёный.

`.pyc` от pytest содержит `-pytest-` версию (assertion rewriting) — она тоже
полезна, но может быть перезаписана пустым модулем после следующего запуска.
**Сначала копируй `__pycache__` в безопасное место**, потом чини.

## Launchers: fail loudly, проверяй СВОЙ процесс (инцидент 2026-09-26)

Инцидент: `scripts/launch_chrome.ps1` остался 3 байтами (только BOM) -> демон
не мог поднять CDP, 3 ретрая по 30с без внятной причины.

### Правило 1: лаунчер обязан падать громко

- маркеры `LAUNCHER-ERROR:` / `LAUNCHER-WARN:` / `LAUNCHER-DIAG:` **только ASCII**
  (кириллица в перенаправленном выводе PowerShell едет через кодировку консоли);
- провал -> `exit 1`, успех -> `exit 0` (тихий 0 при провале = потеря сигнала);
- DIAG печатается на **обоих** путях (успех и провал).

### Правило 2: проверяй СВОЙ процесс, а не "порт отвечает"

`/json/version` может отвечать от **чужого** Chromium: старый инстанс на том же
порту, либо новый процесс ушёл в уже открытый экземпляр (профиль занят другим
Chrome) и умер, а порт отвечает старым. Обязательные guard'ы:

1. **До запуска**: порт уже отвечает -> отказ (`LAUNCHER-WARN`), иначе ложный OK.
2. **После запуска**: `Get-Process -Id $targetPid` умер -> `LAUNCHER-ERROR:
   Chromium pid=N EXITED` + подсказка про занятый профиль.

### Правило 3: не жди мёртвый PID

Опрашивать порт после смерти процесса — потеря времени: `Invoke-RestMethod
-TimeoutSec 2` x 20 = до 40с, вывод лаунчера приходит ПОЗЖЕ, чем Python сдаётся,
и «лог пуст» становится ложной диагностикой.

- В цикле ожидания: `if (-not (Get-Process -Id $targetPid ...)) { break }`.
- В функции ожидания окна: процесс мёртв -> `return $false` сразу.
- Python-side: лаунчер вернул код -> **fail-fast**, не ждём остаток `wait_s`
  (проверено: 32.5с -> 6.6с на одном и том же кейсе).

### Правило 4: файл лога — фиксированный путь, а не «владение»

`NamedTemporaryFile(delete=False)` + «вернуть путь, вызывающий удалит» = мусор:
вызывающий забывает (2 файла в %TEMP% за прогон), а на Windows `os.unlink` падает,
пока файл держит дочерний процесс.

Правильно: путь фиксирован на порт (`%TEMP%\ozon_launch_<port>.log`),
перезаписывается при каждом запуске, уборка не нужна — после провала это улика.
Тесты изолируются через env `OZON_LAUNCH_LOG_DIR` (fixture в `tests/conftest.py`).

### Правило 5: «лог пуст» — проверять с паузой

Лаунчер — отдельный процесс и мог ещё писать. Прежде чем считать лог пустым,
перечитываем его несколько секунд (`_launcher_tail_after_grace`).

## Правило

Правлю файл → сразу:
1. `python -c "import ast; ast.parse(io.open(f,encoding='utf-8').read())"` (для .py)
2. размер файла `len(s)` > 0
3. целевой тест / `--collect-only`
4. в конце серии: `git diff --check`, BOM-скан, `git diff --stat` (нет ли «раздутых» файлов)

## Связанные

- [[ozon-collector-rev-29]] — правки 2.9 идут через python-скрипты, PowerShell Set-Content запрещён.


## Чек-лист после правки лаунчера (обязательно)

1. `len(raw) > 0` и ожидаемый размер (записать预期 рядом с кодом).
2. `ast.parse` для `.py`; для `.ps1` — `ParseFile` (иначе `.sh` уедет молча).
3. Негативный кейс: битый/пустой лаунчер -> внятный текст, НЕ пустой лог.
4. Негативный кейс: порт уже занят -> отказ, а не "OK".
5. Негативный кейс: профиль держит другой Chrome -> "EXITED" + подсказка.
6. Позитивный кейс: чистый профиль + свободный порт -> OK, exit 0.
7. Python-smoke: сообщение содержит `код возврата лаунчера`, путь к логу и
   маркер из лога (без гонки "лог пуст").
8. `git diff --check`, BOM-скан (BOM только у `.ps1`), lizard на изменённые файлы.
