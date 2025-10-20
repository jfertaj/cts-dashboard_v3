from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.cell.cell import Cell

def get_merged_value(ws, cell_ref):
    for rng in ws.merged_cells.ranges:
        if cell_ref in str(rng):
            return ws[rng.min_row][rng.min_col - 1].value
    return ws[cell_ref].value

def is_merged_cell(ws, cell_ref):
    for rng in ws.merged_cells.ranges:
        if cell_ref in str(rng):
            return True
    return False

