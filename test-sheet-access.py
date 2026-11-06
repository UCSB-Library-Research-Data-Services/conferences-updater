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

import os
import pathlib
from typing import Optional

import gspread
from gspread.utils import rowcol_to_a1
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

SERVICE_ACCOUNT_FILE = "service_account.json"
PROMPT_FILE = "prompt.txt"
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


def process(
    conference: str,
    prompt_template: str,
    worksheet: gspread.Worksheet,
    row_num: int,
    col_map: dict[str, int],
    client: OpenAI,
    model: str = "gemini-3.8-flash",
):
    """Invoke an LLM to find Website, Begin, and End for `conference` and write them to the spreadsheet."""
    prompt = prompt_template.replace("{conference}", conference)
    completion = client.beta.chat.completions.parse(
        model=model,
        messages=[
            {"role": "user", "content": prompt},
        ],
        response_format=ConferenceDetails,
    )
    result = completion.choices[0].message.parsed
    dumped = result.model_dump(by_alias=True) if result else {}
    values_to_write = {
        col: (dumped.get(col) or "")
        for col in TARGET_COLUMNS
    }

    print(f"Row {row_num} ({conference}): {values_to_write}")

    updates = [
        {
            "range": rowcol_to_a1(row_num, col_map[col_name]),
            "values": [[val]],
        }
        for col_name, val in values_to_write.items()
    ]
    worksheet.batch_update(updates, value_input_option="USER_ENTERED")


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

    prompt_template = pathlib.Path(PROMPT_FILE).read_text(encoding="utf-8")

    client = OpenAI(
        api_key=load_api_key(),
        base_url=os.environ.get(
            "OPENAI_BASE_URL", "https://litellm.dreamlab.ucsb.edu/v1"
        ),
    )

    for row_num, row in enumerate(rows[1:], start=2):
        conference = row[conf_col_idx] if conf_col_idx < len(row) else ""
        process(conference, prompt_template, worksheet, row_num, col_map, client)
        break  # Temporary: only do one iteration for now


if __name__ == "__main__":
    main()
