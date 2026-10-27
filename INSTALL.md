# Installing Zhol on a new Mac

A step-by-step guide for a clean install: a new computer and an empty database. Every command can be copied into Terminal as is.

Zhol runs on macOS only. Windows and Linux are not supported.

Русская версия: [INSTALL_RU.md](INSTALL_RU.md).

## What you get

- A PostgreSQL database in Docker on this Mac. It is not reachable from outside.
- A web panel at `http://127.0.0.1:15173`.
- A Chrome collector that starts automatically when you log in to macOS.
- 2GIS and Google Maps collection across 147 categories, with Excel export.

Passwords, tokens and collected businesses stay on this computer. They are never pushed to GitHub.

## 1. Install the prerequisites

Open Terminal: Finder, then Applications, Utilities, Terminal.

**Homebrew**, the package manager for Mac. If `brew --version` already prints a version, skip this step.

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

At the end the Homebrew installer prints two commands, `echo ... >> ~/.zprofile` and `eval ...`. Run them, then close and reopen Terminal.

**Git, Python 3.12, Docker Desktop and Google Chrome:**

```bash
brew install git python@3.12
brew install --cask docker-desktop google-chrome
```

Check:

```bash
git --version
python3.12 --version
```

The second command must print `Python 3.12` or newer. The plain `python3` on macOS can be version 3.9, so this guide always uses `python3.12`.

**Start Docker Desktop:** open it from Applications, accept the terms and wait for "Engine running". Check:

```bash
docker info
docker compose version
```

Both commands must finish without errors.

## 2. Clone Zhol from GitHub

```bash
cd ~
git clone https://github.com/Yamanzho/zhol-parser-.git zhol
cd ~/zhol
git checkout v1.0.1
```

If the repository is private, Git asks you to sign in. The easiest way is to run `brew install gh && gh auth login` once and repeat `git clone`.

All further commands run from the `~/zhol` folder.

## 3. Install the Python dependencies

The Chrome collector and the Google Maps commands run from a local `.venv` environment in the project folder. The installer does not create it, so do this before installing:

```bash
cd ~/zhol
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[browser,collector]"
.venv/bin/python -m playwright install chromium
```

Always install with `.venv/bin/python -m pip`, not `pip`, so packages land in this environment.

Check:

```bash
.venv/bin/python -c "import zhol, sqlalchemy, selenium, playwright; print('ok')"
```

## 4. Install Zhol: .env and an empty database

In Finder, open the `zhol` folder and double-click `Установить Zhol.command` ("Install Zhol"). Or from Terminal:

```bash
cd ~/zhol
/bin/bash "Установить Zhol.command"
```

If macOS says the file is from an unidentified developer, right-click it, choose Open and confirm.

What the installer does:

1. Checks Python 3.12, Docker Desktop and Google Chrome.
2. Creates `.env` from the `.env.example` template and fills in random database passwords, keys and the local login. You don't invent or type any passwords. An existing `.env` is never overwritten.
3. Creates an empty PostgreSQL database (user `zhol`, database `zhol`) if none exists yet. An existing database is never removed.
4. Installs the Chrome collector for your macOS user as the `kz.duna.zhol.collector` service.

Never publish or share `.env`: it contains passwords.

## 5. Collection permissions

Each platform is opened separately, and only with written permission. An empty value keeps the platform closed, which is fine.

Open `.env` in TextEdit:

```bash
open -e ~/zhol/.env
```

Find these lines and fill in your permission only if you have one:

```text
ZHOL_2GIS_BROWSER_GRANT=
ZHOL_GOOGLE_BROWSER_GRANT=
```

Only two values are accepted: `written-browser-retention-local` or `written-browser-retention-commercial`. Permission for one platform does not open another. Save the file.

## 6. Start Zhol

Double-click `Запустить Zhol.command` ("Start Zhol") or run:

```bash
cd ~/zhol
/bin/bash "Запустить Zhol.command"
```

The first start builds the Docker images and takes 5-15 minutes. Then the script:

1. starts the database and waits until it is ready;
2. creates the tables in the new empty database;
3. starts the API, the panel, the job queues and backups;
4. opens the panel in your browser.

At the end you see `Жол запущен: http://127.0.0.1:15173/` ("Zhol is running").

Then restart the Chrome collector so it reads the permissions from step 5:

```bash
cd ~/zhol
scripts/zhol-collector-macos.sh restart
scripts/zhol-collector-macos.sh status
```

`status` must show `launchd: loaded` and a `pid`.

## 7. Open the panel and log in

Open in your browser:

```text
http://127.0.0.1:15173
```

Click "Открыть панель" ("Open panel"). No token is needed: login is local to this computer. The panel only opens at `127.0.0.1` or `localhost`. It is not reachable from other computers on the network.

The panel is in Russian. The top menu has four sections: "Сбор" (Collection), "Компании" (Companies), "Выгрузка" (Export) and "Дополнительно" (More).

## 8. Collect from 2GIS and Google Maps across 147 categories

In "Сбор" (Collection):

1. "Площадка" (Platform): 2ГИС or Google Maps.
2. "Город" (City): for example, Алматы.
3. Tick "Все категории" (All categories). The query field is disabled and Zhol uses all 147 categories from the `config/categories.yaml` catalog.
4. "Компаний на категорию" (Businesses per category): 1 to 3. One run takes at most 500 businesses, so 147 categories × 3 = 441 is the maximum.
5. Click "Начать сбор" (Start collection). "История сбора" (Collection history) opens with the progress.

To collect from both 2GIS and Google Maps, start two runs, one per platform. Or open "Дополнительно" (More), then "История сбора" (Collection history): the full form there lets you tick both platforms at once, pick several cities and set "Лимит на сочетание" (businesses per category).

For a single category or your own query, leave "Все категории" unticked and type into "Категория или запрос" (Category or query), for example `кофейня` (coffee shop).

During collection a separate Chrome window opens. Don't close it. If a platform shows an "I'm not a robot" check, a person completes it in that window. Zhol never solves or bypasses such checks.

A run can be paused, resumed or stopped. Businesses already collected stay in the database.

Tip for a first test: Almaty, "Все категории", 1 business per category. That is 147 cards per platform.

## 9. Export to Excel

- **"Выгрузка" (Export) section:** choose all jobs or one job, format Excel, and download.
- **One run:** "Дополнительно" (More), "История сбора" (Collection history), open a run and click "Скачать Excel" (Download Excel). The file contains only that run's businesses.
- **Combined 2GIS and Google file:** "История сбора" also offers `2gis-google.xlsx` with separate "2ГИС" and "Google Maps" sheets.

Files go to your browser's Downloads folder.

## 10. Stop and restart

Stop without deleting data:

```bash
cd ~/zhol
/bin/bash "Остановить Zhol.command"
```

Start again (Docker Desktop must be open):

```bash
cd ~/zhol
/bin/bash "Запустить Zhol.command"
```

A restart without program changes skips the image build and takes about a minute.

Chrome collector:

```bash
scripts/zhol-collector-macos.sh status
scripts/zhol-collector-macos.sh stop
scripts/zhol-collector-macos.sh start
```

After updating the program (`git pull`), first double-click `Обновить базу Zhol.command` ("Update Zhol database"): it backs up the database and updates its schema. Then run `Запустить Zhol.command` again.

## 11. Troubleshooting

| Message or symptom | What to do |
|---|---|
| "Нужен Python 3.12 или новее" (Python 3.12 or newer required) | `brew install python@3.12`, then run `Установить Zhol.command` again. |
| "Docker не установлен" or "Docker Desktop не запущен" (Docker missing or not running) | Open Docker Desktop, wait for "Engine running", retry. |
| "Google Chrome не найден" (Chrome not found) | `brew install --cask google-chrome`. Chrome must be in `/Applications`. |
| "Файла .env нет" (no .env file) | Run `Установить Zhol.command` first. |
| Panel does not open | Check `~/Library/Logs/Zhol/startup.log`. If a port is busy, stop Zhol, change `API_PORT` or `WEB_PORT` in `.env` and start again. |
| "Схема базы не совпадает с программой" (database schema mismatch) | Double-click `Обновить базу Zhol.command`, then `Запустить Zhol.command`. |
| A platform is marked "недоступна" (unavailable) | No permission set for it in `.env` (step 5). Set it, start Zhol and run `scripts/zhol-collector-macos.sh restart`. |
| A run shows "Ждет проверку в браузере" (waiting for a check in the browser) | The platform asks for a check in the Chrome window. Complete it by hand and click "Продолжить" (Resume). |
| `ModuleNotFoundError` or "playwright is not installed" in the collector log | Repeat step 3. |
| `Executable doesn't exist ... Chromium` | Run `.venv/bin/python -m playwright install chromium`. If `PLAYWRIGHT_BROWSERS_PATH` is set, remove it: `unset PLAYWRIGHT_BROWSERS_PATH`. |
| Yandex collection stops | Expected: an access-limit page stops the job. Bypassing it is not supported. |

Logs:

- startup: `~/Library/Logs/Zhol/startup.log`
- Chrome collector: `~/Library/Logs/Zhol/collector.log` and `collector.err.log`
- recent collector errors: `scripts/zhol-collector-macos.sh errors`

Data lives in the project's Docker volume. Stopping, restarting and reinstalling do not delete it. Never run `docker compose down -v`: it deletes the database.

More (in Russian): [docs/TROUBLESHOOTING_RU.md](docs/TROUBLESHOOTING_RU.md) and [docs/USER_GUIDE_RU.md](docs/USER_GUIDE_RU.md).
