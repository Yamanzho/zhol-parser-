from zhol.scoring.adapter import derive, to_score_row
from zhol.scoring.score import load_scoring, score

GROUPS = {"отзывы": "reviews", "цены": "prices", "фото": "photos", "контакты": "contacts"}


def score_groups(result: dict) -> dict[str, int]:
    return {GROUPS[k]: v for k, v in result.items() if k in GROUPS}


__all__ = ["GROUPS", "derive", "load_scoring", "score", "score_groups", "to_score_row"]
