"""Mirror writes to Connor's existing Baseball_Cards.xlsx workbook.

We append rows under the Inventory tab using the existing column layout.
This is a failsafe so the data lives in the same workbook he's been
maintaining, even if Google Sheets is offline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable
from datetime import datetime

import openpyxl

from app.config import settings
from app import models


# These column indices match the existing Inventory header row (row 4):
# A: Card # (formula), B: Year, C: Set, D: Player, E: Card # in Set,
# F: Parallel, G: Condition, H: Storage, I: Est Raw, J: Est Graded,
# K: Comps URL, L: Status, M: Channel, N: Sold Price, O: Fee %,
# P: Net (formula), Q: Notes
HEADER_ROW = 4


def _open():
    path = settings.local_xlsx_path
    if not path or not Path(path).exists():
        return None, None
    wb = openpyxl.load_workbook(path)
    if "Inventory" not in wb.sheetnames:
        return None, None
    return wb, wb["Inventory"]


def append_card(card: models.Card) -> bool:
    wb, ws = _open()
    if ws is None:
        return False
    # find first empty row after header
    row = HEADER_ROW + 1
    while ws.cell(row=row, column=2).value is not None:
        row += 1
    ws.cell(row=row, column=1, value="=ROW()-4")
    ws.cell(row=row, column=2, value=card.year)
    ws.cell(row=row, column=3, value=card.set_brand)
    ws.cell(row=row, column=4, value=card.player)
    ws.cell(row=row, column=5, value=card.card_no)
    ws.cell(row=row, column=6, value=card.parallel)
    ws.cell(row=row, column=7, value=card.condition)
    ws.cell(row=row, column=8, value=card.storage)
    ws.cell(row=row, column=9, value=card.est_value_raw or card.comp_median)
    ws.cell(row=row, column=10, value=card.est_value_graded)
    ws.cell(row=row, column=11, value=card.comp_url)
    ws.cell(row=row, column=12, value=card.status)
    ws.cell(row=row, column=13, value=card.channel)
    ws.cell(row=row, column=14, value=card.sold_price)
    ws.cell(row=row, column=15, value=card.fee_pct or 0.13)
    ws.cell(row=row, column=16, value=f"=IFERROR(N{row}*(1-O{row}),0)")
    notes_bits = []
    if card.notes:
        notes_bits.append(card.notes)
    if card.is_hit_watchlist and card.hit_reason:
        notes_bits.append(f"HIT: {card.hit_reason}")
    notes_bits.append(f"Auto-cataloged {datetime.utcnow():%Y-%m-%d}")
    ws.cell(row=row, column=17, value=" | ".join(notes_bits))
    wb.save(settings.local_xlsx_path)
    return True
