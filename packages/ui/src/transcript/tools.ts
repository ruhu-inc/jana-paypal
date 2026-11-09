// Humanised activity lines (UXS-04): every registered tool maps to a family; each family has running, done and failed
// templates in the catalogue (`tool.<family>.<state>`). The event's `title.obj` (for example "step 3") is the bold
// object of the line. The families are the rows of TABLE UXS-04a; `internal` lines are never rendered.
import type { ToolName } from "../generated/tools.js";

export type ToolFamily =
  | "step_check" | "compute" | "units" | "chem_balance" | "ledger" | "recall" | "lookup" | "visual" | "question"
  | "board" | "memory_write" | "internal";

export const TOOL_FAMILY: Record<ToolName, ToolFamily> = {
  math_check_answer: "step_check", math_check_equivalence: "step_check", math_check_step: "step_check",
  math_parse: "compute", math_solve: "compute", math_simplify: "compute", math_evaluate: "compute",
  math_differentiate: "compute", math_integrate: "compute", math_solve_ode: "compute", math_plot_points: "compute",
  geom_figure: "compute", stats_compute: "compute", stats_distribution: "compute", phys_circuit: "compute",
  phys_mechanics: "compute", phys_optics: "compute", phys_projectile: "compute", bio_calc: "compute",
  bio_genetics: "compute", chem_molar_mass: "compute", chem_stoichiometry: "compute", chem_energetics: "compute",
  chem_equilibrium: "compute", chem_acid_base: "compute", chem_organic: "compute", acc_appraisal: "compute",
  acc_break_even: "compute", acc_cash_flow: "compute", acc_depreciation: "compute", acc_inventory: "compute",
  acc_ratios: "compute", python_run: "compute",
  units_check_dimensions: "units", units_convert: "units",
  chem_balance: "chem_balance",
  acc_double_entry: "ledger", acc_statements: "ledger",
  memory_recall: "recall", session_recall: "recall",
  content_search: "lookup", curriculum_lookup: "lookup", skill: "lookup",
  // The companion's Study tools (SPEC §19.3.3): reads are lookups; an aid or research it starts shows as a button.
  study_search: "lookup", progress_read: "lookup", plan: "lookup", make_aid: "internal", start_research: "internal",
  show_visual: "visual",
  ask_student: "question",
  update_board: "board", show_hint: "board",
  memory_note: "memory_write", progress_update: "memory_write", lesson_plan_write: "memory_write",
  invalid: "internal",
};

export const FAMILIES: readonly ToolFamily[] = [
  "step_check", "compute", "units", "chem_balance", "ledger", "recall", "lookup", "visual", "question", "board",
  "memory_write", "internal",
];

export function familyOf(name: string): ToolFamily {
  return (TOOL_FAMILY as Record<string, ToolFamily>)[name] ?? "internal";
}

export type ToolPhase = "running" | "done" | "failed";

export function phaseOf(state: unknown): ToolPhase {
  const status = (state as { status?: string } | null)?.status;
  if (status === "completed") return "done";
  if (status === "error" || status === "cancelled") return "failed";
  return "running";
}

export function objectOf(state: unknown): string | null {
  const title = (state as { title?: { obj?: string } } | null)?.title;
  return title?.obj ?? null;
}
