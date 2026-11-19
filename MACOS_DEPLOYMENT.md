# Развёртывание на Mac mini без Docker

Инструкция рассчитана на один постоянно работающий процесс бота, подключение по SSH и SQLite. Бот получает сообщения через long polling, поэтому входящий порт и домен ему не нужны. Mac должен иметь исходящий доступ к Telegram API. Для этой схемы не запускайте второй экземпляр бота с тем же токеном.

## 1. Подготовка Mac

Подключитесь по SSH:

```sh
ssh USER@MAC_HOST
```

Замените `USER` и `MAC_HOST` на имя пользователя macOS и адрес Mac mini. Установите [Homebrew](https://brew.sh/), если его ещё нет, затем в SSH-сессии:

```sh
brew install python@3.13 git
python3.13 --version
git --version
```

Если после установки команда `brew` или `python3.13` не находится, добавьте Homebrew в `PATH` по инструкции, которую выводит установщик Homebrew, и переподключитесь по SSH. Python 3.13 входит в поддерживаемый проектом диапазон.

Отключите автоматический сон Mac при питании от сети в настройках macOS. Иначе бот перестанет отвечать, пока компьютер спит. По возможности включите автоматический запуск после отключения питания. Если включён FileVault, после полной перезагрузки может потребоваться локально разблокировать диск, прежде чем запустятся сервисы и SSH.

## 2. Код и окружение

Выполните под обычным пользователем, **без `sudo`**:

```sh
APP_DIR="$HOME/apps/secret-santa"
DATA_DIR="$HOME/santa-data"
mkdir -p "$HOME/apps" "$DATA_DIR/backups"
chmod 700 "$DATA_DIR" "$DATA_DIR/backups"
git clone https://github.com/belikebigjohn/belike-Secret_Santa-bot.git "$APP_DIR"
cd "$APP_DIR"
python3.13 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python scripts/init_env.py
echo "$DATA_DIR"
nano .env
```

В `.env` заполните как минимум эти строки:

```dotenv
BOT_TOKEN=ТОКЕН_ОТ_BOTFATHER
OWNER_IDS=ВАШ_ЧИСЛОВОЙ_TELEGRAM_ID
DATABASE_URL=sqlite:////Users/USER/santa-data/santa.db
LOG_LEVEL=INFO
```

Для `DATABASE_URL` подставьте фактический абсолютный путь из `echo "$DATA_DIR"`: к пути `/Users/USER/santa-data/santa.db` добавьте префикс `sqlite:///`, в результате после двоеточия окажутся четыре `/`. Не пишите `$HOME` в `.env`: бот не подставляет туда переменные shell. Для нескольких владельцев укажите ID через запятую. Сохраните файл и ограничьте доступ:

```sh
chmod 600 .env
```

`POSTGRES_PASSWORD`, созданный скриптом, при SQLite не используется; строку можно оставить. Токен и `.env` не отправляйте в Git.

## 3. Первый запуск и проверка

Из каталога проекта:

```sh
.venv/bin/python -m santa.main
```

При первом запуске бот создаст базу и применит миграции Alembic. В Telegram проверьте `/start` и `/admin` со своей учётной записи. Создайте тестовую игру, присоедините вторую учётную запись и проверьте назначение. Остановите пробный процесс сочетанием `Ctrl+C` до настройки автозапуска.

Проверьте базу:

```sh
.venv/bin/python -m santa.healthcheck
echo $?
```

Код `0` означает, что база доступна. Эта команда проверяет базу; работу Telegram и фонового отправщика проверяйте в самом боте, в `/admin` → «Состояние» и по журналу.

## 4. Автозапуск через launchd

Сервис будет запускаться при загрузке macOS от имени вашего обычного пользователя. В SSH-сессии заново задайте переменные, если переподключались:

```sh
APP_DIR="$HOME/apps/secret-santa"
DATA_DIR="$HOME/santa-data"
BOT_USER="$(id -un)"
cat > "$DATA_DIR/com.belike.secret-santa.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.belike.secret-santa</string>
  <key>UserName</key><string>${BOT_USER}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${APP_DIR}/.venv/bin/python</string>
    <string>-m</string>
    <string>santa.main</string>
  </array>
  <key>WorkingDirectory</key><string>${APP_DIR}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${DATA_DIR}/bot.stdout.log</string>
  <key>StandardErrorPath</key><string>${DATA_DIR}/bot.stderr.log</string>
</dict>
</plist>
EOF
sudo install -o root -g wheel -m 644 "$DATA_DIR/com.belike.secret-santa.plist" /Library/LaunchDaemons/com.belike.secret-santa.plist
sudo plutil -lint /Library/LaunchDaemons/com.belike.secret-santa.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.belike.secret-santa.plist
```

Проверьте состояние и журнал:

```sh
sudo launchctl print system/com.belike.secret-santa
tail -n 100 "$DATA_DIR/bot.stderr.log"
```

Затем снова отправьте `/start` боту и проверьте `/admin` → «Состояние». Для просмотра журнала в реальном времени используйте `tail -f "$DATA_DIR/bot.stderr.log"`. Если `launchctl bootstrap` сообщает, что сервис уже загружен, не создавайте второй: используйте команду перезапуска из следующего раздела.

## 5. Резервная копия и обновление

База содержит игры, анкеты и назначения. Делайте копию регулярно и обязательно перед обновлением. Скрипт использует резервное копирование SQLite, безопасное для работающей базы, и проверяет целостность результата:

```sh
APP_DIR="$HOME/apps/secret-santa"
DATA_DIR="$HOME/santa-data"
cd "$APP_DIR"
.venv/bin/python scripts/backup_sqlite.py "$DATA_DIR/santa.db" "$DATA_DIR/backups/santa-$(date +%Y%m%d-%H%M%S).db"
```

Периодически переносите копии на другой носитель или сервер: файл на том же Mac не защитит от потери диска. Сначала сохраните изменения на рабочем компьютере коммитом и отправьте их в `origin/main`. `git pull` на Mac получит только уже отправленные коммиты.

Если бот сейчас запущен вручную в SSH-окне, остановите его сочетанием `Ctrl+C`. Если бот уже работает как сервис `launchd`, остановите его командой `sudo launchctl bootout system/com.belike.secret-santa`. Перед обновлением сделайте копию базы командой выше. Затем выполните на Mac:

```sh
cd "$HOME/apps/secret-santa"
git pull --ff-only origin main
.venv/bin/python -m pip install -e .
```

Для ручного запуска после обновления выполните `.venv/bin/python -m santa.main`. Если используете `launchd`, снова запустите сервис и проверьте базу:

```sh
sudo launchctl bootstrap system /Library/LaunchDaemons/com.belike.secret-santa.plist
.venv/bin/python -m santa.healthcheck
```

Команда `bootout` останавливает бот; `bootstrap` снова включает автозапуск. Миграции применятся при старте. Если установка или запуск не удались, проверьте `bot.stderr.log` и восстановите работоспособность до следующего обновления. Для перезапуска без обновления:

```sh
sudo launchctl kickstart -k system/com.belike.secret-santa
```

Не копируйте работающий `.db` обычной командой `cp`: используйте скрипт выше. Для восстановления остановите сервис через `bootout`, сохраните повреждённую базу отдельно, скопируйте проверенную резервную копию на место `santa.db` и выполните `bootstrap`. Восстановление вернёт базу к моменту создания копии.

## Если появляется `TelegramConflictError`

Сообщение `terminated by other getUpdates request` означает, что другой процесс одновременно получает обновления того же бота. Пока два процесса работают, они будут прерывать запросы друг друга; кратковременное `Connection established` не означает, что конфликт исчез.

1. Остановите ручной запуск сочетанием `Ctrl+C`.
2. На Mac найдите другие процессы бота и проверьте системный сервис:

   ```sh
   ps aux | grep '[s]anta.main'
   sudo launchctl print system/com.belike.secret-santa
   ```

   Если сервис уже работает, проверяйте его через Telegram и журнал; не запускайте `.venv/bin/python -m santa.main` параллельно. Если найден другой ручной процесс, остановите его в его терминале или отправьте `kill PID`, подставив его идентификатор из вывода `ps`.
3. Если на Mac остался только один процесс, остановите старый экземпляр на другом компьютере, сервере, хостинге или в Docker. Проверьте все места, где использовался тот же `BOT_TOKEN`.
4. После остановки второго экземпляра оставьте один способ запуска и проверьте `/start`. Если конфликт повторяется, значит второй процесс всё ещё работает где-то с этим токеном.

Не публикуйте токен и не вызывайте `getUpdates` вручную для диагностики: это само создаст ещё один конкурирующий запрос.

## Ограничения схемы

Один процесс и SQLite подходят для первого запуска на Mac mini, в том числе для игры на 80 участников, для которой проверена жеребьёвка. На массовой нагрузке лучше перейти на отдельный PostgreSQL; перенос существующей SQLite-базы потребует отдельной миграции данных. Одновременно запускать несколько процессов polling с одним токеном нельзя. Telegram API и база не имеют общей транзакции: при редком сбое между отправкой и записью результата сообщение может повториться.
