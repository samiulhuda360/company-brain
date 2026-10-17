from eval.run_eval import score_answer, score_citations

ITEM = {
    "answer_type": "entities",
    "gold_aliases": [["Summit Trail Outfitters"], ["Mountain Mile Gear"]],
    "other_aliases": [["Riverstone Outdoors"], ["Riverstone Outdoor Club"]],
    "support": [["ops/orders.csv", "row", 12], ["crm/customers.csv", "row", 1]],
}


def test_entity_answers_need_every_gold_name_and_no_wrong_one() -> None:
    assert score_answer(ITEM, "Mountain Mile Gear, Summit Trail Outfitters")
    assert not score_answer(ITEM, "Summit Trail Outfitters")
    assert not score_answer(ITEM, "Summit Trail Outfitters, Mountain Mile Gear, Riverstone Outdoors")


def test_numbers_allow_formatting_and_a_set_tolerance() -> None:
    assert score_answer({"answer_type": "number", "gold": 6510.0, "tolerance": 0.01}, "NZD $6,510.00")
    assert not score_answer({"answer_type": "number", "gold": 29, "tolerance": 0}, "28 orders")


def test_citations_score_rows_and_passage_ranges() -> None:
    assert score_citations(ITEM, [{"file": "ops/orders.csv", "pos": 12}, {"file": "ops/orders.csv", "pos": 13}]) == (0.5, 0.5)
    assert score_citations(ITEM, [{"file": "ops/orders.csv", "pos": 11, "stop": 20}]) == (1.0, 0.5)
    assert score_citations(ITEM, []) == (None, 0.0)
