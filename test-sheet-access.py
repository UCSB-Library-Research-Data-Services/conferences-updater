#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "gspread",
#     "google-auth",
#     "openai>=1.60.0",
#     "pydantic>=2.0.0",
# ]
# ///

import csv
from datetime import date, datetime, timezone
import os
import pathlib
import sys
from typing import Optional

import gspread
from gspread.utils import rowcol_to_a1
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

SERVICE_ACCOUNT_FILE = "service_account.json"
PROMPT_FILE = "prompt.txt"
ACTION_LOG_FILE = "action_log.csv"
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1b846EZadawdaeEN7xTdfSggazWiRDCuHfH7V6X3ehBE/edit?gid=0#gid=0"

# Restrict scope to Google Sheets only (no Google Drive browsing/listing needed when opening by URL/ID)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TARGET_COLUMNS = [
    "Location",
    "Begin",
    "End",
    "Papers due",
    "Posters due",
    "Reg opens",
    "Reg closes",
    "Website",
]

OTHER_DATE_COLUMNS = [
    "Papers due",
    "Posters due",
    "Reg opens",
    "Reg closes",
]

RED_TEXT_FORMAT = {
    "textFormat": {
        "foregroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0},
    }
}

BLACK_TEXT_FORMAT = {
    "textFormat": {
        "foregroundColor": {"red": 0.0, "green": 0.0, "blue": 0.0},
    }
}


def parse_iso_date(val: Optional[str]) -> Optional[date]:
    """Parse a YYYY-MM-DD prefix from an ISO 8601 date string, returning None if invalid."""
    if not val:
        return None
    s = val.strip()
    if len(s) >= 10:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


class ConferenceDetails(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    Location: Optional[str] = Field(
        None,
        description='The city the conference takes place in ("city, state" in US or "city, country" outside US).',
    )
    Begin: Optional[str] = Field(
        None,
        description="The date the conference starts on (ISO 8601 format).",
    )
    End: Optional[str] = Field(
        None,
        description="The date the conference ends on (ISO 8601 format).",
    )
    Papers_due: Optional[str] = Field(
        None,
        alias="Papers due",
        description="The date full paper submissions are due (ISO 8601 format).",
    )
    Posters_due: Optional[str] = Field(
        None,
        alias="Posters due",
        description="The date poster and lightning talk submissions are due (ISO 8601 format).",
    )
    Reg_opens: Optional[str] = Field(
        None,
        alias="Reg opens",
        description="The date registration opens (ISO 8601 format).",
    )
    Reg_closes: Optional[str] = Field(
        None,
        alias="Reg closes",
        description="The date registration closes (ISO 8601 format).",
    )
    Website: Optional[str] = Field(
        None,
        description="The URL of the conference's homepage.",
    )


def load_api_key() -> str:
    """Load API key from environment or ~/.config/dreamlab/llm.env."""
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DREAMLAB_LLM_KEY")
    if key:
        return key

    env_path = pathlib.Path.home() / ".config" / "dreamlab" / "llm.env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export ") :]
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == "DREAMLAB_LLM_KEY":
                    return v.strip().strip("\"'")

    raise RuntimeError(
        "No API key found. Set OPENAI_API_KEY or DREAMLAB_LLM_KEY in your environment."
    )


def log_cell_changes(
    log_path: pathlib.Path,
    changes: list[tuple[str, str, str, str]],
) -> None:
    """Append changed cells (timestamp, cell, old_value, new_value) to action_log.csv."""
    if not changes:
        return
    write_header = not log_path.exists() or log_path.stat().st_size == 0
    with log_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "cell", "old_value", "new_value"])
        writer.writerows(changes)


def process(
    conference: str,
    row: list[str],
    prompt_template: str,
    worksheet: gspread.Worksheet,
    row_num: int,
    col_map: dict[str, int],
    client: OpenAI,
    log_path: pathlib.Path,
    model: str = "gemini-3.8-flash",
):
    """Invoke an LLM to find conference details for `conference` and write them to the spreadsheet."""
    today = date.today()
    prompt = (
        prompt_template
        .replace("{conference}", conference)
        .replace("{today}", today.isoformat())
    )
    completion = client.beta.chat.completions.parse(
        model=model,
        messages=[
            {"role": "user", "content": prompt},
        ],
        response_format=ConferenceDetails,
        web_search_options={"search_context_size": "medium"},
    )
    result = completion.choices[0].message.parsed
    dumped = result.model_dump(by_alias=True) if result else {}
    values_to_write = {
        col: (dumped.get(col) or "")
        for col in TARGET_COLUMNS
    }

    begin_date = parse_iso_date(values_to_write["Begin"])
    end_date = parse_iso_date(values_to_write["End"])
    conf_ref_date = end_date or begin_date

    is_past_conference = conf_ref_date is not None and conf_ref_date < today
    is_future_conference = conf_ref_date is not None and conf_ref_date >= today

    if is_past_conference:
        for col_name in OTHER_DATE_COLUMNS:
            values_to_write[col_name] = ""

    print(f"Row {row_num} ({conference}): {values_to_write}")

    updates = []
    changes_to_log: list[tuple[str, str, str, str]] = []
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for col_name, new_val in values_to_write.items():
        col_1based = col_map[col_name]
        col_0based = col_1based - 1
        old_val = row[col_0based] if col_0based < len(row) else ""
        cell_a1 = rowcol_to_a1(row_num, col_1based)

        updates.append(
            {
                "range": cell_a1,
                "values": [[new_val]],
            }
        )
        if old_val != new_val:
            changes_to_log.append((timestamp, cell_a1, old_val, new_val))

    worksheet.batch_update(updates, value_input_option="USER_ENTERED")
    log_cell_changes(log_path, changes_to_log)

    formats = []
    for col_name in OTHER_DATE_COLUMNS:
        cell_a1 = rowcol_to_a1(row_num, col_map[col_name])
        cell_date = parse_iso_date(values_to_write[col_name])
        if is_future_conference and cell_date is not None and cell_date >= today:
            formats.append({"range": cell_a1, "format": RED_TEXT_FORMAT})
        else:
            formats.append({"range": cell_a1, "format": BLACK_TEXT_FORMAT})
    worksheet.batch_format(formats)


def parse_row_args(args: list[str], max_row: int) -> list[int]:
    """Parse CLI arguments of row numbers (e.g. '3') or ranges (e.g. '4-7')."""
    if not args:
        return list(range(2, max_row + 1))

    selected: list[int] = []
    seen: set[int] = set()

    for arg in args:
        if "-" in arg:
            parts = arg.split("-", 1)
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                raise ValueError(f"Invalid row range argument: {arg!r}")
            start_row, end_row = int(parts[0]), int(parts[1])
            if start_row < 2 or end_row < start_row:
                raise ValueError(
                    f"Invalid row range {arg!r} (rows must be >= 2 and start <= end)"
                )
            row_iter = range(start_row, end_row + 1)
        else:
            if not arg.isdigit():
                raise ValueError(f"Invalid row argument: {arg!r}")
            r = int(arg)
            if r < 2:
                raise ValueError(f"Row number must be >= 2 (got {r})")
            row_iter = range(r, r + 1)

        for r in row_iter:
            if r > max_row:
                raise ValueError(
                    f"Row {r} is out of range (spreadsheet has {max_row} rows)"
                )
            if r not in seen:
                seen.add(r)
                selected.append(r)

    return selected


def main():
    gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    sh = gc.open_by_url(SPREADSHEET_URL)
    worksheet = sh.get_worksheet_by_id(0) or sh.sheet1

    rows = worksheet.get_all_values()
    if not rows:
        print("Spreadsheet is empty.")
        return

    headers = rows[0]
    required_cols = ["Conference", *TARGET_COLUMNS]
    for col in required_cols:
        if col not in headers:
            raise ValueError(f"{col!r} column not found in header row: {headers}")

    conf_col_idx = headers.index("Conference")  # 0-based for reading from `row` list
    col_map = {
        col: headers.index(col) + 1  # 1-based column indices for gspread
        for col in TARGET_COLUMNS
    }

    target_rows = parse_row_args(sys.argv[1:], len(rows))

    prompt_template = pathlib.Path(PROMPT_FILE).read_text(encoding="utf-8")
    log_path = pathlib.Path(ACTION_LOG_FILE)

    client = OpenAI(
        api_key=load_api_key(),
        base_url=os.environ.get(
            "OPENAI_BASE_URL", "https://litellm.dreamlab.ucsb.edu/v1"
        ),
    )

    for row_num in target_rows:
        row = rows[row_num - 1]
        conference = row[conf_col_idx] if conf_col_idx < len(row) else ""
        process(
            conference,
            row,
            prompt_template,
            worksheet,
            row_num,
            col_map,
            client,
            log_path,
        )

    if len(rows) > 1:
        sort_range = f"A2:{rowcol_to_a1(len(rows), len(headers))}"
        worksheet.sort((col_map["Begin"], "asc"), range=sort_range)
        print(f"Sorted rows 2..{len(rows)} by 'Begin' column ascending.")


if __name__ == "__main__":
    main()
