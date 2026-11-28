"""Course and section display text for both role portals."""
from collections import Counter


SEMESTER = {'1': '1학기', '2': '2학기', 'summer': '여름학기', 'winter': '겨울학기'}


def section_label(section):
    section = str(section or '').strip()
    return section + '분반' if section and not section.endswith('분반') else section


def course_location_label(course):
    name = course.get('name') or course['code'] or course['course_key']
    term = ' '.join(filter(None, [f'{course["year"]}학년도' if course.get('year') else '',
                                  SEMESTER.get(course.get('semester'), '')]))
    return ' · '.join(filter(None, [name, term, section_label(course.get('section')),
                                    course['code'] if course['code'] != name else '']))


def course_location_labels(courses):
    labels = {course['course_key']: course_location_label(course) for course in courses}
    counts = Counter(labels.values())
    return {key: label if counts[label] == 1 else f'{label} · {key}' for key, label in labels.items()}
