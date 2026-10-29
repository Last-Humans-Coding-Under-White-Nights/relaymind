// The sanitizer of a host capture (RM12, #135), kept apart from the capture
// script so a test can import it without starting the endpoint.

// Replace the home directory with <home> in a fixture's JSON text. The text is
// JSON, so a home that holds backslashes (C:\Users\x on Windows) appears there
// in its escaped form (C:\\Users\\x); both the escaped and the raw form are
// replaced, the escaped one first.
export function sanitizeCapture(text, home) {
  if (!home) return text;
  const escaped = JSON.stringify(home).slice(1, -1);
  let out = text.split(escaped).join("<home>");
  if (escaped !== home) out = out.split(home).join("<home>");
  return out;
}
