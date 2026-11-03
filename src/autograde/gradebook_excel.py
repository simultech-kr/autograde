"""Bounded XLSX parsing for instructor-authored, student-keyed grade sheets.

Formulae are never executed. Only Excel's saved result is read, and the upload
must be saved with calculated values before an uncached formula can be imported.
"""
from __future__ import annotations

from datetime import date, datetime, time
from io import BytesIO
import math
import re
from zipfile import BadZipFile, ZipFile

from defusedxml.ElementTree import iterparse
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_EXPANDED_BYTES = 40 * 1024 * 1024
MAX_STUDENTS = 5000
MAX_COLUMNS = 100
MAX_VALUE_LENGTH = 2000


class GradebookFormatError(ValueError):
    pass


def _text(value, label, limit=120):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise GradebookFormatError(f'{label}: 1~{limit}자의 내용을 입력하세요.')
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise GradebookFormatError(f'{label}: 줄바꿈이나 제어 문자를 제거하세요.')
    return value


def _validate_package(data, filename, orientation):
    if not isinstance(filename, str) or not filename.lower().endswith('.xlsx'):
        raise GradebookFormatError('Excel 통합 문서(.xlsx)를 올려 주세요. .xls·.xlsm 파일은 .xlsx로 저장한 뒤 올리세요.')
    if not isinstance(data, bytes) or not data or len(data) > MAX_UPLOAD_BYTES:
        raise GradebookFormatError('엑셀 파일은 비어 있지 않은 5 MB 이하의 .xlsx 파일이어야 합니다.')
    if orientation not in {'rows', 'columns'}:
        raise GradebookFormatError('학생 배치를 행 또는 열로 선택하세요.')
    try:
        with ZipFile(BytesIO(data)) as package:
            entries = package.infolist()
            if len(entries) > 2000 or len({item.filename for item in entries}) != len(entries):
                raise GradebookFormatError('엑셀 내부 파일 구성이 너무 크거나 중복되었습니다. 새 통합 문서로 저장해 주세요.')
            if sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES:
                raise GradebookFormatError('엑셀 내부 데이터가 너무 큽니다. 점수 시트만 새 파일에 복사해 주세요.')
            required = {'[Content_Types].xml', 'xl/workbook.xml', 'xl/_rels/workbook.xml.rels'}
            if not required.issubset({item.filename for item in entries}):
                raise GradebookFormatError('올바른 .xlsx 통합 문서가 아닙니다.')
            for item in entries:
                name = item.filename.lower()
                if item.flag_bits & 1 or 'vbaproject' in name or name.startswith('xl/externallinks/'):
                    raise GradebookFormatError('암호화·매크로·외부 통합 문서 연결이 없는 .xlsx 파일을 사용하세요.')
                if item.filename.startswith('/') or '..' in item.filename.split('/'):
                    raise GradebookFormatError('올바른 .xlsx 내부 경로가 아닙니다.')
                # Parse every XML document with entity expansion prohibited. This
                # also bounds work before openpyxl loads shared strings/styles.
                if not name.endswith(('.xml', '.rels')):
                    continue
                with package.open(item) as stream:
                    for _event, node in iterparse(stream, events=('end',)):
                        if name.startswith('xl/worksheets/') and node.tag.rsplit('}', 1)[-1] == 'mergeCell':
                            raise GradebookFormatError('병합된 셀이 있습니다. 학번·점수 표의 셀 병합을 해제한 뒤 다시 올려 주세요.')
                        if name.startswith('xl/worksheets/') and node.tag.rsplit('}', 1)[-1] == 'c':
                            try:
                                row, column = coordinate_to_tuple(node.attrib['r'])
                            except (KeyError, ValueError):
                                raise GradebookFormatError('셀 위치가 올바르지 않습니다.') from None
                            row_limit, column_limit = ((MAX_STUDENTS + 1, MAX_COLUMNS + 1)
                                                       if orientation == 'rows' else (MAX_COLUMNS + 1, MAX_STUDENTS + 1))
                            if row > row_limit or column > column_limit:
                                raise GradebookFormatError(f'최대 학생 {MAX_STUDENTS:,}명, 점수 항목 {MAX_COLUMNS}개까지 올릴 수 있습니다. 불필요한 먼 셀을 지워 주세요.')
                        node.clear()
    except GradebookFormatError:
        raise
    except Exception as error:
        raise GradebookFormatError('엑셀 파일을 읽을 수 없습니다. Excel에서 새 .xlsx 파일로 저장한 뒤 다시 올려 주세요.') from error


def _value(cell, saved):
    value = saved.value if cell.data_type == 'f' else cell.value
    if cell.data_type == 'f' and value is None:
        raise GradebookFormatError(f'{cell.coordinate}: 수식 계산 결과가 없습니다. Excel에서 계산한 뒤 저장하거나 값으로 붙여 넣어 주세요.')
    if cell.data_type == 'e' or saved.data_type == 'e':
        raise GradebookFormatError(f'{cell.coordinate}: 엑셀 오류 값을 수정해 주세요.')
    if value is None:
        return None
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise GradebookFormatError(f'{cell.coordinate}: 유효한 숫자를 입력하세요.')
        return value
    if isinstance(value, str):
        value = value.strip()
        if len(value) > MAX_VALUE_LENGTH or any(ord(char) < 32 and char not in '\n\t' or ord(char) == 127 for char in value):
            raise GradebookFormatError(f'{cell.coordinate}: 내용은 제어 문자 없이 {MAX_VALUE_LENGTH:,}자 이내로 입력하세요.')
        return value or None
    raise GradebookFormatError(f'{cell.coordinate}: 숫자 또는 문자를 입력하세요.')


def _student_key(value, cell, fallback):
    location = getattr(cell, 'coordinate', fallback)
    if value is None or cell is None:
        raise GradebookFormatError(f'{location}: 학번이 비어 있습니다. 학번을 입력하거나 해당 학생을 지워 주세요.')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < 0 or int(value) != value or value >= 10 ** 15:
            raise GradebookFormatError(f'{location}: 학번은 텍스트 형식으로 입력하세요.')
        digits = str(int(value))
        # Preserve explicit leading zero formatting, without guessing an ID.
        if re.fullmatch(r'0{1,32}', cell.number_format or ''):
            digits = digits.zfill(len(cell.number_format))
        return digits
    return _text(value, f'{location} 학번', 255)


def parse_workbook(data, filename, *, orientation='rows', sheet_name='', student_column='학번'):
    _validate_package(data, filename, orientation)
    workbook = saved = None
    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=False, keep_links=False)
        saved = load_workbook(BytesIO(data), read_only=True, data_only=True, keep_links=False)
        if sheet_name and sheet_name not in workbook.sheetnames:
            raise GradebookFormatError('선택한 시트가 없습니다. 시트 이름을 확인하세요.')
        selected = sheet_name or workbook.sheetnames[0]
        sheet, cached = workbook[selected], saved[selected]
        # Ignore forged or stale dimension metadata. Package cell coordinates
        # were bounded above; resetting prevents iterating a million empty rows.
        sheet.reset_dimensions()
        cached.reset_dimensions()
        cells = list(sheet.iter_rows())
        saved_cells = list(cached.iter_rows())
        if not cells:
            raise GradebookFormatError('시트가 비어 있습니다. 첫 행에 학번과 점수 항목을 입력하세요.')
        width = max(map(len, cells))
        matrix = [[_value(cell, saved_cells[r][c]) for c, cell in enumerate(row)] + [None] * (width - len(row))
                  for r, row in enumerate(cells)]
        if orientation == 'columns':
            matrix = [list(row) for row in zip(*matrix)]
            cells = [list(row) for row in zip(*[list(row) + [None] * (width - len(row)) for row in cells])]
        while matrix and not any(value is not None for value in matrix[-1]):
            matrix.pop()
        while matrix and matrix[0] and not any(row[-1] is not None for row in matrix):
            for row in matrix:
                row.pop()
        if len(matrix) < 2 or len(matrix[0]) < 2:
            raise GradebookFormatError('학번과 점수 항목이 있는 머리글, 학생 1명 이상의 내용을 입력하세요.')
        headers = [_text(value, f'머리글 {index + 1}') for index, value in enumerate(matrix[0])]
        if len(headers) != len(set(headers)):
            raise GradebookFormatError('중복된 열 제목이 있습니다. 점수 항목마다 다른 이름을 사용하세요.')
        student_column = _text(student_column, '학번 열 제목')
        if student_column not in headers:
            raise GradebookFormatError(f'첫 행에서 학번 열 “{student_column}”을 찾지 못했습니다. 열 제목을 확인하세요.')
        student_index = headers.index(student_column)
        columns = [{'key': f'column_{index + 1}', 'label': label} for index, label in enumerate(headers) if index != student_index]
        seen, rows = set(), []
        for row_number, values in enumerate(matrix[1:], 2):
            if not any(value is not None for value in values):
                continue
            location = (f'{get_column_letter(student_index + 1)}{row_number}' if orientation == 'rows'
                        else f'{get_column_letter(row_number)}{student_index + 1}')
            key = _student_key(values[student_index], cells[row_number - 1][student_index], location)
            if key in seen:
                raise GradebookFormatError(f'{location}: 학번 {key}가 중복되었습니다. 학생마다 하나의 항목만 남겨 주세요.')
            seen.add(key)
            rows.append({'student_key': key, 'source_row': row_number,
                         'values': {f'column_{index + 1}': value for index, value in enumerate(values) if index != student_index}})
        if not rows:
            raise GradebookFormatError('학생 행이 없습니다. 학번과 점수를 입력하세요.')
        return {'sheet_name': selected, 'sheet_names': workbook.sheetnames, 'columns': columns, 'rows': rows}
    except GradebookFormatError:
        raise
    except Exception as error:
        raise GradebookFormatError('엑셀 내용을 읽을 수 없습니다. 병합 셀을 해제하고 점수 표만 새 .xlsx에 복사해 주세요.') from error
    finally:
        if workbook is not None:
            workbook.close()
        if saved is not None:
            saved.close()


def template_workbook(student_keys=()):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = '성적'
    sheet.append(['학번', '중간고사', '기말고사', '출석', '기타 평가', '교수자 피드백'])
    for key in student_keys or ('20260001', '20260002'):
        sheet.append([str(key), None, None, None, None, None])
        sheet.cell(sheet.max_row, 1).number_format = '@'
        sheet.cell(sheet.max_row, 1).data_type = 's'
    for cell in sheet[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='2455A6')
    sheet.freeze_panes = 'B2'
    sheet.auto_filter.ref = sheet.dimensions
    for column in 'ABCDEF':
        sheet.column_dimensions[column].width = 18 if column != 'F' else 48
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


template_bytes = template_workbook
