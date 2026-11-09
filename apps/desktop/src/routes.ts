// UXS-01: click paths from Home for the S2 route screens. Lessons require an installed lesson; the tutor requires
// an available tutor. Conditional states (exam review, safety/capture, first run) are reached by their owning flows.
export const S2_ROUTES = [
  { screen: "home", keys: [] },
  { screen: "tutor", keys: ["composer.new_question"] },
  { screen: "learn", keys: ["nav.lessons"] },
  { screen: "lesson", keys: ["nav.lessons", "map.open_lesson"] },
  { screen: "practice", keys: ["app.practice"] },
  { screen: "mock", keys: ["app.mock"] },
  { screen: "progress", keys: ["app.progress"] },
  { screen: "assessment", keys: ["assessment.find_start"] },
  { screen: "search", keys: ["search.title"] },
  { screen: "settings", keys: ["frame.menu.label", "app.settings"] }, // the student menu, then Settings in it
  { screen: "knows-from-progress", keys: ["app.progress", "progress.knows"] },
  { screen: "saved-questions", keys: ["nav.notes_saved", "saved_questions.title"] },
] as const;
export type S2Screen = (typeof S2_ROUTES)[number]["screen"];
