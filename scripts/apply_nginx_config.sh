#!/bin/bash
# Скрипт для применения изменений конфигурации nginx
#
# Источник — `config/nginx_setka.conf`, а НЕ `config/setka.conf.editable`.
#
# Почему (01.11, D-096): прежним источником был шаблон `setka.conf.editable` —
# с `listen 80`, плейсхолдером `YOUR_DOMAIN` и без TLS. Скрипт из документации
# (docs/OPERATIONS.md, docs/START_HERE.md) копировал его на бокс, то есть
# сносил с техдомена HTTPS и уводил `location /` в приложение всё подряд,
# включая сканеры (см. #251 и D-096). Шаблон остался — но он для ЧУЖОГО
# развёртывания, и ставить его на наш бокс нельзя.
SOURCE_FILE="/home/valstan/SETKA/config/nginx_setka.conf"
TARGET_FILE="/etc/nginx/conf.d/setka.conf"
BACKUP_FILE="/etc/nginx/conf.d/setka.conf.backup.$(date +%Y%m%d_%H%M%S)"

# Страховка от подстановки шаблона: плейсхолдер в файле = заведомо неверная
# конфигурация (сертификат YOUR_DOMAIN, домен YOUR_DOMAIN).
if grep -q 'YOUR_DOMAIN' "$SOURCE_FILE" 2>/dev/null; then
    echo "Ошибка: в $SOURCE_FILE остался плейсхолдер YOUR_DOMAIN — это шаблон, а не конфиг бокса."
    exit 1
fi

echo "Применение конфигурации nginx..."
echo ""

# Проверка существования файла
if [ ! -f "$SOURCE_FILE" ]; then
    echo "Ошибка: Файл $SOURCE_FILE не найден!"
    exit 1
fi

# Создать бэкап
echo "Создаю бэкап текущей конфигурации: $BACKUP_FILE"
sudo cp "$TARGET_FILE" "$BACKUP_FILE" || {
    echo "Ошибка: Не удалось создать бэкап!"
    exit 1
}
echo "✓ Бэкап создан"
echo ""

# Проверить синтаксис перед применением
echo "Проверяю синтаксис конфигурации..."
sudo nginx -t -c /etc/nginx/nginx.conf 2>&1 | grep -q "syntax is ok" && {
    echo "✓ Текущая конфигурация валидна"
} || {
    echo "⚠ Предупреждение: Текущая конфигурация имеет ошибки"
}

# Копировать новую конфигурацию
echo "Копирую новую конфигурацию..."
sudo cp "$SOURCE_FILE" "$TARGET_FILE" || {
    echo "Ошибка: Не удалось скопировать файл!"
    echo "Восстанавливаю из бэкапа..."
    sudo cp "$BACKUP_FILE" "$TARGET_FILE"
    exit 1
}
echo "✓ Файл скопирован"
echo ""

# Проверить синтаксис новой конфигурации
echo "Проверяю синтаксис новой конфигурации..."
if sudo nginx -t 2>&1 | grep -q "syntax is ok"; then
    echo "✓ Синтаксис корректен"
    echo ""
    echo "Применить изменения? (y/n)"
    read -r response
    if [[ "$response" =~ ^[Yy]$ ]]; then
        echo "Перезагружаю nginx..."
        sudo systemctl reload nginx && {
            echo "✓ Nginx успешно перезагружен!"
            echo ""
            echo "Конфигурация применена успешно!"
        } || {
            echo "✗ Ошибка при перезагрузке nginx!"
            echo "Восстанавливаю из бэкапа..."
            sudo cp "$BACKUP_FILE" "$TARGET_FILE"
            sudo systemctl reload nginx
            exit 1
        }
    else
        echo "Изменения не применены. Файл обновлен, но nginx не перезагружен."
        echo "Для применения запустите: sudo systemctl reload nginx"
    fi
else
    echo "✗ Ошибка синтаксиса в новой конфигурации!"
    echo "Восстанавливаю из бэкапа..."
    sudo cp "$BACKUP_FILE" "$TARGET_FILE"
    sudo nginx -t
    exit 1
fi

