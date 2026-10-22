class Error(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def missing():
    raise Error(404, "not_found", "Resource not found")


def conflict(message="The resource changed or this operation is no longer allowed"):
    raise Error(409, "conflict", message)
