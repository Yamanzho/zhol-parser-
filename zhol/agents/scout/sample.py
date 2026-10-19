from __future__ import annotations

import hashlib


def selected_for_recheck(id_2gis: str | None) -> bool:
    if not id_2gis:
        return False
    digest = hashlib.sha256(f"scout-recheck:{id_2gis}".encode()).digest()
    return digest[0] % 10 == 0
