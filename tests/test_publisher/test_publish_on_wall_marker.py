"""P215: издатель различает «пост на стене» и «пост в очереди предложений».

Журнал публикаций отвечает на вопрос «что читают подписчики». Ответ VK в обоих
случаях одинаков — настоящий id поста, — различается только ПУТЬ публикации:

* community-токен **самой** группы ⇒ пост всегда на стене;
* user-токен ⇒ пост мог уйти в «Предложенные записи» (если аккаунт не админ).

Издатель отдаёт метку ``on_wall``, журнал по ней решает, писать строку или нет.
Метки community-токена бывают двух видов: ``community-token`` (один клиент) и
``community-token:<NAME>`` (перебор кандидатов) — обе означают публикацию от
имени самой группы, обе ⇒ на стене.
"""

import pytest

from modules.publisher.vk_publisher_extended import VKPublisher


class _DummyClient:
    """Клиент ВК без сети: отвечает id поста на любой вызов."""

    def __init__(self, post_id: int = 555):
        self.calls = []
        self._post_id = post_id

    def api_call(self, method, params):
        self.calls.append((method, params))
        return {"response": {"post_id": self._post_id}}


@pytest.mark.asyncio
async def test_community_token_publish_is_on_wall():
    # Один community-токен на группу — обычный путь прод-публикации.
    publisher = VKPublisher(vk_client=_DummyClient(), community_tokens={12345: "tok"})
    publisher._community_clients[12345] = _DummyClient()

    result = await publisher.publish_bulletin(group_id=12345, text="bulletin")

    assert result["success"] is True
    assert result["via"] == "community-token"
    assert result["on_wall"] is True


@pytest.mark.asyncio
async def test_community_candidates_publish_is_on_wall():
    # Перебор community-кандидатов даёт метку с суффиксом — она тоже «от группы».
    # Регресс: при строгом сравнении с «community-token» такие посты молча
    # выпадали бы из журнала, то есть весь прод-путь тихо перестал бы писаться.
    publisher = VKPublisher(
        vk_client=_DummyClient(),
        community_candidates={12345: [("COMM_12345", "tok")]},
    )
    publisher._community_clients["COMM_12345"] = _DummyClient()

    result = await publisher.publish_bulletin(group_id=12345, text="bulletin")

    assert result["success"] is True
    assert result["via"] == "community-token:COMM_12345"
    assert result["on_wall"] is True


@pytest.mark.asyncio
async def test_user_token_publish_is_not_on_wall():
    # У-токен без прав админа ⇒ пост может лежать в «Предложенных записях».
    publisher = VKPublisher(vk_client=_DummyClient())

    result = await publisher.publish_bulletin(group_id=12345, text="bulletin")

    assert result["success"] is True
    assert result["via"] == "publish-token"
    assert result["on_wall"] is False


@pytest.mark.asyncio
async def test_suggested_post_is_on_wall():
    # Принятие предложенного поста — публикация ОТ СООБЩЕСТВА (from_group=1):
    # читатели видят пост, значит строка журнала нужна.
    publisher = VKPublisher(vk_client=_DummyClient())

    result = await publisher.publish_suggested(group_id=12345, post_id=777)

    assert result["success"] is True
    assert result["on_wall"] is True
