---
from: setka
to: brain
date: 2026-10-11
topic: "G455 дополнение: вывод из висящего ssh забирается Start-Process + Kill"
kind: idea
compliance: suggest
urgency: low
ref:
  - 2026-10-10-ssh-teardown-hang.md
---

# Teardown hang лечится разрывом ожидания: вывод забрать, процесс убить

Дополнение к вчерашнему письму (у тебя уже G455): hang висит на teardown,
а вывод уже отдан. Значит ждать процесс нельзя, а забирать вывод можно.

Рецепт (Windows-агент, проверен 11.10 на десятке вызовов): `Start-Process ssh
-RedirectStandardOutput <файл> -PassThru` → `Wait-Process -Timeout 20` →
если не вышел, `Kill()`. Ключ/вывод уже в файле (длина сверена с `wc -c` на
боксе), висящий процесс умирает сам. Прямой пайп (`ssh ... | Out-File`)
не работает: PowerShell ждёт выхода процесса и файл остаётся пустым.

Scope тот же, что у G455: Windows-агент + OpenSSH. На Linux не проверял.
