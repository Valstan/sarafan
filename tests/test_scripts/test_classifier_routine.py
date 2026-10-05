"""Тесты обвязки облачной рутины классификатора (scripts/classifier_routine.py).

Покрывают чистую логику: валидацию пакетов вердиктов и черновиков правил (то,
что модель чаще всего портит в JSON) и выбор базового URL из env. HTTP-функции
(`fetch`/`submit`/`corrections`/`propose`) не трогаем — они интеграционные,
живут против прод-API.
"""

from scripts.classifier_routine import (
    DEFAULT_API_BASE,
    RULE_TEXT_MAX,
    _fetch_merged_pending,
    api_base,
    validate_proposals,
    validate_verdicts,
)


def _verdict(**overrides):
    base = {
        "lip": "156168183_14260",
        "theme": "новости района",
        "action": "publish",
        "merge_with": [],
        "split": False,
        "confidence": 90,
        "reasoning": "локальное событие",
        "text": "т",
        "url": "https://vk.com/wall-1_1",
        "region_code": "mi",
    }
    base.update(overrides)
    return base


def test_valid_batch_passes():
    payload = {"verdicts": [_verdict(), _verdict(lip="1_2", action="hold")]}
    assert validate_verdicts(payload) == []


def test_not_a_dict_and_empty_list_rejected():
    assert validate_verdicts([_verdict()]) == ['ожидается объект {"verdicts": [...]}']
    assert validate_verdicts({"verdicts": []}) == ["пустой список verdicts — нечего отправлять"]


def test_missing_lip_and_bad_action_reported_with_index():
    payload = {"verdicts": [_verdict(lip=""), _verdict(action="drop")]}
    errors = validate_verdicts(payload)
    assert any(e.startswith("[0]") and "lip" in e for e in errors)
    assert any(e.startswith("[1]") and "action" in e for e in errors)


def test_confidence_bounds_and_bool_rejected():
    assert validate_verdicts({"verdicts": [_verdict(confidence=101)]})
    assert validate_verdicts({"verdicts": [_verdict(confidence=True)]})
    assert validate_verdicts({"verdicts": [_verdict(confidence=None)]}) == []


def test_merge_with_must_be_list():
    assert validate_verdicts({"verdicts": [_verdict(merge_with="1_2")]})


def _proposal(**overrides):
    base = {
        "rule_text": "частные продажи (продам/куплю) — action=hold, не publish",
        "rationale": "оператор трижды поправил publish→hold на объявлениях",
        "evidence": [
            {
                "lip": "1_2",
                "verdict_type": "action",
                "ai_value": "publish",
                "operator_value": "hold",
            }
        ],
        "region_code": None,
        "model": "test-model",
    }
    base.update(overrides)
    return base


def test_valid_proposals_pass():
    payload = {"proposals": [_proposal(), _proposal(rationale=None, evidence=None)]}
    assert validate_proposals(payload) == []


def test_proposals_not_a_dict_and_empty_list_rejected():
    assert validate_proposals([_proposal()]) == ['ожидается объект {"proposals": [...]}']
    assert validate_proposals({"proposals": []}) == ["пустой список proposals — нечего отправлять"]


def test_proposal_rule_text_bounds_reported_with_index():
    payload = {
        "proposals": [
            _proposal(rule_text="  a "),
            _proposal(rule_text="x" * (RULE_TEXT_MAX + 1)),
            _proposal(rule_text=None),
        ]
    }
    errors = validate_proposals(payload)
    assert len(errors) == 3
    assert all("rule_text" in e for e in errors)
    assert errors[0].startswith("[0]") and errors[2].startswith("[2]")


def test_proposal_rationale_and_evidence_types():
    assert validate_proposals({"proposals": [_proposal(rationale="x" * 501)]})
    assert validate_proposals({"proposals": [_proposal(evidence="1_2")]})
    assert validate_proposals({"proposals": [_proposal(), "не объект"]}) == ["[1] не объект"]


def test_api_base_env_override_strips_trailing_slash(monkeypatch):
    monkeypatch.delenv("CLASSIFIER_API_BASE", raising=False)
    assert api_base() == DEFAULT_API_BASE
    monkeypatch.setenv("CLASSIFIER_API_BASE", "https://example.test/")
    assert api_base() == "https://example.test"


def _pending_batch(region, lips):
    return {
        "region_filter": [region],
        "count": len(lips),
        "posts": [
            {"lip": lip, "region_code": region, "text": "новость", "has_media": False}
            for lip in lips
        ],
    }


def test_fetch_merged_pending_combines_regions(monkeypatch):
    import json

    import scripts.classifier_routine as routine

    calls = []

    def fake_request(method, path, body=None):
        calls.append((method, path))
        region = path.split("region=")[1].split("&")[0]
        return json.dumps(_pending_batch(region, [f"{region}_1", f"{region}_2"]))

    monkeypatch.setattr(routine, "_request", fake_request)
    merged = _fetch_merged_pending(["mi", "ur", "klz"], 30)
    assert merged["region_filter"] == ["mi", "ur", "klz"]
    assert merged["count"] == 6
    assert [p["lip"] for p in merged["posts"]] == [
        "mi_1",
        "mi_2",
        "ur_1",
        "ur_2",
        "klz_1",
        "klz_2",
    ]
    assert len(calls) == 3
    assert all(c[0] == "GET" and "limit=30" in c[1] for c in calls)


def test_fetch_merged_pending_skips_empty_batches(monkeypatch):
    import json

    import scripts.classifier_routine as routine

    def fake_request(method, path, body=None):
        if "region=ur" in path:
            return json.dumps(_pending_batch("ur", []))
        return json.dumps(_pending_batch("mi", ["mi_1"]))

    monkeypatch.setattr(routine, "_request", fake_request)
    merged = _fetch_merged_pending(["mi", "ur"], 10)
    assert merged["count"] == 1
    assert merged["posts"][0]["region_code"] == "mi"
