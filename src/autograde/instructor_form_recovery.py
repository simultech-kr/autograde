"""Restore safe fields only in the failed form, never credentials or file bytes."""
from html import escape
from html.parser import HTMLParser
import re


class FormRecovery(HTMLParser):
    def __init__(self, action, values):
        super().__init__(convert_charrefs=False)
        self.action, self.values = action, values
        self.output = []
        self.active = False
        self.select = None
        self.skip_text = False
        self.found = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'form':
            self.active = attributes.get('action') == self.action and attributes.get('method') == 'post'
            if self.active and attributes.get('data-form-kind'):
                submitted_kind = 'tests' if self.values.get('tests_present') == 'yes' else 'problem'
                self.active = attributes['data-form-kind'] == submitted_kind
            self.found |= self.active
            if self.active:
                attributes['data-recovered'] = ''
        name = attributes.get('name', '')
        value = self.values.get(name)
        if self.values.get('tests_present') == 'yes' and re.fullmatch(r'test_\d+_(title|input|output|weight|public)', name):
            # Disabled/removed case controls are absent from enhanced submissions.
            # Do not resurrect their old database values after a failed save.
            value = self.values.get(name, '')
        safe = isinstance(value, str) and name not in {'password', 'csrf', 'token', 'secret', 'expected_name'}
        if self.active and tag == 'input':
            kind = attributes.get('type', 'text')
            if kind in {'checkbox', 'radio'}:
                attributes.pop('checked', None)
                if safe and value == attributes.get('value'):
                    attributes['checked'] = None
            elif safe and kind not in {'file', 'password', 'hidden', 'submit'}:
                attributes['value'] = value
            elif safe and kind == 'hidden' and name in {'revision', 'delete_revision', 'creation_key'}:
                # Never silently upgrade a stale edit to the latest revision.
                attributes['value'] = value
        if self.active and tag == 'select':
            self.select = value if safe else None
        if self.active and tag == 'option' and self.select is not None:
            attributes.pop('selected', None)
            if attributes.get('value') == self.select:
                attributes['selected'] = None
        self.output.append('<' + tag + ''.join(' ' + key + ('' if val is None else '="' + escape(val, quote=True) + '"') for key, val in attributes.items()) + '>')
        if self.active and tag == 'textarea' and safe:
            self.output.append(escape(value))
            self.skip_text = True

    def handle_endtag(self, tag):
        if tag == 'textarea':
            self.skip_text = False
        if tag == 'select':
            self.select = None
        if tag == 'form':
            self.active = False
        self.output.append('</' + tag + '>')

    def handle_data(self, data):
        if not self.skip_text:
            self.output.append(data)

    def handle_entityref(self, name):
        self.handle_data('&' + name + ';')

    def handle_charref(self, name):
        self.handle_data('&#' + name + ';')

    @classmethod
    def restore(cls, body, action, values):
        parser = cls(action, values)
        parser.feed(body)
        return ''.join(parser.output) if parser.found else None
