"""Bounded multipart adapter; boundary parsing belongs to python-multipart.

No client filename is used as a filesystem path. Call only after instructor
authorization, same-origin and signed-cookie checks, before controller CSRF.
"""
from __future__ import annotations

_UNAVAILABLE_MESSAGE = (
    "서버 파일 업로드 모듈을 초기화할 수 없습니다. 운영자는 서버 실행에 사용하는 동일 가상환경에서 "
    "python-multipart 설치·버전과 프로젝트 의존성을 확인·갱신한 뒤 서비스를 재시작해 주세요."
)


class InstructorUploadUnavailable(RuntimeError):
    """Upload-only deployment failure; never fall back to unbounded parsing."""


def parse_instructor_upload(content_type: str, body: bytes, *, file_limit: int) -> dict:
    try:
        from python_multipart import MultipartParser
        from python_multipart.multipart import parse_options_header
    except ImportError as exc:
        raise InstructorUploadUnavailable(_UNAVAILABLE_MESSAGE) from exc
    media, options = parse_options_header(content_type)
    boundary = options.get(b"boundary", b"")
    if media != b"multipart/form-data" or not boundary or len(boundary) > 200:
        raise ValueError("올바른 파일 업로드 형식이 아닙니다.")
    if len(body) > file_limit + 64 * 1024:
        raise ValueError("업로드 요청 크기 제한을 초과했습니다.")
    result = {}
    headers = {}
    header_name = bytearray()
    header_value = bytearray()
    data = bytearray()
    current = {}
    ended = False
    count = 0

    def part_begin():
        nonlocal count
        count += 1
        if count > 7:
            raise ValueError("업로드 항목이 너무 많습니다.")
        headers.clear()
        data.clear()
        current.clear()

    def header_field(chunk, start, end):
        header_name.extend(chunk[start:end])

    def header_data(chunk, start, end):
        header_value.extend(chunk[start:end])

    def header_end():
        name = bytes(header_name).lower()
        if name in headers or name not in {b"content-disposition", b"content-type"}:
            raise ValueError("중복되거나 허용되지 않은 업로드 헤더입니다.")
        headers[name] = bytes(header_value)
        header_name.clear()
        header_value.clear()

    def headers_finished():
        disposition, attrs = parse_options_header(headers.get(b"content-disposition", b""))
        name = attrs.get(b"name", b"").decode("ascii", "strict")
        if disposition != b"form-data" or name not in {"csrf", "revision", "file", "auto_generate", "starter_confirm", "grading_confirm"} or name in result:
            raise ValueError("중복되거나 허용되지 않은 업로드 항목입니다.")
        is_file = b"filename" in attrs
        if (name == "file") != is_file:
            raise ValueError("파일과 입력 항목 형식이 올바르지 않습니다.")
        current.update(name=name, is_file=is_file)

    def part_data(chunk, start, end):
        limit = file_limit if current.get("is_file") else 1024
        if len(data) + end - start > limit:
            raise ValueError("업로드 파일 또는 입력 항목 크기 제한을 초과했습니다.")
        data.extend(chunk[start:end])

    def part_end():
        result[current["name"]] = bytes(data) if current["is_file"] else data.decode("utf-8", "strict")

    def end():
        nonlocal ended
        ended = True

    try:
        parser = MultipartParser(boundary, {
            "on_part_begin": part_begin, "on_header_field": header_field,
            "on_header_value": header_data, "on_header_end": header_end,
            "on_headers_finished": headers_finished, "on_part_data": part_data,
            "on_part_end": part_end, "on_end": end,
        }, max_size=file_limit + 64 * 1024, max_header_count=2, max_header_size=4096)
    except TypeError as exc:
        # Older installed releases do not implement header limits. Do not retry
        # without these limits or misreport a server dependency as a bad ZIP.
        raise InstructorUploadUnavailable(_UNAVAILABLE_MESSAGE) from exc
    try:
        parser.write(body)
        parser.finalize()
    except Exception as exc:
        # Parser exceptions can include raw headers: never return those values.
        raise ValueError("파일 업로드 형식 또는 크기를 확인하고 다시 시도하세요.") from exc
    if not ended or "file" not in result or not result["file"]:
        raise ValueError("파일 업로드가 완료되지 않았습니다. 다시 선택하세요.")
    return result
