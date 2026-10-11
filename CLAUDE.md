# CLAUDE.md

Отвечай по-русски, коротко. Долгие команды запускай с `timeout`, связь проверяй быстро.

## Работа на Маке пользователя через SSH (туннель Cloudflare)

Ты в облачном контейнере, а работать нужно на Маке. Подключайся к нему по SSH через туннель Cloudflare.

- Адрес туннеля меняется при каждом перезапуске. Пользователь присылает его в виде `https://АДРЕС.trycloudflare.com`.
- Пользователь на Маке: `niklavrukhin`.

1. Поставь cloudflared:
   `mkdir -p ~/.local/bin && curl -sSL -o ~/.local/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod +x ~/.local/bin/cloudflared`
   Если в контейнере нет `ssh`/`ssh-keygen`, поставь их: `apt-get install -y openssh-client`.
2. Создай ключ: `ssh-keygen -q -t ed25519 -N "" -f ~/.ssh/mac_key`. Сразу дай пользователю ОДНОЙ строкой команду для Терминала Мака:
   `mkdir -p ~/.ssh && echo '<публичный ключ>' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && echo готово`
   Потом жди, пока он напишет «добавил» или «сделал».
3. Пропиши `~/.ssh/config`:
   ```
   Host mac
     HostName АДРЕС.trycloudflare.com
     User niklavrukhin
     IdentityFile ~/.ssh/mac_key
     IdentitiesOnly yes
     BatchMode yes
     StrictHostKeyChecking accept-new
     ProxyCommand ~/.local/bin/cloudflared access ssh --hostname %h
     ServerAliveInterval 30
   ```
4. Проверка должна быть короткой, минутами не жди: `timeout 30 ssh mac 'echo alive'`.
   Если `curl` на адрес отвечает 530, туннель отвалился (обычно из-за VPN). Попроси пользователя перезапустить туннель и прислать новый адрес.
5. Работа:
   - команды на Маке: `ssh mac '…'`;
   - файлы: `scp`;
   - доступ к порту на Маке: `ssh -f -N -L <локальный>:127.0.0.1:<порт> mac`.
   Код в репо правь в облаке, коммить и пушь. На Мак переноси так: `git bundle` + `scp` + `git fetch` / `git merge --ff-only`
   (git на Маке по SSH не может достать пароль GitHub).
6. Секреты и ключи с Мака не печатай и в репо не клади.

## Репозиторий

- `index.html`: одностраничная читалка сценария «В поисках Герды» (без сборки, открывается в браузере).
