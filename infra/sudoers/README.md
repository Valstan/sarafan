# sudo на боксе: разработчику — полный, движку — никакого

Граница изоляции на проде проходит **по пользователю**, а не по списку команд.

| Пользователь | Что под ним работает | sudo |
|---|---|---|
| `valstan` | воркер Celery, релизная процедура, агент-разработчик | `NOPASSWD: ALL` ([`valstan-dev`](valstan-dev)) |
| `opencode` | нейро-движок классификатора (`opencode-web`) | **нет вообще**; юнит запечатан `NoNewPrivileges=yes` |

## Почему так (решение владельца 21.10)

До этого здесь лежал `setka-worker` — белый список привилегированных команд для
`valstan`. Он закрывал не то: ограничивал **разработчика**, а изолировать надо было
**прод-нейросеть**. У движка свой пользователь `opencode`, sudo у него нет и не
было, а юнит уже держит `NoNewPrivileges=yes` — то есть песочница существовала и
без белого списка. Цена при этом платилась настоящая: каждая новая
привилегированная команда в релизе требовала правки sudoers **в том же PR**,
иначе на боксе `sudo: a password is required` — и это выглядело как поломка
прода. Разработка бюрократизировалась ради свойства, которого у неё не было.

Сторож — [`tests/test_infra/test_sudo_policy.py`](../../tests/test_infra/test_sudo_policy.py).
Он падает, если:

- в `infra/sudoers/` снова появится перечень команд для `valstan` (регрессия к белому списку);
- кто-то выдаст права пользователю `opencode` — движку sudo не положен ни в каком виде;
- из юнита движка пропадёт `NoNewPrivileges=yes` или сменится `User`;
- из [`docs/OPERATIONS.md`](../../docs/OPERATIONS.md) исчезнет описание политики.

## Установка / обновление

```bash
sudo install -m 0440 -o root -g root infra/sudoers/valstan-dev \
    /etc/sudoers.d/valstan-dev
sudo visudo -c
sudo rm -f /etc/sudoers.d/setka-worker     # старый белый список, если ещё лежит
```

Проверка:

```bash
sudo -n true && echo developer-ok           # без пароля — так и должно быть
sudo -n -u opencode true                    # отказ: у движка прав нет
systemctl show opencode-web -p User -p NoNewPrivileges
                                            # User=opencode, NoNewPrivileges=yes
```

## Что остаётся human-gate'ом

Полный sudo у `valstan` — про **механику** доступа, а не про разрешение. Гейт на
destructive-операциях с прод-данными (`ALTER`, `DROP`, `DELETE`, `UPDATE`,
`systemctl stop`, ротация токенов) остаётся человеческим: [`AGENTS.md`](../../AGENTS.md),
[#025](../../../brain_matrica/cross-project-ideas/ideas/025-destructive-prod-confirm-same-turn.md).
`NOPASSWD` снимает вопрос «дадут ли пароль», а не вопрос «стоит ли это делать».
