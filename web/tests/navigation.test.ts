import test from "node:test";
import assert from "node:assert/strict";
import { courseNavigation, selectedCourse } from "../src/features/navigation.ts";
import { sampleBootstrap } from "./fixtures.ts";

const course = sampleBootstrap.courses[0];

test("course navigation requires a selected course from the current authenticated list", () => {
  assert.equal(selectedCourse(undefined, course.course_key), undefined);
  assert.equal(selectedCourse([], course.course_key), undefined);
  assert.equal(selectedCourse([course], "another-course"), undefined);
  assert.equal(selectedCourse([course], course.course_key), course);
  assert.deepEqual(courseNavigation(selectedCourse([], course.course_key), "results", true), []);
});

test("invalid course segments cannot construct portal links even if repeated in a response", () => {
  for (const key of ["", ".", "..", "../other", "//remote.test", "https://remote.test", "a/b", "a\\b", "a%2fb", "a?course_key=b", "a#b", "a\n", "a\r", "a\0", "a".repeat(97)]) {
    const value = { ...course, course_key: key };
    assert.equal(selectedCourse([value], key), undefined, key);
    assert.deepEqual(courseNavigation(value, "results", true), [], key);
  }
});

test("every course link keeps the selected scope and current result page is identified once", () => {
  const scoped = { ...course, course_key: "come2201-2026_02" };
  const links = courseNavigation(scoped, "results", true);
  assert.equal(links.length, 8);
  assert.deepEqual(links.filter(link => link.current).map(link => link.id), ["results"]);
  const base = `/courses/${scoped.course_key}/instructor`;
  assert.equal(links.find(link => link.id === "home")?.href, base);
  assert.equal(links.find(link => link.id === "assignments")?.href, `${base}/assignments`);
  assert.equal(links.find(link => link.id === "students")?.href, `${base}/students`);
  assert.equal(links.find(link => link.id === "registrations")?.href, `${base}/registrations`);
  assert.equal(links.find(link => link.id === "environments")?.href, `${base}/environments`);
  assert.equal(links.find(link => link.id === "settings")?.href, `${base}/settings`);
  assert.equal(links.find(link => link.id === "results")?.href, `/app?course_key=${scoped.course_key}`);
  assert.equal(links.find(link => link.id === "classroom")?.href, `/app/classroom?course_key=${scoped.course_key}`);
  assert.ok(!links.some(link => ["content", "materials", "extensions", "rubrics"].includes(link.id)));
});

test("classroom keeps an existing Python results fallback when Next results are not enabled", () => {
  const links = courseNavigation(course, "classroom");
  assert.deepEqual(links.filter(link => link.current).map(link => link.id), ["classroom"]);
  assert.equal(links.find(link => link.id === "results")?.href, `/courses/${course.course_key}/instructor/submissions`);
});
