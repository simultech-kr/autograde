export const modules = Object.freeze([
  { id: "courses", label: "수업", capability: "results.read", href: "/app" },
  { id: "results", label: "제출·채점 현황", capability: "results.read", href: "/app" },
  { id: "classroom", label: "실습 교실", capability: "classroom.read", href: "/app/classroom" },
]);
export function availableModules(capabilities: readonly string[]) {
  return modules.filter(module => capabilities.includes(module.capability));
}
