"""Runtime invariants for the incremental instructor schema (migration 11)."""


def initialize_instructor_runtime(connection):
    # The explicit empty web bootstrap has zero enrollments; preserve old marks.
    connection.execute("ALTER TABLE platform_roster_bootstrap RENAME TO platform_roster_bootstrap_v9")
    connection.execute("CREATE TABLE platform_roster_bootstrap (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                       "enrollment_count INTEGER NOT NULL CHECK(enrollment_count>=0), initialized_at TEXT NOT NULL)")
    connection.execute("INSERT INTO platform_roster_bootstrap SELECT * FROM platform_roster_bootstrap_v9")
    connection.execute("DROP TABLE platform_roster_bootstrap_v9")
    # Fence admission in the same write transaction as course archive. The facade
    # also checks status on every read. Unregistered legacy CLI-only courses are
    # unchanged; they are not exposed by the dynamic portal registry.
    scopes = {
        "platform_sessions": "NEW.course_key",
        "platform_token_families": "NEW.course_key",
        "device_authorizations": "NEW.course_key",
        "platform_assignment_grants": "(SELECT course_key FROM platform_enrollments WHERE id=NEW.enrollment_id)",
        "platform_assignment_acceptances": "(SELECT course_key FROM platform_enrollments WHERE id=NEW.enrollment_id)",
        "bundle_submission_requests": "(SELECT course_key FROM bundle_assignment_releases WHERE assignment_id=NEW.assignment_id)",
    }
    for table, scope in scopes.items():
        connection.execute(f"CREATE TRIGGER admin_active_{table} BEFORE INSERT ON {table} "
                           f"WHEN EXISTS (SELECT 1 FROM admin_courses WHERE course_key={scope} AND status!='active') "
                           "BEGIN SELECT RAISE(ABORT, 'admin_course_not_active'); END")
