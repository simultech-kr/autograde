"""Original filenames are opt-in metadata, never a path or client form field."""
import pytest

from autograde.instructor_upload import parse_instructor_upload


def multipart(filename, extra=b''):
    return (b'--synthetic\r\nContent-Disposition: form-data; name="file"; filename="' + filename +
            b'"\r\n\r\npayload\r\n' + extra + b'--synthetic--\r\n')


def parse(body, capture=True):
    return parse_instructor_upload('multipart/form-data; boundary=synthetic', body,
                                   file_limit=1024, capture_filename=capture)


def test_filename_metadata_is_opt_in_and_preserves_leading_zeros():
    assert parse(multipart(b'001234.pdf')) == {'file': b'payload', 'file_name': '001234.pdf'}
    assert parse(multipart(b'001234.pdf'), capture=False) == {'file': b'payload'}


def test_multipart_library_strips_legacy_windows_browser_prefix():
    # python-multipart normalizes legacy IE paths before returning filename.
    assert parse(multipart(b'C:\\001.zip'))['file_name'] == '001.zip'


@pytest.mark.parametrize('name', [b'../001.pdf', b'folder/001.pdf', b'bad\x00.pdf',
                                  b'', b'x' * 256, b'\xff.pdf'])
def test_unsafe_original_filenames_rejected(name):
    with pytest.raises(ValueError):
        parse(multipart(name))


def test_supplied_filename_form_field_cannot_override_header():
    extra = b'--synthetic\r\nContent-Disposition: form-data; name="file_name"\r\n\r\nother.pdf\r\n'
    with pytest.raises(ValueError):
        parse(multipart(b'001.pdf', extra))
