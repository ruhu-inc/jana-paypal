// Exam notation is presentation only: retain exact decimal strings and their stated precision.
export function accountingAmount(amount: string, currency?: string, brackets = false): string {
  if (amount === "") return "";
  const match = /^(-?)(\d+(?:,\d{3})*)(\.\d+)?$/.exec(amount);
  if (!match) return amount;
  const digits = (match[2] ?? "").replace(/,/g, "");
  const grouped = digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (match[3] ?? "");
  const symbol = !currency || currency === "USD" ? "$" : currency;
  const body = `${symbol}${/^[A-Z]{3}$/.test(symbol) ? " " : ""}${grouped}`;
  return match[1] ? brackets ? `(${body})` : `-${body}` : body;
}
