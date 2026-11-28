"""Public context for distinguishing assignments with the same title in a course."""
from collections import Counter
from datetime import datetime, timedelta, timezone


def due_label(due_at):
    if not due_at:
        return '마감일 미설정'
    instant = datetime.fromisoformat(due_at.replace('Z', '+00:00'))
    local = instant.astimezone(timezone(timedelta(hours=9)))
    return f'마감 {local:%Y-%m-%d %H:%M} KST'


def choice_context(due_at, assignment_id, *, same_title, same_minute):
    if same_title < 2:
        return ''
    label = due_label(due_at)
    if same_minute > 1:
        label += f' · 과제 구분 {assignment_id}'
    return label


def add_choice_contexts(rows):
    """Annotate a full, unfiltered list of unique assignments in a single course."""
    # Match SQLite COLLATE NOCASE, which folds ASCII letters only.
    lowercase = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')
    groups = [(row['title'].translate(lowercase), due_label(row['due_at'])) for row in rows]
    titles = Counter(title for title, _ in groups)
    minutes = Counter(groups)
    for row, (title, minute) in zip(rows, groups):
        context = choice_context(row['due_at'], row['assignment_id'], same_title=titles[title],
                                 same_minute=minutes[title, minute])
        if context:
            row['title_context'] = context
