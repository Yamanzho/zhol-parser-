class ScoutError(Exception):
    pass


class InvalidField(ScoutError):
    def __init__(self, key: str, detail: str) -> None:
        self.key = key
        super().__init__(f"invalid field {key}: {detail}")


class CollectionFailed(ScoutError):
    pass
