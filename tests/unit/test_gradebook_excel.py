from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED

from openpyxl import Workbook, load_workbook
import pytest

from autograde.gradebook_excel import GradebookFormatError, parse_workbook, template_bytes


def excel(rows, setup=None):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = '성적'
    for row in rows:
        sheet.append(row)
    if setup:
        setup(workbook)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def change_package(data, edit, extra=()):
    output = BytesIO()
    with ZipFile(BytesIO(data)) as source, ZipFile(output, 'w', ZIP_DEFLATED) as target:
        for name in source.namelist():
            target.writestr(name, edit(name, source.read(name)))
        for name, value in extra:
            target.writestr(name, value)
    return output.getvalue()


def test_zero_blank_decimal_text_and_leading_zero_id_are_distinct():
    parsed = parse_workbook(excel([['학번', '중간', '기말', '출석', '피드백'], ['001234', 0, None, 9.5, '수정 <필요>']]), 'grades.xlsx')
    assert parsed['rows'] == [{'student_key': '001234', 'source_row': 2,
        'values': {'column_2': 0, 'column_3': None, 'column_4': 9.5, 'column_5': '수정 <필요>'}}]
    assert parsed['columns'][-1]['label'] == '피드백'


def test_student_id_column_can_be_selected_and_numeric_zero_format_is_preserved():
    data = excel([['중간', 'Student ID', '기말'], [50, 1234, 60]],
                 lambda workbook: setattr(workbook.active['B2'], 'number_format', '000000'))
    result = parse_workbook(data, 'test.xlsx', student_column='Student ID')
    assert result['rows'][0]['student_key'] == '001234'
    assert result['columns'] == [{'key': 'column_1', 'label': '중간'}, {'key': 'column_3', 'label': '기말'}]


def test_alternative_students_in_columns_transposes_consistently():
    rows = [['학번', '중간', '기말'], ['001', 0, 80], ['002', 20, None]]
    transposed = [list(row) for row in zip(*rows)]
    first = parse_workbook(excel(rows), 'test.xlsx')
    second = parse_workbook(excel(transposed), 'test.xlsx', orientation='columns')
    assert second['columns'] == first['columns']
    assert second['rows'] == first['rows']


def test_sheet_selection_uses_only_selected_data():
    def add(workbook):
        sheet = workbook.create_sheet('기말')
        sheet.append(['학번', '기말'])
        sheet.append(['002', 70])
    data = excel([['학번', '중간'], ['001', 30]], add)
    result = parse_workbook(data, 'test.xlsx', sheet_name='기말')
    assert result['sheet_name'] == '기말' and result['rows'][0]['student_key'] == '002'
    with pytest.raises(GradebookFormatError, match='시트'):
        parse_workbook(data, 'test.xlsx', sheet_name='없는 시트')


@pytest.mark.parametrize('rows,match', [
    ([['학번', '중간'], ['001', 1], ['001', 2]], '중복'),
    ([['학번', '중간', '중간'], ['001', 1, 2]], '중복'),
    ([['이름', '중간'], ['홍길동', 1]], '학번 열'),
    ([['학번', None], ['001', 1]], '머리글'),
    ([['학번', '중간'], [None, 1]], 'A2.*학번'),
    ([['학번', '중간']], '학생 1명'),
    ([['학번', '중간'], [1.5, 10]], '텍스트'),
    ([['학번', '중간'], [1234567890123456, 10]], '텍스트'),
    ([['학번', '중간'], ['001', '#DIV/0!']], '오류 값'),
])
def test_actionable_header_and_row_validation(rows, match):
    with pytest.raises(GradebookFormatError, match=match):
        parse_workbook(excel(rows), 'test.xlsx')


def test_formula_requires_saved_value_and_does_not_execute_it():
    data = excel([['학번', '중간'], ['001', '=SUM(1,2)']])
    with pytest.raises(GradebookFormatError, match='B2.*계산 결과'):
        parse_workbook(data, 'test.xlsx')
    cached = change_package(data, lambda name, value: value.replace(b'<v />', b'<v>3</v>').replace(b'<v/>', b'<v>3</v>').replace(b'<v></v>', b'<v>3</v>')
                            if name == 'xl/worksheets/sheet1.xml' else value)
    assert parse_workbook(cached, 'test.xlsx')['rows'][0]['values']['column_2'] == 3


@pytest.mark.parametrize('filename', ['grades.xls', 'grades.xlsm', 'grades.zip'])
def test_only_xlsx_extension(filename):
    with pytest.raises(GradebookFormatError, match='xlsx'):
        parse_workbook(template_bytes(), filename)


def test_no_entities_macros_or_external_workbooks():
    data = template_bytes()
    for extra in [('xl/vbaProject.bin', b'macro'), ('xl/externalLinks/externalLink1.xml', b'<test/>')]:
        with pytest.raises(GradebookFormatError, match='매크로'):
            parse_workbook(change_package(data, lambda name, value: value, [extra]), 'test.xlsx')
    hostile = change_package(data, lambda name, value: b'<!DOCTYPE a [<!ENTITY x "expanded">]><a>&x;</a>' if name == 'xl/workbook.xml' else value)
    with pytest.raises(GradebookFormatError, match='읽을 수'):
        parse_workbook(hostile, 'test.xlsx')


def test_cell_limits_merged_cells_and_fake_dimensions_are_handled():
    data = excel([['학번', '점수'], ['001', 50]])
    fake = change_package(data, lambda name, value: value.replace(b'ref="A1:B2"', b'ref="A1:XFD1048576"')
                          if name == 'xl/worksheets/sheet1.xml' else value)
    assert len(parse_workbook(fake, 'test.xlsx')['rows']) == 1
    far = excel([['학번', '점수'], ['001', 50]], lambda workbook: setattr(workbook.active['A5002'], 'value', '999'))
    with pytest.raises(GradebookFormatError, match='5,000'):
        parse_workbook(far, 'test.xlsx')
    merged = excel([['학번', '점수'], ['001', 50]], lambda workbook: workbook.active.merge_cells('B2:C2'))
    with pytest.raises(GradebookFormatError, match='병합'):
        parse_workbook(merged, 'test.xlsx')


def test_template_contains_only_supplied_ids_and_blank_scores():
    result = parse_workbook(template_bytes(['0001', '0002']), 'template.xlsx')
    assert [row['student_key'] for row in result['rows']] == ['0001', '0002']
    assert all(value is None for row in result['rows'] for value in row['values'].values())


def test_template_student_identifier_is_text_even_when_it_starts_with_equals():
    workbook = load_workbook(BytesIO(template_bytes(['=1+1'])), data_only=False)
    assert workbook.active['A2'].value == '=1+1' and workbook.active['A2'].data_type == 's'
    workbook.close()
