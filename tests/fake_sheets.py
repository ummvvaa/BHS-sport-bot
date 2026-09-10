"""Поддельная Google-таблица в памяти: считает запросы к API и умеет отвечать 429.

Реализует только те методы gspread, которыми пользуется sheets.py.
"""
from __future__ import annotations

import re
from typing import Any

from gspread.exceptions import APIError

A1_RE = re.compile(r"^([A-Z]*)(\d*)(?::([A-Z]*)(\d*))?$")


class FakeResponse:
    """Минимальный requests.Response для gspread.APIError."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.text = message
        self._payload = {"error": {"code": status_code, "message": message, "status": "RESOURCE_EXHAUSTED"}}

    def json(self) -> dict[str, Any]:
        return self._payload


def rate_limit_error(message: str = "Read requests per minute per user") -> APIError:
    return APIError(FakeResponse(429, message))


def _col_index(letters: str) -> int:
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def _split_range(a1: str) -> tuple[str | None, str]:
    """«'Ученики'!A2:E» → («Ученики», «A2:E»)."""
    if "!" not in a1:
        return None, a1
    title, _, rng = a1.partition("!")
    title = title.strip()
    if title.startswith("'") and title.endswith("'"):
        title = title[1:-1].replace("''", "'")
    return title, rng


class FakeWorksheet:
    def __init__(
        self,
        spreadsheet: "FakeSpreadsheet",
        title: str,
        values: list[list[str]] | None = None,
        rows: int = 1000,
        cols: int = 26,
    ):
        self._sh = spreadsheet
        self.title = title
        self.values: list[list[str]] = [list(row) for row in (values or [])]
        self.row_count = rows
        self.col_count = cols

    # --- внутреннее (без учёта запросов) ---

    def _last_row(self) -> int:
        for index in range(len(self.values), 0, -1):
            if any(str(cell).strip() for cell in self.values[index - 1]):
                return index
        return 0

    def _ensure_rows(self, row: int) -> None:
        while len(self.values) < row:
            self.values.append([])

    def _set(self, row: int, col: int, value: Any) -> None:
        self._ensure_rows(row)
        line = self.values[row - 1]
        while len(line) <= col:
            line.append("")
        if value is None:
            line[col] = ""
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            line[col] = str(value)
        else:
            line[col] = value  # RAW: число остаётся числом, как и в реальной таблице

    def read(self, rng: str) -> list[list[str]]:
        match = A1_RE.match(rng.upper().replace("$", ""))
        if not match:
            raise ValueError(f"не разобрал диапазон {rng!r}")
        col_from, row_from, col_to, row_to = match.groups()
        start_row = int(row_from) if row_from else 1
        start_col = _col_index(col_from) if col_from else 0
        if col_to is None and row_to is None:  # одиночная ячейка вида «A1»
            end_row, end_col = start_row, start_col
        else:
            end_row = int(row_to) if row_to else len(self.values)
            end_col = _col_index(col_to) if col_to else 25
        end_row = min(end_row, len(self.values))
        rows: list[list[str]] = []
        for index in range(start_row, end_row + 1):
            line = self.values[index - 1]
            rows.append([str(line[c]) if c < len(line) else "" for c in range(start_col, end_col + 1)])
        while rows and not any(cell.strip() for cell in rows[-1]):  # API обрезает пустой хвост
            rows.pop()
        return rows

    def write(self, rng: str, values: list[list[Any]]) -> None:
        match = A1_RE.match(rng.upper().replace("$", ""))
        if not match:
            raise ValueError(f"не разобрал диапазон {rng!r}")
        col_from, row_from = match.group(1), match.group(2)
        start_row = int(row_from) if row_from else 1
        start_col = _col_index(col_from) if col_from else 0
        for r, row in enumerate(values):
            for c, value in enumerate(row):
                self._set(start_row + r, start_col + c, value)

    # --- публичное API gspread (каждый вызов — запрос) ---

    def get_all_values(self) -> list[list[str]]:
        self._sh._request(f"values.get {self.title}")
        return [list(row) for row in self.values[: self._last_row()]]

    def row_values(self, row: int) -> list[str]:
        self._sh._request(f"values.get {self.title}!{row}")
        return list(self.values[row - 1]) if row <= len(self.values) else []

    def update(self, range_name: str, values: list[list[Any]], value_input_option: Any = None) -> dict:
        self._sh._request(f"values.update {self.title}!{range_name}")
        self.check_grid(range_name, values)
        self.write(range_name, values)
        return {"updatedRange": f"{self.title}!{range_name}"}

    def append_row(self, values: list[Any], value_input_option: Any = None) -> dict:
        self._sh._request(f"values.append {self.title}")
        row = self._last_row() + 1
        self.write(f"A{row}", [values])
        end = chr(ord("A") + max(len(values) - 1, 0))
        return {"updates": {"updatedRange": f"'{self.title}'!A{row}:{end}{row}", "updatedRows": 1}}

    def resize(self, rows: int | None = None, cols: int | None = None) -> dict:
        self._sh._request(f"updateSheetProperties {self.title}")
        if rows is not None:
            self.row_count = rows
        if cols is not None:
            self.col_count = cols
        return {}

    def check_grid(self, rng: str, values: list[list[Any]]) -> None:
        """Google отвечает 400 «exceeds grid limits», если данные не влезают в лист."""
        match = A1_RE.match(rng.upper().replace("$", ""))
        start_row = int(match.group(2)) if match and match.group(2) else 1
        start_col = _col_index(match.group(1)) if match and match.group(1) else 0
        need_rows = start_row + len(values) - 1
        need_cols = start_col + max((len(row) for row in values), default=0)
        if need_rows > self.row_count or need_cols > self.col_count:
            raise APIError(FakeResponse(400, f"Range ('{self.title}'!{rng}) exceeds grid limits"))

    def delete_rows(self, row: int, end_row: int | None = None) -> dict:
        self._sh._request(f"deleteDimension {self.title}!{row}")
        last = end_row or row
        del self.values[row - 1:last]
        return {}


class FakeSpreadsheet:
    def __init__(
        self,
        sheets: dict[str, list[list[str]]] | None = None,
        grids: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        self._sheets: dict[str, FakeWorksheet] = {}
        for title, values in (sheets or {}).items():
            rows, cols = (grids or {}).get(title, (1000, 26))
            self._sheets[title] = FakeWorksheet(self, title, values, rows=rows, cols=cols)
        self.requests: list[str] = []
        self.fail_next: list[APIError | None] = []  # очередь ошибок для следующих запросов

    # --- учёт запросов ---

    def _request(self, what: str) -> None:
        self.requests.append(what)
        if self.fail_next:
            error = self.fail_next.pop(0)
            if error is not None:
                raise error

    @property
    def count(self) -> int:
        return len(self.requests)

    def reset_count(self) -> None:
        self.requests.clear()

    def reads(self) -> list[str]:
        return [r for r in self.requests if r.startswith(("values.batchGet", "values.get", "spreadsheets.get"))]

    def writes(self) -> list[str]:
        return [r for r in self.requests if not r.startswith(("values.batchGet", "values.get", "spreadsheets.get"))]

    # --- API gspread ---

    def worksheets(self) -> list[FakeWorksheet]:
        self._request("spreadsheets.get")
        return list(self._sheets.values())

    def values_batch_get(self, ranges: list[str], params: Any = None) -> dict:
        self._request(f"values.batchGet {len(ranges)}")
        value_ranges = []
        for a1 in ranges:
            title, rng = _split_range(a1)
            ws = self._sheets.get(title or "")
            if ws is None:
                raise APIError(FakeResponse(400, f"Unable to parse range: {a1}"))
            value_ranges.append({"range": a1, "values": ws.read(rng)})
        return {"valueRanges": value_ranges}

    def values_update(self, range_name: str, params: Any = None, body: Any = None) -> dict:
        self._request(f"values.update {range_name}")
        title, rng = _split_range(range_name)
        ws = self._sheets[title or ""]
        values = (body or {}).get("values", [])
        ws.check_grid(rng, values)
        ws.write(rng, values)
        return {"updatedRange": range_name}

    def add_worksheet(self, title: str, rows: int = 100, cols: int = 20) -> FakeWorksheet:
        self._request(f"addSheet {title}")
        ws = FakeWorksheet(self, title, rows=rows, cols=cols)
        self._sheets[title] = ws
        return ws

    # --- помощники для тестов (мимо учёта запросов: «админ правит руками») ---

    def sheet(self, title: str) -> FakeWorksheet:
        return self._sheets[title]

    def rows(self, title: str) -> list[list[str]]:
        return self._sheets[title].values
