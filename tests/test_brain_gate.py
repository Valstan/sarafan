"""Гейт системы мозга проекта: сторож на то, ради чего он заведён.

Система мозга (`docs/PROJECT_LENS.md`, `docs/EXPERTS.md`, `docs/INTAKE.md`) —
это документы, которые живут дольше кода, который они описывают. У них один
неприятный класс отказа: они **выглядят живыми, пока в них написано «как
было»**. Никто не падает, тесты зелёные, а новая сессия читает линзу, верит ей
и идёт в каталог, которого уже нет.

Гвозди этого файла:

* `test_real_repo_is_consistent` — главная проверка: настоящий репозиторий
  проходит гейт. Пока линза, реестр и порядок приёма расходятся с кодом —
  краснеет CI;
* `test_missing_role_is_caught` — роль, выпавшая из реестра, не должна молчать:
  совет без неё превращается в монолог;
* `test_role_without_output_field_is_caught` — «посмотрел и сказал» не считается
  выходом: у роли обязателен проверяемый выход;
* `test_vanished_path_is_caught` — каталог, который переименовали, а линза про
  него ещё помнит;
* `test_unreadable_brain_is_not_success` — «не смог посмотреть» обязано
  отличаться от «сошлось» (G375): у отсутствующих файлов нет права на exit 0.
"""

from __future__ import annotations

import sys

import pytest

from scripts import brain_gate

ROLE_TEMPLATE = """
### {title}

- **Что делает:** делает что-то конкретное для проекта.
- **Что запускает:** запускает вот это.
- **Что отдаёт дальше:** отдаёт вот это.
- **Проверяемый выход:** выход виден и проверяем.
"""

ALL_ROLES = (
    ("Координатор", "координатор"),
    ("Программист", "программист"),
    ("Разведчик", "разведчик"),
    ("Критик", "критик"),
    ("Тестер", "тестер"),
    ("Эксперт по антропогенезу", "антропогенез"),
    ("Шут", "шут"),
    ("Эксперт по эффективности и дублям", "эффективност"),
    ("Эксперт по нужности", "нужност"),
)


def build_registry(roles=ALL_ROLES, drop_field=None, drop_role=None) -> str:
    """Собрать макет реестра экспертов."""
    out = ["# Реестр\n"]
    for title, key in roles:
        if drop_role and key == drop_role:
            continue
        section = ROLE_TEMPLATE.format(title=title)
        if drop_field:
            section = "\n".join(
                line for line in section.split("\n") if not line.startswith(f"- **{drop_field}:**")
            )
        out.append(section)
    out.append("Маршрут простой задачи: координатор → программист.\n")
    out.append("Потолок совета — 3 эксперта.\n")
    return "\n".join(out)


def run(*argv) -> int:
    old = sys.argv
    sys.argv = ["brain_gate.py", *argv]
    try:
        return brain_gate.main()
    finally:
        sys.argv = old


@pytest.fixture
def aim(monkeypatch, tmp_path):
    """Разложить минимальный макет системы мозга и перенацелить на него гейт."""

    def _aim(
        registry: str = None,
        agents: str = None,
        lens: str = "# Линза\n\nПроект: `docs/`\n",
        intake: str = "# Приём\n\nПорядок приёма: `docs/`\n",
    ):
        docs = tmp_path / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "PROJECT_LENS.md").write_text(lens, encoding="utf-8")
        (docs / "EXPERTS.md").write_text(
            registry if registry is not None else build_registry(), encoding="utf-8"
        )
        (docs / "INTAKE.md").write_text(intake, encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text(
            (
                agents
                if agents is not None
                else (
                    "# Канон\n\nИзменился проект — изменилась линза и реестр.\n\n"
                    "Линза: docs/PROJECT_LENS.md\n"
                    "Реестр: docs/EXPERTS.md\n"
                    "Приём: docs/INTAKE.md\n"
                )
            ),
            encoding="utf-8",
        )
        (tmp_path / "docs" / "marker.md").write_text("# marker\n", encoding="utf-8")
        monkeypatch.setattr(brain_gate, "ROOT", tmp_path)
        monkeypatch.setattr(brain_gate, "AGENTS", tmp_path / "AGENTS.md")
        monkeypatch.setattr(brain_gate, "LENS", docs / "PROJECT_LENS.md")
        monkeypatch.setattr(brain_gate, "EXPERTS", docs / "EXPERTS.md")
        monkeypatch.setattr(brain_gate, "INTAKE", docs / "INTAKE.md")
        monkeypatch.setattr(
            brain_gate, "DOCS", (docs / "PROJECT_LENS.md", docs / "EXPERTS.md", docs / "INTAKE.md")
        )

    return _aim


class TestSound:
    def test_matching_brain_passes(self, aim):
        aim()
        assert run() == 0

    def test_real_repo_is_consistent(self):
        """Настоящий репозиторий проходит гейт: линза не разошлась с кодом."""
        assert run("--verbose") == 0

    def test_non_path_tokens_are_ignored(self, aim):
        """Слова в обратных кавычках — не пути, их не проверяем."""
        aim(lens="# Линза\n\nСлово `Celery` и `D-097` и `Python 3.12` — не пути.\n")
        assert run() == 0


class TestCatches:
    def test_missing_role_is_caught(self, aim):
        """Роль выпала из реестра — совет без неё станет монологом."""
        aim(registry=build_registry(drop_role="шут"))
        assert run() == 1

    def test_role_without_output_field_is_caught(self, aim):
        """«Посмотрел и сказал» — не выход: поле обязано быть."""
        aim(registry=build_registry(drop_field="Проверяемый выход"))
        assert run() == 1

    def test_empty_field_is_caught(self, aim):
        aim(
            registry=build_registry().replace(
                "- **Что делает:** делает что-то конкретное для проекта.",
                "- **Что делает:** —",
            )
        )
        assert run() == 1

    def test_vanished_path_is_caught(self, aim):
        """Линза помнит каталог, которого в коде уже нет."""
        aim(lens="# Линза\n\nПроект: `docs/vanished/`\n")
        assert run() == 1

    def test_missing_simple_route_marker_is_caught(self, aim):
        aim(registry=build_registry().replace("координатор → программист", "как-то"))
        assert run() == 1

    def test_missing_council_cap_is_caught(self, aim):
        aim(registry=build_registry().replace("Потолок совета — 3 эксперта.", "Совет большой."))
        assert run() == 1

    def test_update_rule_lost_in_agents_is_caught(self, aim):
        """Правило обновления выкинули из правил проекта — это молчаливый отказ."""
        aim(agents="# Канон\n\nЛинза: docs/PROJECT_LENS.md\n")
        assert run() == 1

    def test_missing_doc_link_in_agents_is_caught(self, aim):
        aim(
            agents=(
                "# Канон\n\nИзменился проект — изменилась линза и реестр.\n\n"
                "Линза: docs/PROJECT_LENS.md\n"
                "Реестр: docs/EXPERTS.md\n"
            )
        )
        assert run() == 1


class TestCannotLook:
    def test_unreadable_brain_is_not_success(self, monkeypatch, tmp_path):
        """Файлов нет — это код 2, а не «сошлось»."""
        monkeypatch.setattr(brain_gate, "ROOT", tmp_path)
        monkeypatch.setattr(brain_gate, "AGENTS", tmp_path / "AGENTS.md")
        monkeypatch.setattr(brain_gate, "LENS", tmp_path / "docs" / "PROJECT_LENS.md")
        monkeypatch.setattr(brain_gate, "EXPERTS", tmp_path / "docs" / "EXPERTS.md")
        monkeypatch.setattr(brain_gate, "INTAKE", tmp_path / "docs" / "INTAKE.md")
        assert run() == 2
