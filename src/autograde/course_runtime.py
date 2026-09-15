"""Dynamic course services sharing one bounded student worker pool."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import itertools
import threading

from .platform_bundle_worker import BundleSubmissionWorker
from .platform_state import PlatformNotFound


class CourseRuntimeRegistry(Mapping):
    """Lazily construct each registered course once; never create from URL input."""

    def __init__(self, courses, factory):
        self.courses = courses
        self.factory = factory
        self._services = {}
        self._processors = {}
        self._lock = threading.RLock()

    def __getitem__(self, key):
        with self._lock:
            self.courses.get_course(key)
            if key not in self._services:
                service, processor = self.factory(key)
                self._services[key] = service
                self._processors[key] = processor
            return self._services[key]

    def __iter__(self):
        return iter([course["course_key"] for course in self.courses.list_courses()])

    def __len__(self):
        return len(self.courses.list_courses())

    def __contains__(self, key):
        try:
            self.courses.get_course(key)
            return True
        except PlatformNotFound:
            return False

    def processor(self, key):
        self[key]
        return self._processors[key]


class _CourseProcessor:
    course_key = "portal-shared"

    def __init__(self, state, registry):
        self.state = state
        self.registry = registry

    @staticmethod
    def _aware_now():
        return datetime.now(timezone.utc)

    def process(self, submission_id):
        submission = self.state.get_bundle_submission(submission_id)
        assignment = self.state.get_bundle_assignment(submission.assignment_id)
        return self.registry.processor(assignment.course_key).process(submission_id)


class SharedCourseWorker(BundleSubmissionWorker):
    """One queue/pool for all courses, round-robin durable recovery across courses.

    Upload notifications wake the pool; durable recovery chooses the order so a
    busy course cannot fill the in-memory queue ahead of every other course.
    """

    def __init__(self, state, registry, *, worker_count=4, recovery_interval_seconds=0.5):
        super().__init__(_CourseProcessor(state, registry), course_key="portal-shared",
                         worker_count=worker_count,
                         recovery_interval_seconds=recovery_interval_seconds)
        self.registry = registry
        self._recovery_lock = threading.Lock()
        self._course_offset = 0

    def submission_available(self, submission_id):
        # Receipt is already durable. Keep HTTP intake free of recovery queries.
        return True

    def recover(self):
        with self._recovery_lock:
            courses = list(self.registry)
            if not courses:
                return 0
            offset = self._course_offset % len(courses)
            courses = courses[offset:] + courses[:offset]
            self._course_offset += 1
            queues = []
            for course in courses:
                pending = self.processor.state.list_bundle_submissions_for_processing(course_key=course)
                pending += self.processor.state.list_publishable_bundle_submissions(
                    course_key=course, at=self.processor._aware_now())
                queues.append(pending)
            count = 0
            for row in itertools.zip_longest(*queues):
                for request in row:
                    if request is not None:
                        count += self.notify(request.submission_id)
            return count
